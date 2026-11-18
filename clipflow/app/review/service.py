"""Review actions on rendered clips (no web code). All functions return (ok, msg) where they can refuse."""
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.db.enums import ClipStatus
from app.db.models import Campaign, Clip, Post, Source
from app.rules import Rules, get_rules
from app.rules.caption import LIMITS, YT_TITLE_MAX, check_text

REJECT_REASONS = ("boring", "bad cut", "bad captions", "off-brief", "duplicate", "quality")
OTHER_PREFIX = "other:"
OTHER_MAX = 80
MAX_DELTA = 5.0
BODY_FIELDS = ("description", "caption", "text")


def _limit(platform: str, field: str) -> int:
    if platform == "youtube" and field == "title":
        return YT_TITLE_MAX
    return LIMITS.get(platform, 2200)


def _ok_compliance(clip: Clip) -> bool:
    return bool(clip.compliance) and bool(clip.compliance.get("ok")) and not clip.compliance.get("errors")


def review_queue(session: Session, campaign_id: Optional[int] = None) -> list[Clip]:
    """Rendered, compliance-OK clips: best campaigns first, then best moments, then oldest."""
    stmt = (select(Clip).join(Campaign, Clip.campaign_id == Campaign.id)
            .where(Clip.status == ClipStatus.rendered)
            .order_by(func.coalesce(Campaign.score, 0).desc(), func.coalesce(Clip.moment_score, 0).desc(),
                      Clip.created_at, Clip.id))
    if campaign_id is not None:
        stmt = stmt.where(Clip.campaign_id == campaign_id)
    return [c for c in session.scalars(stmt) if _ok_compliance(c)]


def validate_captions(rules: Rules, edits: dict) -> list[str]:
    """Problems with edited captions: {platform: {field: text}}."""
    problems: list[str] = []
    banned_only = Rules(banned_words=list(rules.banned_words), caption_template="{hook}")
    for platform, fields in (edits or {}).items():
        for field, text in fields.items():
            text = text or ""
            limit = _limit(platform, field)
            if len(text) > limit:
                problems.append(f"{platform} {field}: {len(text)} chars > {limit}")
            checker = rules if field in BODY_FIELDS else banned_only
            problems += [f"{platform} {field}: {v}" for v in check_text(checker, text)]
    return problems


def approve(clip_id: int, platform_captions: Optional[dict] = None,
            engine: Optional[Engine] = None) -> tuple[bool, str]:
    """Approve a rendered clip, optionally with edited per-platform captions."""
    with get_session(engine) as s:
        clip = s.get(Clip, clip_id)
        if clip is None:
            return False, f"clip #{clip_id} not found"
        if clip.status != ClipStatus.rendered:
            return False, f"clip #{clip_id} is {clip.status.value}, not rendered"
        if not _ok_compliance(clip):
            n = len((clip.compliance or {}).get("errors") or [])
            return False, f"fix errors first ({n} compliance error{'s' if n != 1 else ''})" if n \
                else "fix errors first (compliance not checked)"
        edits = {p: {f: (t or "").replace("\r\n", "\n").strip() for f, t in fields.items()}
                 for p, fields in (platform_captions or {}).items()}
        if edits:
            rules = get_rules(s.get(Campaign, clip.campaign_id))
            problems = validate_captions(rules, edits)
            if problems:
                return False, "caption problems: " + "; ".join(problems)
            current = {p: dict(v) for p, v in (clip.platform_captions or {}).items()}
            changed = False
            for p, fields in edits.items():
                target = current.setdefault(p, {"platform": p})
                for f, t in fields.items():
                    if target.get(f) != t:
                        target[f] = t
                        changed = True
            if changed:
                clip.platform_captions = current
                clip.edited = True
        now = datetime.now(timezone.utc)
        clip.status, clip.reviewed_at, clip.approved_at = ClipStatus.approved, now, now
        clip.reject_reason = None
    return True, f"Approved #{clip_id}" + (" (captions edited)" if edits else "")


def normalize_reason(reason: str) -> Optional[str]:
    """A valid reject reason or None."""
    reason = (reason or "").strip()
    if reason.lower() in REJECT_REASONS:
        return reason.lower()
    if reason.lower().startswith(OTHER_PREFIX):
        text = reason[len(OTHER_PREFIX):].strip()
        if text:
            return f"other: {text[:OTHER_MAX]}"
    return None


