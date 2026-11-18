"""Application settings loaded from environment / .env."""
import os
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
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_GRAPH_VERSION: str = "v26.0"
    INSTAGRAM_DAILY_CAP: int = 20
    INSTAGRAM_SHARE_TO_FEED: bool = True
    POST_MODE: str = "youtube:auto,instagram:api,tiktok:manual,x:manual"
    POSTING_SLOTS: str = ("youtube:10:30,14:30,19:30,22:00;instagram:11:00,18:00,21:30;"
                          "tiktok:12:00,17:30,20:30,23:00")
    SLOT_JITTER_MIN: int = 12
    MIN_GAP_MIN: int = 90
    PLAN_DAYS_AHEAD: int = 2
    MANUAL_REMIND_HOURS: float = 12
    MANUAL_EXPIRE_HOURS: float = 48
    SUBMIT_WINDOW: str = "09:00-23:30"  # local times when posts may go live (the user must submit within 30 min)
    SUBMIT_DEADLINE_MIN: int = 30
    SUBMIT_REMIND_MIN: int = 15
    YOUTUBE_API_KEY: str = ""
    TRACK_DAYS: int = 30
    AUTO_TYPICAL_VIEWS: bool = True
    VIEW_MILESTONES: str = "10000,50000,100000,500000"
    KEEP_AWAKE: bool = True  # macOS: run caffeinate while the scheduler runs

    APIFY_TOKEN: str = ""
    APIFY_ACTOR: str = "fayoussef~whop-clipping-campaigns-scraper"

    MY_PLATFORMS: str = "youtube,instagram"
    MY_NICHES: str = ""
    MY_CAMPAIGN_TYPES: str = "clipping"
    TYPICAL_VIEWS: int = 3000
    MIN_CPM: float = 0.3
    MIN_BUDGET_LEFT: float = 500
    MIN_DAYS_LEFT: float = 3

    MAX_DISK_GB: int = 20
    MAX_SOURCE_MINUTES: int = 240
    MAX_SOURCE_GB: float = 4
    YTDLP_COOKIES_FILE: str = ""
    DOWNLOAD_MAX_HEIGHT: int = 1080
    KEEP_SOURCE_DAYS: float = 3
    KEEP_CLIP_DAYS: float = 7
    DISK_WARN_PCT: float = 85
    TRANSCRIBE_BACKEND: str = "local"  # local | openai
    WHISPER_MODEL: str = "small"
    WHISPER_COMPUTE: str = "int8"
    TRANSCRIPTS_DIR: str = "data/transcripts"
    OPENAI_API_KEY: str = ""
    OPENAI_TRANSCRIBE_MODEL: str = "whisper-1"
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_PRICE_IN: float = 0.30  # $ per 1M input tokens (estimates only)
    GEMINI_PRICE_OUT: float = 2.50  # $ per 1M output tokens
    AI_MONTHLY_BUDGET_USD: float = 5
    MOMENTS_PER_SOURCE: int = 8
    MOMENTS_WINDOW_MIN: float = 45
    RENDER_WIDTH: int = 1080
    RENDER_HEIGHT: int = 1920
    RENDER_FPS: int = 30
    RENDER_CRF: int = 21
    RENDER_LAYOUT: str = "auto"  # auto | crop | pad
    RENDER_PER_RUN: int = 4
    CAPTION_FONT: str = "Anton"
    CAPTION_WORDS_PER_LINE: int = 3
    PLATFORM_MAX_SEC: str = "youtube:180,instagram:90,tiktok:600,x:140"
    MAX_CLIP_MB: float = 200
    DASHBOARD_PUBLIC_URL: str = ""
    YOUTUBE_PRIVACY: str = "public"  # public | unlisted | private
    YOUTUBE_CATEGORY_ID: str = "24"
    YOUTUBE_DAILY_UPLOAD_CAP: int = 90
    YOUTUBE_API_AUDITED: bool = False
    SECRETS_DIR: str = "data/secrets"


settings = Settings()


def ensure_dirs() -> None:
    """Create runtime directories if missing."""
    for d in (settings.DATA_DIR, settings.DOWNLOAD_DIR, settings.CLIPS_DIR, settings.LOG_DIR,
              settings.TRANSCRIPTS_DIR, settings.SECRETS_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(settings.SECRETS_DIR, 0o700)  # token files live here
    except OSError:
        pass


def csv_list(value: str) -> list[str]:
    """Split a comma list into lowercase, trimmed, non-empty items."""
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


def platform_max_sec(value: str | None = None) -> dict[str, float]:
    """Parse 'youtube:180,instagram:90' into {'youtube': 180.0, ...} (bad entries ignored)."""
    out: dict[str, float] = {}
    for item in csv_list(settings.PLATFORM_MAX_SEC if value is None else value):
        name, _, sec = item.partition(":")
        try:
            out[name.strip()] = float(sec)
        except ValueError:
            continue
    return out


def masked(value: str) -> str:
    """Return '' if empty, else '****' plus the last 4 characters."""
    return "" if not value else "****" + value[-4:]
