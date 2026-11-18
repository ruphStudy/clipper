"""Download orchestration: claim → fetch → probe → store; never raises to callers."""
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, or_, select, update

from app.config import settings
from app.db import get_session
from app.db.enums import CampaignStatus, SourceKind, SourceStatus
from app.db.models import Campaign, Source
from app.downloader import fetchers
from app.downloader.fetchers import DownloadError
from app.downloader.media import ProbeError, has_space, probe
from app.logger import get_logger
from app.notify import notify
from app.sources.links import is_drive_folder, normalize_url

log = get_logger("clipflow.downloader")

ERROR_MAX = 200
MAX_RETRIES = 3
BACKOFF = [timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6)]
PERMANENT_ERRORS = ("private/unavailable", "too long", "too large", "geo-blocked", "drive: not public",
                    "no video stream")
EXT_BY_FORMAT = (("matroska", ".mkv"), ("webm", ".webm"), ("mp4", ".mp4"), ("mov", ".mov"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short(msg: str) -> str:
    msg = str(msg).strip() or "unknown error"
    return msg if len(msg) <= ERROR_MAX else msg[: ERROR_MAX - 1] + "…"


def _claim(source_id: int, engine: Optional[Engine]) -> bool:
    """Atomically move pending → downloading; True if this caller owns the source."""
    with get_session(engine) as s:
        res = s.execute(update(Source).where(Source.id == source_id, Source.status == SourceStatus.pending)
                        .values(status=SourceStatus.downloading, error=None, updated_at=_now()))
        return res.rowcount == 1


def _finish(source_id: int, engine: Optional[Engine], **values) -> None:
    with get_session(engine) as s:
        src = s.get(Source, source_id)
        for k, v in values.items():
            setattr(src, k, v)


def _cleanup(out_base: Path) -> None:
    for p in out_base.parent.glob(f"{out_base.name}.*"):
        if p.is_file():
            p.unlink(missing_ok=True)


def _final_ext(downloaded: Path, fmt: str) -> str:
    for suffix in reversed([s.lower() for s in downloaded.suffixes]):
        if suffix in fetchers.VIDEO_EXTS:
            return suffix
    for key, ext in EXT_BY_FORMAT:
        if key in (fmt or ""):
            return ext
    return ".mp4"


def _expand_folder(src_id: int, campaign_id: int, url: str, engine: Optional[Engine]) -> str:
    files = fetchers.expand_drive_folder(url)
    with get_session(engine) as s:
        existing = set(s.scalars(select(Source.url).where(Source.campaign_id == campaign_id)))
        added = 0
        for file_url, _name in files:
            norm = normalize_url(file_url)
            if norm in existing:
                continue
            s.add(Source(campaign_id=campaign_id, url=norm, kind=SourceKind.drive, status=SourceStatus.pending))
            existing.add(norm)
            added += 1
        folder = s.get(Source, src_id)
        folder.status, folder.error = SourceStatus.processed, None
    log.info("source %d: drive folder expanded %d files (%d new)", src_id, len(files), added)
    return SourceStatus.processed.value


def _fetch(kind: SourceKind, url: str, out_base: Path) -> Path:
    if kind == SourceKind.drive:
        return fetchers.drive_fetch(url, out_base)
    if kind == SourceKind.direct:
        return fetchers.direct_fetch(url, out_base)
    return fetchers.ytdlp_fetch(url, out_base)


def classify_error(msg: Optional[str]) -> str:
    """'permanent' (retrying won't help) or 'transient' (retry with backoff)."""
    low = (msg or "").lower()
    if low.startswith("drive: not public or quota"):  # ambiguous Drive HTML page: often a temporary quota block
        return "transient"
    return "permanent" if any(k in low for k in PERMANENT_ERRORS) else "transient"


def _record_failure(source_id: int, engine: Optional[Engine], msg: str, now: datetime) -> None:
    """Mark failed and schedule a retry (transient) or stop (permanent / attempts exhausted)."""
    with get_session(engine) as s:
        src = s.get(Source, source_id)
        src.status, src.error, src.local_path = SourceStatus.failed, msg, None
        src.attempts = (src.attempts or 0) + 1
        if classify_error(msg) == "permanent" or src.attempts > MAX_RETRIES:
            src.error_permanent, src.next_retry_at = True, None
        else:
            src.error_permanent = False
            src.next_retry_at = now + BACKOFF[src.attempts - 1]


def reset_for_manual_retry(src: Source) -> None:
    """User-initiated retry: clear the automatic retry bookkeeping."""
    src.status, src.error = SourceStatus.pending, None
    src.attempts, src.error_permanent, src.next_retry_at = 0, False, None


def download_source(source_id: int, engine: Optional[Engine] = None, now: Optional[datetime] = None) -> str:
    """Download one source; returns its resulting status value. Never raises."""
    try:
        if not _claim(source_id, engine):
            with get_session(engine) as s:
                src = s.get(Source, source_id)
                return src.status.value if src else "missing"
        with get_session(engine) as s:
            src = s.get(Source, source_id)
            url, kind, campaign_id = src.url, src.kind, src.campaign_id
    except Exception as exc:  # noqa: BLE001
        log.error("source %d: claim failed: %s", source_id, exc)
        return "error"

    out_base = Path(settings.DOWNLOAD_DIR) / str(campaign_id) / str(source_id)
    try:
        if kind == SourceKind.drive and is_drive_folder(url):
            return _expand_folder(source_id, campaign_id, url, engine)
        if not has_space(settings.MAX_SOURCE_GB):
            raise DownloadError("disk full")
        out_base.parent.mkdir(parents=True, exist_ok=True)
        _cleanup(out_base)
        downloaded = _fetch(kind, url, out_base)
        try:
            info = probe(downloaded)
        except ProbeError as exc:
            raise DownloadError(f"unreadable media: {exc}") from exc
        if not info["has_video"]:
            raise DownloadError("no video stream")
        if info["duration_sec"] and info["duration_sec"] > settings.MAX_SOURCE_MINUTES * 60:
            raise DownloadError("too long")
        final = out_base.with_name(out_base.name + _final_ext(downloaded, info["format_name"]))
        tmp = final.with_name(final.name + ".part")
        if downloaded != tmp:
            shutil.move(str(downloaded), tmp)
        tmp.replace(final)
        _finish(source_id, engine, status=SourceStatus.downloaded, local_path=str(final),
                duration_sec=info["duration_sec"], error=None, error_permanent=False, next_retry_at=None,
                downloaded_at=now or _now(), file_deleted_at=None)
        log.info("source %d downloaded: %s (%.0fs)", source_id, final, info["duration_sec"] or 0)
        return SourceStatus.downloaded.value
    except Exception as exc:  # noqa: BLE001 — DownloadError or anything unexpected
        msg = _short(exc if isinstance(exc, DownloadError) else f"{type(exc).__name__}: {exc}")
        _cleanup(out_base)
        try:
            _record_failure(source_id, engine, msg, now or _now())
        except Exception as db_exc:  # noqa: BLE001
            log.error("source %d: could not record failure: %s", source_id, db_exc)
        log.warning("source %d failed: %s", source_id, msg)
        return SourceStatus.failed.value


def retry_due(engine: Optional[Engine] = None, now: Optional[datetime] = None) -> int:
    """Failed, non-permanent sources whose retry time has come go back to pending."""
    now = now or _now()
    with get_session(engine) as s:
        res = s.execute(update(Source).where(
            Source.status == SourceStatus.failed,
            or_(Source.error_permanent.is_(None), Source.error_permanent.is_(False)),
            Source.next_retry_at.is_not(None), Source.next_retry_at <= now,
        ).values(status=SourceStatus.pending, updated_at=now))
        n = res.rowcount
    if n:
        log.info("%d failed sources due for retry", n)
    return n


def download_pending(limit: int = 5, engine: Optional[Engine] = None, now: Optional[datetime] = None) -> dict:
    """Process the oldest pending sources of joined campaigns, one at a time."""
    with get_session(engine) as s:
        ids = list(s.scalars(
            select(Source.id).join(Campaign, Source.campaign_id == Campaign.id)
            .where(Source.status == SourceStatus.pending, Campaign.status == CampaignStatus.joined)
            .order_by(Source.created_at, Source.id).limit(limit)))
    summary = {"downloaded": 0, "failed": 0, "expanded": 0, "skipped": 0}
    for sid in ids:
        status = download_source(sid, engine, now)
        key = {"downloaded": "downloaded", "failed": "failed", "processed": "expanded"}.get(status, "skipped")
        summary[key] += 1
    if summary["downloaded"] or summary["failed"] or summary["expanded"]:
        try:
            notify("Downloads done", ", ".join(f"{k}: {v}" for k, v in summary.items()),
                   level="error" if summary["failed"] and not summary["downloaded"] else "success")
        except Exception as exc:  # noqa: BLE001
            log.warning("download notify failed: %s", exc)
    return summary


def reset_stuck(max_minutes: int = 120, engine: Optional[Engine] = None,
                processing_minutes: int = 180, now: Optional[datetime] = None) -> int:
    """Crash recovery: old 'downloading' → pending, old 'processing' → downloaded. Returns rows reset."""
    now = now or _now()
    with get_session(engine) as s:
        res = s.execute(update(Source).where(Source.status == SourceStatus.downloading,
                                             Source.updated_at < now - timedelta(minutes=max_minutes))
                        .values(status=SourceStatus.pending, updated_at=now))
        n_dl = res.rowcount
        res = s.execute(update(Source).where(Source.status == SourceStatus.processing,
                                             Source.updated_at < now - timedelta(minutes=processing_minutes))
                        .values(status=SourceStatus.downloaded, updated_at=now))
        n_proc = res.rowcount
    if n_dl or n_proc:
        log.info("reset stuck sources: %d downloading → pending, %d processing → downloaded", n_dl, n_proc)
    return n_dl + n_proc
