"""Single-instance guard for the scheduler: a DB heartbeat {host, pid, at} refreshed every minute."""
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine

from app.config import settings
from app.db import get_session
from app.logger import get_logger
from app.scout.scorer import utc
from app.uploader.common import get_setting, set_setting

log = get_logger("clipflow.lock")

HEARTBEAT_KEY = "scheduler_heartbeat"
FRESH = timedelta(minutes=3)


def hostname() -> str:
    return socket.gethostname()


def pid_alive(pid: int) -> bool:
    """True if a process with this pid exists on this machine."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except OSError:
        return False
    return True


def read_heartbeat(engine: Optional[Engine] = None) -> Optional[dict]:
    with get_session(engine) as s:
        raw = get_setting(s, HEARTBEAT_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def check_can_start(force: bool = False, engine: Optional[Engine] = None, now: Optional[datetime] = None,
                    host: Optional[str] = None, pid: Optional[int] = None) -> tuple[bool, str]:
    """(ok, reason): refuse when another live scheduler wrote a heartbeat in the last 3 minutes."""
    now = now or datetime.now(timezone.utc)
    host, pid = host or hostname(), pid or os.getpid()
    hb = read_heartbeat(engine)
    if force or not hb or not hb.get("at"):
        return True, ""
    try:
        at = utc(datetime.fromisoformat(hb["at"]))
    except ValueError:
        return True, ""
    other_host, other_pid = hb.get("host"), int(hb.get("pid") or 0)
    if (other_host, other_pid) == (host, pid) or now - at > FRESH:
        return True, ""
    if other_host == host and not pid_alive(other_pid):
        return True, ""
    age = int((now - at).total_seconds())
    return False, (f"another ClipFlow scheduler is running (pid {other_pid} on {other_host}, heartbeat {age}s ago). "
                   "Stop it first (scripts/mac/uninstall.sh or Ctrl+C in its terminal), or use --force.")


def write_heartbeat(engine: Optional[Engine] = None, now: Optional[datetime] = None,
                    host: Optional[str] = None, pid: Optional[int] = None) -> None:
    """Store {host, pid, at} and data/heartbeat.txt (ISO time)."""
    now = now or datetime.now(timezone.utc)
    with get_session(engine) as s:
        set_setting(s, HEARTBEAT_KEY, json.dumps({"host": host or hostname(), "pid": pid or os.getpid(),
                                                  "at": now.isoformat()}))
    try:
        path = Path(settings.DATA_DIR) / "heartbeat.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(now.isoformat() + "\n")
    except OSError as exc:
        log.warning("could not write heartbeat file: %s", exc)


def clear_heartbeat(engine: Optional[Engine] = None, pid: Optional[int] = None) -> None:
    """Remove our heartbeat (only if it is ours) on a clean shutdown."""
    hb = read_heartbeat(engine)
    if hb and int(hb.get("pid") or 0) not in (pid or os.getpid(), 0) and hb.get("host") == hostname():
        return
    with get_session(engine) as s:
        set_setting(s, HEARTBEAT_KEY, "")
    try:
        (Path(settings.DATA_DIR) / "heartbeat.txt").unlink(missing_ok=True)
    except OSError:
        pass
