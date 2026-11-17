"""High-level alert helpers on a lazy module-level notifier."""
import html
from typing import Optional

from app.notify.telegram import TelegramNotifier

EMOJI = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}
_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """Return the shared notifier, creating it on first use."""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


def esc(text: object) -> str:
    """HTML-escape dynamic text."""
    return html.escape(str(text), quote=False)


def notify(title: str, body: str = "", level: str = "info") -> bool:
    """Send a formatted alert; returns True if sent."""
    emoji = EMOJI.get(level, EMOJI["info"])
    text = f"{emoji} <b>{esc(title)}</b>"
    if body:
        text += f"\n{esc(body)}"
    return get_notifier().send_message(text)


def notify_error(where: str, exc: BaseException) -> bool:
    """Send an error alert with exception type and short message."""
    msg = str(exc)
    if len(msg) > 300:
        msg = msg[:297] + "..."
    return notify(f"Error in {where}", f"{type(exc).__name__}: {msg}", level="error")
