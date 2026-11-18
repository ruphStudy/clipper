"""Render the user LaunchAgent plists (used by scripts/mac/install.sh and the tests).

    python -m app.scheduler.launchd write --out-dir ~/Library/LaunchAgents [--lan]
"""
import argparse
import plistlib
import sys
from pathlib import Path

from app.config import settings

LABELS = {"scheduler": "com.clipflow.scheduler", "dashboard": "com.clipflow.dashboard"}
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PORT = 8000


class LanRefused(Exception):
    """--lan without DASHBOARD_PASSWORD."""


def render_plist(name: str, repo: Path, lan: bool = False) -> bytes:
    """XML plist for one service ('scheduler' or 'dashboard')."""
    repo = Path(repo).resolve()
    python = str(repo / ".venv" / "bin" / "python")
    if name == "scheduler":
        args = [python, "-m", "app.main", "run-scheduler"]
    elif name == "dashboard":
        if lan and not settings.DASHBOARD_PASSWORD:
            raise LanRefused("--lan needs DASHBOARD_PASSWORD in .env (the dashboard would be open to your Wi-Fi)")
        args = [python, "-m", "app.main", "dashboard", "--host", "0.0.0.0" if lan else "127.0.0.1",
                "--port", str(PORT)]
    else:
        raise ValueError(f"unknown service {name}")
    log = str(repo / "data" / "logs" / f"launchd-{name}.log")
    return plistlib.dumps({
        "Label": LABELS[name],
        "ProgramArguments": args,
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after a clean exit
        "ThrottleInterval": 30,
        "EnvironmentVariables": {"PATH": LAUNCHD_PATH, "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
    })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="launchd")
    parser.add_argument("action", choices=["write"])
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--lan", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[2]
    out = Path(args.out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    try:
        plists = {name: render_plist(name, repo, args.lan) for name in LABELS}
    except LanRefused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    (repo / "data" / "logs").mkdir(parents=True, exist_ok=True)
    for name, data in plists.items():
        path = out / f"{LABELS[name]}.plist"
        path.write_bytes(data)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
