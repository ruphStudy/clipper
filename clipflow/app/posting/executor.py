"""Run due posts: API uploads with retries, manual hand-offs, manual follow-ups. Never raises."""
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import Engine, select

from app.config import settings
from app.db import get_session
from app.db.enums import ClipStatus, PostStatus
from app.db.models import Clip, Post
from app.logger import get_logger
from app.notify import get_notifier, notify, notify_error
from app.posting.manual import send_manual_handoff
from app.posting.modes import YT_OVERRIDE_KEY, in_submit_window, is_paused
from app.posting.planner import next_valid_slot
from app.scout.scorer import utc
from app.uploader.common import get_setting, set_setting

log = get_logger("clipflow.executor")

MAX_PER_RUN = 5
MAX_RETRIES = 2
RETRY_DELAY = timedelta(minutes=60)
PERMANENT_HINTS = ("auth", "compliance", "file missing", "not approved", "no such", "inactive", "reauthorize",
                   "reconnect", "not found")


def _uploaders() -> dict[str, Callable]:
    from app.uploader.instagram import upload_instagram
    from app.uploader.youtube import upload_youtube
    return {"youtube": upload_youtube, "instagram": upload_instagram}


def is_permanent(msg: str) -> bool:
    low = (msg or "").lower()
    return any(h in low for h in PERMANENT_HINTS)


def _youtube_locked(engine: Optional[Engine]) -> None:
    """Future YouTube posts go manual; tell the user once."""
    with get_session(engine) as s:
        if get_setting(s, YT_OVERRIDE_KEY) == "manual":
            return
        set_setting(s, YT_OVERRIDE_KEY, "manual")
    try:
        notify("YouTube switched to manual posting",
               "API uploads are locked to private (unaudited project), so new YouTube posts will be sent to you to "
               "post by hand. Clear app setting youtube_mode_override after the audit is approved.", level="warning")
    except Exception as exc:  # noqa: BLE001
        log.warning("override notify failed: %s", type(exc).__name__)


def _run_api(post_id: int, platform: str, clip_id: int, account_id: int, now: datetime,
             uploaders: dict[str, Callable], engine: Optional[Engine]) -> str:
    uploader = uploaders.get(platform)
    if uploader is None:
        result, msg = None, f"no uploader for {platform}"
    else:
        try:
            result, msg = uploader(clip_id, account_id, engine=engine)
        except Exception as exc:  # noqa: BLE001 — uploaders shouldn't raise, but never let one stop the run
            result, msg = None, f"uploader crashed: {type(exc).__name__}"
    if result is not None and result.status == PostStatus.posted:
        if platform == "youtube" and result.visibility == "private_locked":
            _youtube_locked(engine)
        return "posted"
    with get_session(engine) as s:
        post = s.get(Post, post_id)
        error = (result.error if result is not None and result.error else msg) or "upload failed"
        attempts = post.attempts or 0
        if attempts < MAX_RETRIES and not is_permanent(error):
            post.attempts = attempts + 1
            post.status, post.scheduled_at, post.error = PostStatus.scheduled, now + RETRY_DELAY, error[:200]
            log.warning("post %d failed (%s); retry %d at +60 min", post_id, error, post.attempts)
            return "retry"
        post.status, post.error = PostStatus.failed, error[:200]
    try:
        notify_error(f"post #{post_id} ({platform})", RuntimeError(error))
    except Exception:  # noqa: BLE001
        pass
    return "failed"


def _followups(now: datetime, engine: Optional[Engine], notifier) -> dict:
    remind_at, expire_at = timedelta(hours=settings.MANUAL_REMIND_HOURS), timedelta(hours=settings.MANUAL_EXPIRE_HOURS)
    out = {"reminded": 0, "expired": 0}
    with get_session(engine) as s:
        pending = s.scalars(select(Post).where(Post.status == PostStatus.manual_pending)).all()
        for post in pending:
            age = now - (utc(post.notified_at) or utc(post.scheduled_at) or now)
            if age >= expire_at:
                post.status, post.error = PostStatus.failed, "manual not posted"
                other_posted = s.scalar(select(Post.id).where(Post.clip_id == post.clip_id, Post.id != post.id,
                                                             Post.status.in_([PostStatus.posted,
                                                                              PostStatus.submitted])))
                clip = s.get(Clip, post.clip_id)
                if clip is not None and other_posted is None and clip.status in (ClipStatus.scheduled,
                                                                                ClipStatus.approved):
                    clip.status = ClipStatus.approved
                out["expired"] += 1
            elif age >= remind_at and not post.reminder_sent:
                post.reminder_sent = True
                out["reminded"] += 1
                if notifier.enabled:
                    notifier.send_message(f"⏰ Reminder: post #{post.id} ({post.platform.value}) is still waiting. "
                                          f"After posting reply: /posted {post.id} &lt;link&gt;")
    return out


def execute_due(now: Optional[datetime] = None, engine: Optional[Engine] = None,
                uploaders: Optional[dict[str, Callable]] = None, notifier=None) -> dict:
    """Process due posts (max 5) and manual follow-ups."""
    now = utc(now) or datetime.now(timezone.utc)
    notifier = notifier or get_notifier()
    summary = {"posted": 0, "retry": 0, "failed": 0, "manual": 0, "reminded": 0, "expired": 0, "rescheduled": 0,
               "paused": False}
    with get_session(engine) as s:
        if is_paused(s):
            summary["paused"] = True
            return summary
        due = [(p.id, p.platform.value, p.clip_id, p.account_id, p.mode or "api") for p in s.scalars(
            select(Post).where(Post.status == PostStatus.scheduled, Post.scheduled_at <= now)
            .order_by(Post.scheduled_at, Post.id).limit(MAX_PER_RUN))]
    uploaders = uploaders if uploaders is not None else _uploaders()
    if due and not in_submit_window(now):
        # e.g. the machine slept past the slot: going live now would leave no time to submit
        with get_session(engine) as s:
            for post_id, platform, clip_id, account_id, _mode in due:
                s.get(Post, post_id).scheduled_at = next_valid_slot(platform, clip_id, account_id, now)
        summary["rescheduled"] = len(due)
        log.info("now is outside SUBMIT_WINDOW: rescheduled %d due posts", len(due))
        due = []
    for post_id, platform, clip_id, account_id, mode in due:
        try:
            if mode == "manual":
                send_manual_handoff(post_id, engine, notifier)
                summary["manual"] += 1
            else:
                summary[_run_api(post_id, platform, clip_id, account_id, now, uploaders, engine)] += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("post %d execution crashed", post_id)
            summary["failed"] += 1
            try:
                notify_error(f"post #{post_id}", exc)
            except Exception:  # noqa: BLE001
                pass
    try:
        summary.update(_followups(now, engine, notifier))
    except Exception as exc:  # noqa: BLE001
        log.exception("manual follow-ups crashed: %s", type(exc).__name__)
    return summary
