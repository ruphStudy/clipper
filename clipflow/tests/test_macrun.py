"""Mac run: single-instance lock, keep-awake, battery checks, LaunchAgent plists (no real launchctl)."""
import os
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import config, main
from app.db import get_engine, init_db
from app.db import session as db_session
from app.scheduler import jobs, launchd, lock, power
from app.scheduler.lock import check_can_start, clear_heartbeat, read_heartbeat, write_heartbeat

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    for key in ("DATA_DIR", "DOWNLOAD_DIR", "CLIPS_DIR", "LOG_DIR", "TRANSCRIPTS_DIR", "SECRETS_DIR"):
        monkeypatch.setattr(config.settings, key, str(tmp_path / key.lower()))
    eng = init_db(get_engine(str(tmp_path / "m.db")))
    monkeypatch.setattr(db_session, "_engine", eng)
    yield eng
    eng.dispose()


def dead_pid() -> int:
    pid = 999_999
    while lock.pid_alive(pid):
        pid -= 1
    return pid


# --- lock -----------------------------------------------------------------------------------------

def test_lock_rules(engine):
    host = lock.hostname()
    write_heartbeat(engine, NOW - timedelta(seconds=30), host=host, pid=os.getppid())  # alive, not us
    ok, msg = check_can_start(engine=engine, now=NOW)
    assert not ok and f"pid {os.getppid()}" in msg and "--force" in msg
    assert check_can_start(force=True, engine=engine, now=NOW)[0]
    assert check_can_start(engine=engine, now=NOW + timedelta(minutes=4))[0]  # stale heartbeat
    write_heartbeat(engine, NOW, host=host, pid=dead_pid())
    assert check_can_start(engine=engine, now=NOW)[0]  # dead pid on this host
    write_heartbeat(engine, NOW, host="other-mac", pid=os.getppid())
    assert not check_can_start(engine=engine, now=NOW)[0]  # fresh, another host
    assert (Path(config.settings.DATA_DIR) / "heartbeat.txt").read_text().strip() == NOW.isoformat()


def test_run_scheduler_refuses_then_force(engine, monkeypatch):
    write_heartbeat(engine, datetime.now(timezone.utc), host=lock.hostname(), pid=os.getppid())
    assert main.main(["run-scheduler"]) == 2

    events: list = []

    class FakeSched:
        def add_job(self, *a, **k):
            pass

        def get_jobs(self):
            return []

        def start(self):
            events.append(("started", read_heartbeat()["pid"]))
            raise KeyboardInterrupt

        def shutdown(self, wait=False):
            pass

    import app.notify as notify_mod
    monkeypatch.setattr(jobs, "build_scheduler", lambda: FakeSched())
    monkeypatch.setattr(power, "start_keep_awake", lambda pid: events.append(("caffeinate", pid)))
    monkeypatch.setattr(notify_mod, "notify", lambda title, body="", level="info": events.append(("notify", body)))
    assert main.main(["run-scheduler", "--force"]) == 0
    assert ("caffeinate", os.getpid()) in events and ("started", os.getpid()) in events
    assert any(b.startswith("▶️ ClipFlow scheduler started on") for k, b in events if k == "notify")
    assert ("notify", "⏹ ClipFlow scheduler stopped") in events
    assert not read_heartbeat()  # clean shutdown cleared it
    assert not (Path(config.settings.DATA_DIR) / "heartbeat.txt").exists()


def test_heartbeat_job_not_in_business_jobs():
    sched = jobs.build_scheduler()
    business = {j.id for j in sched.get_jobs()}
    jobs.add_heartbeat_job(sched)
    assert "heartbeat" not in business and {j.id for j in sched.get_jobs()} == business | {"heartbeat"}


def test_clear_heartbeat_leaves_foreign_alive(engine):
    write_heartbeat(engine, NOW, host=lock.hostname(), pid=os.getppid())
    clear_heartbeat(engine)
    assert read_heartbeat(engine)["pid"] == os.getppid()


# --- keep-awake ---------------------------------------------------------------------------------

