"""Application settings loaded from environment / .env."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """ClipFlow configuration."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    APP_ENV: str = "dev"
    LOG_LEVEL: str = "INFO"
    TIMEZONE: str = "Asia/Kolkata"

    DATA_DIR: str = "data"
    DB_PATH: str = "data/clipflow.db"
    DOWNLOAD_DIR: str = "data/downloads"
    CLIPS_DIR: str = "data/clips"
    LOG_DIR: str = "data/logs"

    GEMINI_API_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    DASHBOARD_USER: str = "admin"
    DASHBOARD_PASSWORD: str = ""

    YOUTUBE_CLIENT_SECRETS_PATH: str = ""
    INSTAGRAM_ACCESS_TOKEN: str = ""
    INSTAGRAM_USER_ID: str = ""

    MAX_DISK_GB: int = 20


settings = Settings()


def ensure_dirs() -> None:
    """Create runtime directories if missing."""
    for d in (settings.DATA_DIR, settings.DOWNLOAD_DIR, settings.CLIPS_DIR, settings.LOG_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)


def masked(value: str) -> str:
    """Return '' if empty, else '****' plus the last 4 characters."""
    return "" if not value else "****" + value[-4:]
