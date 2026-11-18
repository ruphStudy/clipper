"""Test safety: never talk to the real Telegram bot, whatever is in .env."""
import pytest

from app import config
from app.notify import alerts


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    monkeypatch.setattr(config.settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(alerts, "_notifier", None)
    yield
