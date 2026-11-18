"""Content Rewards submission: alert when a post goes live, deadlines, mark submitted.

There is no public submit API, so submission is a human tap: we make it fast (one alert with the link + buttons)
and keep reminding until it is done or the 30-minute window has passed. Never raises.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select

from app.config import settings
from app.db import get_session
from app.db.enums import PostStatus
from app.db.models import Account, Campaign, Clip, Post
from app.logger import get_logger
from app.notify import esc, get_notifier
from app.scout.scorer import utc

log = get_logger("clipflow.submit")


def keyboard(post_id: int, campaign_url: Optional[str]) -> dict:
    rows = []
    if campaign_url:
        rows.append([{"text": "📋 Open campaign", "url": campaign_url}])
    rows.append([{"text": "✅ Submitted", "callback_data": f"sub:{post_id}"}])
    return {"inline_keyboard": rows}


def _context(session, post: Post) -> tuple[str, str, str]:
    clip = session.get(Clip, post.clip_id)
    campaign = session.get(Campaign, clip.campaign_id) if clip else None
    account = session.get(Account, post.account_id)
    return (campaign.name if campaign else "campaign", campaign.url if campaign else "",
            (account.handle if account else "?").lstrip("@"))


def send_submit_alert(post_id: int, engine: Optional[Engine] = None, notifier=None,
                      now: Optional[datetime] = None) -> bool:
    """Alert the user that a post is live and must be submitted; returns True if Telegram got it. Never raises."""
    try:
        notifier = notifier or get_notifier()
        now = now or datetime.now(timezone.utc)
        with get_session(engine) as s:
            post = s.get(Post, post_id)
            if post is None or post.status != PostStatus.posted:
                return False
            name, url, handle = _context(s, post)
            posted = utc(post.posted_at) or now
            platform, link = post.platform.value, post.post_url or ""
        deadline = (posted + timedelta(minutes=settings.SUBMIT_DEADLINE_MIN)).astimezone(ZoneInfo(settings.TIMEZONE))
        text = (f"🚀 <b>LIVE — submit within {settings.SUBMIT_DEADLINE_MIN} min</b> (by {deadline:%H:%M})\n"
                f"{esc(name)} · {esc(platform)} @{esc(handle)}\n<code>{esc(link)}</code>")
        sent = bool(notifier.enabled) and notifier.send_message(text, reply_markup=keyboard(post_id, url))
        message_id = getattr(notifier, "last_message_id", None) if sent else None
        with get_session(engine) as s:
            post = s.get(Post, post_id)
            post.submit_alert_at = now
            if message_id:
                post.alert_message_id = int(message_id)
        return sent
    except Exception as exc:  # noqa: BLE001
        log.warning("submit alert for post %d failed: %s", post_id, type(exc).__name__)
        return False


def mark_submitted(post_id: int, engine: Optional[Engine] = None, notifier=None,
                   now: Optional[datetime] = None) -> tuple[bool, str]:
    """posted → submitted; says whether it was late; edits the original alert."""
    now = now or datetime.now(timezone.utc)
    with get_session(engine) as s:
        post = s.get(Post, post_id)
        if post is None:
            return False, f"post #{post_id} not found"
        if post.status == PostStatus.submitted:
            return False, f"post #{post_id} is already submitted"
        if post.status != PostStatus.posted:
            return False, f"post #{post_id} is {post.status.value}, not posted"
        posted = utc(post.posted_at) or now
        minutes = max(0, round((now - posted).total_seconds() / 60))
        late = minutes > settings.SUBMIT_DEADLINE_MIN
        post.status, post.submitted_at = PostStatus.submitted, now
        message_id = post.alert_message_id
    msg = f"Submitted #{post_id} in {minutes} min" + (f" (late: over {settings.SUBMIT_DEADLINE_MIN} min)" if late else "")
    if message_id:
        try:
            notifier = notifier or get_notifier()
            notifier.edit_message_text(message_id, f"✅ Submitted #{post_id} (in {minutes} min)"
                                       + (" — late" if late else ""))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not edit alert for post %d: %s", post_id, type(exc).__name__)
    return True, msg


def check_submit_deadlines(now: Optional[datetime] = None, engine: Optional[Engine] = None, notifier=None) -> dict:
    """One reminder at SUBMIT_REMIND_MIN, one 'window passed' at SUBMIT_DEADLINE_MIN, per alerted post."""
    now = now or datetime.now(timezone.utc)
    notifier = notifier or get_notifier()
    remind, deadline = timedelta(minutes=settings.SUBMIT_REMIND_MIN), timedelta(minutes=settings.SUBMIT_DEADLINE_MIN)
    out = {"reminded": 0, "missed": 0}
    with get_session(engine) as s:
        pending = s.scalars(select(Post).where(Post.status == PostStatus.posted, Post.submitted_at.is_(None),
                                               Post.submit_alert_at.is_not(None))).all()
        for post in pending:
            posted = utc(post.posted_at) or utc(post.submit_alert_at)
            age = now - posted
            _name, url, _handle = _context(s, post)
            text = None
            if age >= deadline and not post.submit_missed:
                post.submit_missed = post.submit_reminded = True
                text = f"⚠️ Window passed for #{post.id}; submit anyway, it may still be accepted"
                out["missed"] += 1
            elif remind <= age < deadline and not post.submit_reminded:
                post.submit_reminded = True
                left = max(1, round((deadline - age).total_seconds() / 60))
                text = f"⏰ {left} min left to submit #{post.id}"
                out["reminded"] += 1
            if text and notifier.enabled:
                try:
                    notifier.send_message(text, reply_markup=keyboard(post.id, url))
                except Exception as exc:  # noqa: BLE001
                    log.warning("deadline message failed: %s", type(exc).__name__)
    return out


def to_submit(session, now: Optional[datetime] = None) -> list[tuple[Post, int, bool]]:
    """[(post, minutes since posted, late)] for posted-not-submitted posts, oldest first."""
    now = now or datetime.now(timezone.utc)
    rows = session.scalars(select(Post).where(Post.status == PostStatus.posted, Post.submitted_at.is_(None))
                           .order_by(Post.posted_at, Post.id)).all()
    out = []
    for p in rows:
        age = int((now - (utc(p.posted_at) or now)).total_seconds() // 60)
        out.append((p, age, age >= settings.SUBMIT_DEADLINE_MIN))
    return out
