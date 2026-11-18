"""Disk cleanup: old source/clip files, over-limit pruning, orphans, stale .part files, disk warnings."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.db.enums import CampaignStatus, ClipStatus, SourceStatus
from app.db.models import AppSetting, Campaign, Clip, Source
from app.downloader.media import GB, disk_usage_gb, volume_used_pct
from app.logger import get_logger
from app.notify import notify
from app.scout.scorer import utc

log = get_logger("clipflow.cleanup")

WARN_KEY = "disk_warn_at"
WARN_EVERY = timedelta(hours=24)
ORPHAN_AGE = timedelta(hours=24)
PART_AGE = timedelta(hours=6)
OVER_LIMIT_TARGET = 0.9
CLIP_DONE = (ClipStatus.posted, ClipStatus.rejected)
INACTIVE_CAMPAIGN = (CampaignStatus.ended, CampaignStatus.paused)


@dataclass
class Candidate:
    """A DB-tracked file that may be deleted."""

    row: object
    path: Path
    age_ref: datetime
    size: int


def _resolve(p: Optional[str]) -> Optional[Path]:
    return Path(p).resolve() if p else None


def _source_candidates(s: Session) -> list[Candidate]:
    """Status-eligible source files (age not yet applied). Never: downloaded/processing of active campaigns."""
    out = []
    rows = s.execute(select(Source, Campaign.status).join(Campaign, Source.campaign_id == Campaign.id)
                     .where(Source.local_path.is_not(None), Source.file_deleted_at.is_(None))).all()
    for src, camp_status in rows:
        if src.status == SourceStatus.processing:
            continue
        eligible = src.status == SourceStatus.processed or camp_status in INACTIVE_CAMPAIGN
        path = _resolve(src.local_path)
        if eligible and path and path.is_file():
            ref = utc(src.downloaded_at) or utc(src.updated_at) or datetime.now(timezone.utc)
            out.append(Candidate(src, path, ref, path.stat().st_size))
    return out


def _clip_candidates(s: Session) -> list[Candidate]:
    """Posted/rejected clip files. Never: rendered/approved/scheduled."""
    out = []
    for clip in s.scalars(select(Clip).where(Clip.status.in_(CLIP_DONE), Clip.file_path.is_not(None),
                                             Clip.file_deleted_at.is_(None))):
        path = _resolve(clip.file_path)
        if path and path.is_file():
            out.append(Candidate(clip, path, utc(clip.updated_at) or datetime.now(timezone.utc), path.stat().st_size))
    return out


def _delete(c: Candidate, now: datetime, dry_run: bool) -> int:
    if not dry_run:
        c.path.unlink(missing_ok=True)
        c.row.file_deleted_at = now  # local_path / file_path kept for history
    log.info("%s %s (%.1f MB)", "would delete" if dry_run else "deleted", c.path, c.size / 1024 ** 2)
    return c.size


def _tracked_paths(s: Session) -> set[Path]:
    paths = {_resolve(p) for p in s.scalars(select(Source.local_path).where(Source.local_path.is_not(None)))}
    paths |= {_resolve(p) for p in s.scalars(select(Clip.file_path).where(Clip.file_path.is_not(None)))}
    return paths


def _orphans(s: Session, now: datetime, dry_run: bool) -> tuple[int, int]:
    """Delete stale *.part (>6 h) and untracked files (>24 h). Returns (count, bytes)."""
    tracked = _tracked_paths(s)
    count = size = 0
    for root in (Path(settings.DOWNLOAD_DIR), Path(settings.CLIPS_DIR)):
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file():
                continue
            age = now - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            is_part = f.name.endswith(".part")
            if (is_part and age > PART_AGE) or (not is_part and f.resolve() not in tracked and age > ORPHAN_AGE):
                count += 1
                size += f.stat().st_size
                if not dry_run:
                    f.unlink(missing_ok=True)
                log.info("%s orphan %s", "would delete" if dry_run else "deleted", f)
    return count, size


def _storage_gb() -> float:
    return disk_usage_gb(settings.DOWNLOAD_DIR) + disk_usage_gb(settings.CLIPS_DIR)


def _warn_throttled(s: Session, now: datetime, message: str, dry_run: bool) -> bool:
    """Send a disk warning at most once per 24 h; returns True if sent."""
    row = s.scalar(select(AppSetting).where(AppSetting.key == WARN_KEY))
    last = None
    if row and row.value:
        try:
            last = utc(datetime.fromisoformat(row.value))
        except ValueError:
            last = None
    if dry_run or (last and now - last < WARN_EVERY):
        return False
    try:
        notify("Disk warning", message, level="warning")
    except Exception as exc:  # noqa: BLE001
        log.warning("disk warning notify failed: %s", exc)
    if row is None:
        s.add(AppSetting(key=WARN_KEY, value=now.isoformat()))
    else:
        row.value = now.isoformat()
    return True


def run_cleanup(dry_run: bool = False, engine: Optional[Engine] = None, now: Optional[datetime] = None) -> dict:
    """Free disk space safely; returns {files_deleted, gb_freed, orphans, warnings}."""
    now = now or datetime.now(timezone.utc)
    warnings: list[str] = []
    deleted = freed = 0
    with get_session(engine) as s:
        sources = _source_candidates(s)
        clips = _clip_candidates(s)
        keep_src, keep_clip = timedelta(days=settings.KEEP_SOURCE_DAYS), timedelta(days=settings.KEEP_CLIP_DAYS)
        aged = [c for c in sources if now - c.age_ref > keep_src] + [c for c in clips if now - c.age_ref > keep_clip]
        done = set()
        for c in aged:
            freed += _delete(c, now, dry_run)
            deleted += 1
            done.add(id(c))

        orphan_count, orphan_bytes = _orphans(s, now, dry_run)
        freed += orphan_bytes

        limit = settings.MAX_DISK_GB
        usage = _storage_gb() if not dry_run else _storage_gb() - freed / GB
        if usage > limit:
            target = limit * OVER_LIMIT_TARGET
            for c in sorted((c for c in sources + clips if id(c) not in done), key=lambda c: c.age_ref):
                if usage <= target:
                    break
                freed += _delete(c, now, dry_run)
                deleted += 1
                usage -= c.size / GB
            if usage > limit:
                msg = "Disk over limit, nothing safe to delete"
                warnings.append(msg)
                if not dry_run:
                    try:
                        notify("Disk warning", f"{msg} ({usage:.1f} / {limit} GB)", level="warning")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("notify failed: %s", exc)

        pct = volume_used_pct()
        if pct >= settings.DISK_WARN_PCT:
            msg = f"Volume {pct:.0f}% full (warn at {settings.DISK_WARN_PCT:.0f}%)"
            warnings.append(msg)
            _warn_throttled(s, now, msg, dry_run)
        if dry_run:
            s.rollback()
    summary = {"files_deleted": deleted, "gb_freed": round(freed / GB, 3), "orphans": orphan_count,
               "warnings": warnings}
    log.info("cleanup%s: %s", " (dry run)" if dry_run else "", summary)
    return summary
