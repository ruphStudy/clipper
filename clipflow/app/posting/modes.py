"""Posting configuration helpers: per-platform mode and slots, pause flag."""
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.config import csv_list, settings
from app.db import get_session
from app.uploader.common import get_setting, set_setting

PAUSED_KEY = "posting_paused"
YT_OVERRIDE_KEY = "youtube_mode_override"
API_PLATFORMS = ("youtube", "instagram")  # platforms with an uploader


def _mode_map(value: Optional[str] = None) -> dict[str, str]:
    out = {}
    for item in csv_list(settings.POST_MODE if value is None else value):
        name, _, mode = item.partition(":")
        out[name.strip()] = mode.strip() or "manual"
    return out


def post_mode(platform: str, session: Optional[Session] = None) -> str:
    """'api' or 'manual' for a platform (auto → api only if the YouTube API project is audited)."""
    platform = platform.lower()
    if platform == "youtube" and session is not None and get_setting(session, YT_OVERRIDE_KEY) == "manual":
        return "manual"
    mode = _mode_map().get(platform, "manual")
    if mode == "auto":
        mode = "api" if settings.YOUTUBE_API_AUDITED else "manual"
    if mode == "api" and platform not in API_PLATFORMS:
        return "manual"
    return "api" if mode == "api" else "manual"


def slots(platform: str, value: Optional[str] = None) -> list[time]:
    """Configured posting times for a platform, sorted."""
    raw = settings.POSTING_SLOTS if value is None else value
    for block in raw.split(";"):
        name, _, times = block.strip().partition(":")
        if name.strip().lower() != platform.lower():
            continue
        out = []
        for t in times.split(","):
            hh, _, mm = t.strip().partition(":")
            try:
                out.append(time(int(hh), int(mm or 0)))
            except ValueError:
                continue
        return sorted(set(out))
    return []


def is_paused(session: Session) -> bool:
    return (get_setting(session, PAUSED_KEY) or "").lower() == "true"


def set_paused(paused: bool, engine: Optional[Engine] = None) -> None:
    with get_session(engine) as s:
        set_setting(s, PAUSED_KEY, "true" if paused else "false")


SUBMIT_MARGIN = timedelta(minutes=15)  # the user needs time to submit before the window closes


def submit_window(value: Optional[str] = None) -> tuple[time, time]:
    """(start, end) local times from SUBMIT_WINDOW 'HH:MM-HH:MM' (invalid → whole day)."""
    raw = (settings.SUBMIT_WINDOW if value is None else value) or ""
    try:
        a, b = raw.split("-", 1)
        start = time(*map(int, a.strip().split(":")))
        end = time(*map(int, b.strip().split(":")))
        if start < end:
            return start, end
    except ValueError:
        pass
    return time(0, 0), time(23, 59, 59)


def in_submit_window(at: datetime, margin: timedelta = SUBMIT_MARGIN) -> bool:
    """True if `at` and `at + margin` both fall inside the local submit window on the same day."""
    tz = ZoneInfo(settings.TIMEZONE)
    local = at.astimezone(tz)
    start, end = submit_window()
    day_start = datetime.combine(local.date(), start, tzinfo=tz)
    day_end = datetime.combine(local.date(), end, tzinfo=tz)
    return day_start <= local and local + margin <= day_end
