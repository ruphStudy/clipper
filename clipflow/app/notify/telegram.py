"""Minimal Telegram Bot API notifier over httpx (outgoing only)."""
import logging
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import masked, settings
from app.logger import get_logger

log = get_logger("clipflow.telegram")
# httpx logs full request URLs (which contain the bot token) at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org/bot{token}/"
MAX_CHUNK = 4000
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER = 30
TIMEOUT = 15.0


def split_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split text into chunks of at most `limit` chars on line boundaries."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current or not chunks:
        chunks.append(current)
    return [c.rstrip("\n") or c for c in chunks]


class TelegramNotifier:
    """Sends messages and documents to one Telegram chat."""

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.token = settings.TELEGRAM_BOT_TOKEN if token is None else token
        self.chat_id = settings.TELEGRAM_CHAT_ID if chat_id is None else chat_id
        self.base_url = API_BASE.format(token=self.token)
        self.client = client or httpx.Client(timeout=TIMEOUT)
        self._warned_disabled = False

    @property
    def enabled(self) -> bool:
        """True only if both token and chat id are set."""
        return bool(self.token and self.chat_id)

    def _safe(self, text: object) -> str:
        """Mask the bot token in any text."""
        s = str(text)
        return s.replace(self.token, masked(self.token)) if self.token else s

    def _check_enabled(self) -> bool:
        """Warn once and return False if disabled."""
        if self.enabled:
            return True
        if not self._warned_disabled:
            log.warning("telegram disabled")
            self._warned_disabled = True
        return False

    def _call(self, method: str, **kwargs) -> Optional[dict]:
        """POST to a Bot API method with 429 retry; returns JSON result or None."""
        url = self.base_url + method
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self.client.post(url, timeout=TIMEOUT, **kwargs)
            except httpx.HTTPError as exc:
                log.error("telegram %s network error: %s", method, self._safe(exc))
                return None
            if resp.status_code == 429:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 1)
                except ValueError:
                    retry_after = 1
                wait = min(float(retry_after), MAX_RETRY_AFTER)
                log.warning("telegram %s rate limited, retry in %ss (attempt %d)", method, wait, attempt)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(wait)
                    continue
                log.error("telegram %s failed: rate limited after %d attempts", method, MAX_ATTEMPTS)
                return None
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code != 200 or not data.get("ok"):
                desc = data.get("description") or resp.text[:200]
                log.error("telegram %s failed: HTTP %s %s", method, resp.status_code, self._safe(desc))
                return None
            return data
        return None

    def send_message(self, text: str, parse_mode: str = "HTML", silent: bool = False) -> bool:
        """Send text (auto-chunked); never raises."""
        if not self._check_enabled():
            return False
        try:
            for chunk in split_text(text):
                payload = {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                    "disable_notification": silent,
                }
                if self._call("sendMessage", json=payload) is None:
                    return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("telegram send_message error: %s", self._safe(exc))
            return False

    def send_document(self, file_path: str, caption: str = "") -> bool:
        """Upload a file as a document; never raises."""
        if not self._check_enabled():
            return False
        try:
            path = Path(file_path)
            with path.open("rb") as fh:
                data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "HTML"}
                files = {"document": (path.name, fh)}
                return self._call("sendDocument", data=data, files=files) is not None
        except Exception as exc:  # noqa: BLE001
            log.error("telegram send_document error: %s", self._safe(exc))
            return False

    def get_updates(self) -> list[dict]:
        """Return recent updates (used to discover chat ids); needs only the token."""
        if not self.token:
            log.warning("telegram token missing")
            return []
        data = self._call("getUpdates")
        return list(data.get("result", [])) if data else []
