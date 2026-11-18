"""macOS power helpers: keep the Mac awake while the scheduler runs, and warn when it is on battery."""
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import Engine

from app.config import settings
from app.db import get_session
from app.logger import get_logger
from app.scout.scorer import utc
from app.uploader.common import get_setting, set_setting

log = get_logger("clipflow.power")

CHECK_EVERY = timedelta(minutes=10)
BATTERY_WARN_EVERY = timedelta(hours=1)
LOW_BATTERY = 20
CHECKED_KEY = "power_checked_at"
WARNED_KEY = "power_battery_warned_at"
LOW_KEY = "power_low_notified"


def start_keep_awake(pid: int, platform: Optional[str] = None, popen: Callable = subprocess.Popen):
    """On macOS (KEEP_AWAKE=true) run `caffeinate -i -s -w <pid>`; it exits with the scheduler. Returns the process."""
    if (platform or sys.platform) != "darwin" or not settings.KEEP_AWAKE:
        return None
    try:
        proc = popen(["caffeinate", "-i", "-s", "-w", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.warning("keep-awake not started: %s", exc)
        return None
    log.info("keep-awake on: caffeinate -i -s -w %d (no idle/system sleep while on AC power)", pid)
    return proc


def parse_pmset(text: str) -> Optional[dict]:
    """`pmset -g batt` → {'ac': bool, 'percent': int|None}; None if unreadable."""
    if not text:
        return None
    source = re.search(r"Now drawing from '([^']+)'", text)
    if not source:
        return None
    pct = re.search(r"(\d{1,3})%", text)
    return {"ac": "AC Power" in source.group(1), "percent": int(pct.group(1)) if pct else None}


def read_pmset(runner: Optional[Callable[[], str]] = None) -> Optional[dict]:
    try:
        text = runner() if runner else subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                                                      timeout=10).stdout
        return parse_pmset(text)
    except Exception as exc:  # noqa: BLE001
        log.debug("pmset failed: %s", type(exc).__name__)
        return None


def check_power(now: Optional[datetime] = None, engine: Optional[Engine] = None,
                runner: Optional[Callable[[], str]] = None, notify_fn: Optional[Callable] = None,
                platform: Optional[str] = None, force: bool = False) -> Optional[dict]:
    """Every 10 min: battery → warning (≤1/hour); <20% → one error until back on AC. Never raises."""
    if (platform or sys.platform) != "darwin":
        return None
    now = now or datetime.now(timezone.utc)
    try:
        if notify_fn is None:
            from app.notify import notify as notify_fn
        with get_session(engine) as s:
            last = get_setting(s, CHECKED_KEY)
            if not force and last and now - utc(datetime.fromisoformat(last)) < CHECK_EVERY:
                return None
            set_setting(s, CHECKED_KEY, now.isoformat())
        state = read_pmset(runner)
        if state is None:
            return None
        with get_session(engine) as s:
            if state["ac"]:
                set_setting(s, LOW_KEY, "")
                return state
            warned = get_setting(s, WARNED_KEY)
            if not warned or now - utc(datetime.fromisoformat(warned)) >= BATTERY_WARN_EVERY:
                set_setting(s, WARNED_KEY, now.isoformat())
                notify_fn("Mac on battery", "Mac on battery: plug in or ClipFlow may sleep", level="warning")
            pct = state["percent"]
            if pct is not None and pct < LOW_BATTERY and not get_setting(s, LOW_KEY):
                set_setting(s, LOW_KEY, now.isoformat())
                notify_fn("Battery low", f"Mac battery at {pct}% — ClipFlow will stop when it sleeps", level="error")
        return state
    except Exception as exc:  # noqa: BLE001
        log.warning("power check failed: %s", type(exc).__name__)
        return None