def test_keep_awake(monkeypatch):
    calls: list = []
    fake = lambda args, **kw: calls.append(args) or "proc"  # noqa: E731
    monkeypatch.setattr(config.settings, "KEEP_AWAKE", True)
    assert power.start_keep_awake(4242, platform="darwin", popen=fake) == "proc"
    assert calls == [["caffeinate", "-i", "-s", "-w", "4242"]]
    assert power.start_keep_awake(4242, platform="linux", popen=fake) is None
    monkeypatch.setattr(config.settings, "KEEP_AWAKE", False)
    assert power.start_keep_awake(4242, platform="darwin", popen=fake) is None and len(calls) == 1


# --- battery ------------------------------------------------------------------------------------

AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=4653155)\t100%; charged; 0:00 remaining present: true\n"
BATT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=4653155)\t{p}%; discharging; 3:10 remaining present: true\n"


def test_parse_pmset():
    assert power.parse_pmset(AC) == {"ac": True, "percent": 100}
    assert power.parse_pmset(BATT.format(p=55)) == {"ac": False, "percent": 55}
    assert power.parse_pmset("garbage") is None and power.parse_pmset("") is None


def test_check_power_throttled(engine):
    sent: list = []
    note = lambda title, body="", level="info": sent.append((level, body))  # noqa: E731
    out = {"text": AC}
    run = lambda: out["text"]  # noqa: E731

    def check(t):
        return power.check_power(t, engine, run, note, platform="darwin")

    assert check(NOW) == {"ac": True, "percent": 100} and sent == []
    out["text"] = BATT.format(p=55)
    assert check(NOW + timedelta(minutes=5)) is None  # within 10 min: not checked
    check(NOW + timedelta(minutes=11))
    assert sent == [("warning", "Mac on battery: plug in or ClipFlow may sleep")]
    check(NOW + timedelta(minutes=30))
    assert len(sent) == 1  # warning throttled to once per hour
    out["text"] = BATT.format(p=15)
    check(NOW + timedelta(minutes=75))
    assert [lvl for lvl, _ in sent] == ["warning", "warning", "error"]
    check(NOW + timedelta(minutes=90))
    assert [lvl for lvl, _ in sent].count("error") == 1  # low-battery error once
    out["text"] = "unparseable"
    assert check(NOW + timedelta(minutes=120)) is None
    assert power.check_power(NOW, engine, run, note, platform="linux") is None


# --- launchd plists + scripts ------------------------------------------------------------------------

def test_plists(monkeypatch, tmp_path):
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "")
    sched = plistlib.loads(launchd.render_plist("scheduler", REPO))
    dash = plistlib.loads(launchd.render_plist("dashboard", REPO))
    for p in (sched, dash):
        assert p["RunAtLoad"] is True and p["KeepAlive"] == {"SuccessfulExit": False} and p["ThrottleInterval"] == 30
        assert p["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:/usr/local/bin")
        assert p["WorkingDirectory"] == str(REPO) and p["ProgramArguments"][0].endswith(".venv/bin/python")
    assert sched["ProgramArguments"][-1] == "run-scheduler" and sched["Label"] == "com.clipflow.scheduler"
    assert sched["StandardOutPath"].endswith("data/logs/launchd-scheduler.log")
    assert dash["ProgramArguments"][-4:] == ["--host", "127.0.0.1", "--port", "8000"]
    with pytest.raises(launchd.LanRefused):
        launchd.render_plist("dashboard", REPO, lan=True)
    assert launchd.main(["write", "--out-dir", str(tmp_path / "agents"), "--lan"]) == 1
    assert not (tmp_path / "agents" / "com.clipflow.dashboard.plist").exists()
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    assert "0.0.0.0" in plistlib.loads(launchd.render_plist("dashboard", REPO, lan=True))["ProgramArguments"]
    assert launchd.main(["write", "--out-dir", str(tmp_path / "agents")]) == 0
    assert (tmp_path / "agents" / "com.clipflow.scheduler.plist").exists()


@pytest.mark.skipif(not Path("/usr/bin/plutil").exists(), reason="macOS only")
def test_plutil_and_script_usage(tmp_path):
    launchd.main(["write", "--out-dir", str(tmp_path)])
    for f in tmp_path.glob("*.plist"):
        assert subprocess.run(["plutil", "-lint", str(f)], capture_output=True).returncode == 0
    for script in ("install.sh", "restart.sh", "logs.sh", "status.sh", "uninstall.sh"):
        r = subprocess.run(["bash", str(REPO / "scripts" / "mac" / script), "--bogus"], capture_output=True, text=True)
        assert r.returncode == 1 and "usage:" in r.stdout