def reject(clip_id: int, reason: str, engine: Optional[Engine] = None) -> tuple[bool, str]:
    """Reject with a reason from REJECT_REASONS or 'other: <text>'."""
    valid = normalize_reason(reason)
    if valid is None:
        return False, f"invalid reason; use one of: {', '.join(REJECT_REASONS)}, or 'other: <text>'"
    with get_session(engine) as s:
        clip = s.get(Clip, clip_id)
        if clip is None:
            return False, f"clip #{clip_id} not found"
        if clip.status not in (ClipStatus.rendered, ClipStatus.approved):
            return False, f"clip #{clip_id} is {clip.status.value}; cannot reject"
        clip.status, clip.reject_reason = ClipStatus.rejected, valid
        clip.reviewed_at, clip.approved_at = datetime.now(timezone.utc), None
    return True, f"Rejected #{clip_id} ({valid})"


def rerender(clip_id: int, hook: Optional[str] = None, start_delta: float = 0, end_delta: float = 0,
             engine: Optional[Engine] = None) -> tuple[bool, str]:
    """Adjust hook/cut and send the clip back to the renderer (the caller starts render_clip)."""
    if not (-MAX_DELTA <= start_delta <= MAX_DELTA and -MAX_DELTA <= end_delta <= MAX_DELTA):
        return False, f"adjustments must be within ±{MAX_DELTA:g}s"
    with get_session(engine) as s:
        clip = s.get(Clip, clip_id)
        if clip is None:
            return False, f"clip #{clip_id} not found"
        if clip.status not in (ClipStatus.rendered, ClipStatus.rejected):
            return False, f"clip #{clip_id} is {clip.status.value}; only rendered/rejected clips can be re-rendered"
        rules = get_rules(s.get(Campaign, clip.campaign_id))
        source = s.get(Source, clip.source_id)
        start, end = round(clip.start_sec + start_delta, 2), round(clip.end_sec + end_delta, 2)
        if start < 0:
            return False, "start would be before the beginning of the source"
        if source is not None and source.duration_sec and end > source.duration_sec:
            return False, "end would be past the end of the source"
        length = end - start
        if not rules.clip_min_sec <= length <= rules.clip_max_sec:
            return False, f"length {length:.1f}s outside {rules.clip_min_sec}-{rules.clip_max_sec}s"
        for p in (clip.file_path, clip.thumb_path):
            if p:
                Path(p).unlink(missing_ok=True)
        if hook is not None and hook.strip():
            clip.hook_title = hook.strip()[:120]
        clip.start_sec, clip.end_sec = start, end
        clip.status, clip.render_attempts, clip.compliance, clip.compliance_at = ClipStatus.candidate, 0, None, None
        clip.file_path = clip.thumb_path = None
        clip.reviewed_at = clip.approved_at = clip.reject_reason = None
        clip.edited, clip.platform_captions = False, None
    return True, f"Re-rendering #{clip_id}"


def undo(clip_id: int, engine: Optional[Engine] = None) -> tuple[bool, str]:
    """approved/rejected → rendered, only if the clip was never scheduled or posted."""
    with get_session(engine) as s:
        clip = s.get(Clip, clip_id)
        if clip is None:
            return False, f"clip #{clip_id} not found"
        if clip.status not in (ClipStatus.approved, ClipStatus.rejected):
            return False, f"clip #{clip_id} is {clip.status.value}; nothing to undo"
        if s.scalar(select(func.count()).select_from(Post).where(Post.clip_id == clip_id)):
            return False, f"clip #{clip_id} was already scheduled/posted"
        clip.status = ClipStatus.rendered
        clip.reviewed_at = clip.approved_at = clip.reject_reason = None
    return True, f"Undid review of #{clip_id}"


def _today_start() -> datetime:
    tz = ZoneInfo(settings.TIMEZONE)
    return datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz).astimezone(timezone.utc)


def stats_today(engine: Optional[Engine] = None) -> dict:
    """Approved/rejected today (TIMEZONE) and clips waiting for review."""
    start = _today_start()
    with get_session(engine) as s:
        approved = s.scalar(select(func.count()).select_from(Clip).where(Clip.approved_at >= start)) or 0
        rejected = s.scalar(select(func.count()).select_from(Clip).where(
            Clip.status == ClipStatus.rejected, Clip.reviewed_at >= start)) or 0
        waiting = len(review_queue(s))
    return {"approved": approved, "rejected": rejected, "waiting": waiting}
