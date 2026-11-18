"""Telegram bot commands from the owner's chat: /posted, /queue, /pause, /resume, /help. Never raises."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Engine, select

from app.config import settings
from app.db import get_session
from app.db.enums import PostStatus
from app.db.models import Account, Post
from app.logger import get_logger
from app.notify.alerts import esc, get_notifier
from app.scout.scorer import utc
from app.uploader.common import get_setting, set_setting

log = get_logger("clipflow.commands")

OFFSET_KEY = "telegram_update_offset"
HELP = ("<b>ClipFlow commands</b>\n/posted &lt;id&gt; &lt;link&gt; — record a manual post\n"
        "/submitted &lt;id&gt; — mark a post submitted on Content Rewards\n/pending — live posts not submitted yet\n"
        "/queue — posts waiting for you\n/pause — stop posting\n/resume — resume posting\n/help — this list")


def _queue_text(engine: Optional[Engine], now: datetime) -> str:
    with get_session(engine) as s:
        rows = s.execute(select(Post, Account.handle).join(Account, Post.account_id == Account.id)
                         .where(Post.status == PostStatus.manual_pending).order_by(Post.notified_at, Post.id)).all()
    if not rows:
        return "✅ No manual posts waiting."
    lines = ["<b>Waiting for manual posting</b>"]
    for post, handle in rows[:30]:
        age_h = (now - (utc(post.notified_at) or now)).total_seconds() / 3600
        lines.append(f"#{post.id} {post.platform.value} @{esc(handle.lstrip('@'))} · {age_h:.0f}h")
    return "\n".join(lines)


def _pending_text(engine: Optional[Engine], now: datetime) -> str:
    from app.posting.submit import to_submit

    with get_session(engine) as s:
        rows = to_submit(s, now)
        if not rows:
            return "✅ Nothing waiting to be submitted."
        lines = ["<b>Live, not submitted yet</b>"]
        for post, age, late in rows[:30]:
            lines.append(f"#{post.id} {post.platform.value} · {age} min{' ⚠️ late' if late else ''}")
    return "\n".join(lines)


def _handle(text: str, engine: Optional[Engine], now: datetime, notifier=None) -> Optional[str]:
    from app.posting.manual import mark_posted
    from app.posting.modes import set_paused
    from app.posting.submit import mark_submitted, send_submit_alert

    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0].split("@", 1)[0].lower()
    if cmd == "/posted":
        if len(parts) < 3 or not parts[1].lstrip("#").isdigit():
            return "Usage: /posted &lt;post_id&gt; &lt;link&gt;"
        post_id = int(parts[1].lstrip("#"))
        ok, msg = mark_posted(post_id, parts[2], engine=engine, alert=False)
        if ok and send_submit_alert(post_id, engine, notifier, now):
            return ""  # handled: the submit alert itself is the reply
        return ("✅ " if ok else "❌ ") + esc(msg)
    if cmd == "/submitted":
        if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
            return "Usage: /submitted &lt;post_id&gt;"
        ok, msg = mark_submitted(int(parts[1].lstrip("#")), engine, notifier, now)
        return ("✅ " if ok else "❌ ") + esc(msg)
    if cmd == "/pending":
        return _pending_text(engine, now)
    if cmd == "/queue":
        return _queue_text(engine, now)
    if cmd == "/pause":
        set_paused(True, engine)
        return "⏸ Posting paused."
    if cmd == "/resume":
        set_paused(False, engine)
        return "▶️ Posting resumed."
    if cmd in ("/help", "/start"):
        return HELP
    return None


def _handle_callback(cb: dict, owner: str, engine: Optional[Engine], notifier, now: datetime) -> int:
    """Inline-button taps: 'sub:<id>' marks a post submitted. Only from the owner's chat."""
    from app.posting.submit import mark_submitted

    chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id"))
    if chat_id != owner:
        return 0
    data = str(cb.get("data") or "")
    if not data.startswith("sub:") or not data[4:].isdigit():
        notifier.answer_callback_query(cb.get("id", ""), "Unknown action")
        return 0
    try:
        ok, msg = mark_submitted(int(data[4:]), engine, notifier, now)
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, f"failed: {type(exc).__name__}"
    notifier.answer_callback_query(cb.get("id", ""), "Submitted ✅" if ok else msg)
    return 1


def poll_commands(engine: Optional[Engine] = None, notifier=None, now: Optional[datetime] = None) -> int:
    """Fetch new updates, run commands from TELEGRAM_CHAT_ID only; returns commands handled."""
    notifier = notifier or get_notifier()
    if not notifier.enabled:
        return 0
    now = now or datetime.now(timezone.utc)
    handled = 0
    try:
        with get_session(engine) as s:
            raw = get_setting(s, OFFSET_KEY)
        offset = int(raw) if raw and raw.isdigit() else None
        updates = notifier.get_updates(offset=offset, allowed_updates=["message", "callback_query"])
        if not updates:
            return 0
        owner = str(settings.TELEGRAM_CHAT_ID).strip()
        for upd in updates:
            cb = upd.get("callback_query")
            if cb:
                handled += _handle_callback(cb, owner, engine, notifier, now)
                continue
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != owner:
                continue
            try:
                reply = _handle(msg.get("text") or "", engine, now, notifier)
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram command failed: %s", type(exc).__name__)
                reply = "❌ command failed"
            if reply is not None:
                if reply:
                    notifier.send_message(reply)
                handled += 1
        new_offset = max(int(u.get("update_id", 0)) for u in updates) + 1
        with get_session(engine) as s:
            set_setting(s, OFFSET_KEY, str(new_offset))
    except Exception as exc:  # noqa: BLE001
        log.error("telegram command polling failed: %s", type(exc).__name__)
    return handled
