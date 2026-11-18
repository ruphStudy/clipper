# ClipFlow

## Setup
    python3.11 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    cp .env.example .env

## Run
    .venv/bin/python -m app.main check
    .venv/bin/python -m app.main version
    .venv/bin/python -m pytest -q

## Run on Mac (first month)
Install the scheduler + dashboard as background services (user LaunchAgents, no sudo):

    make install                     # dashboard on http://127.0.0.1:8000
    scripts/mac/install.sh --lan     # phone access on Wi-Fi (needs DASHBOARD_PASSWORD in .env)

Check and inspect:

    make status                      # pids, last exit, heartbeat age, posting status, /health
    make logs                        # app log (scripts/mac/logs.sh scheduler|dashboard|app -f)
    make restart                     # restart both services

Keep it running:
- Keep the Mac plugged in. ClipFlow keeps it awake (caffeinate) only while on AC power and warns on battery.
- A MacBook sleeps when the lid is closed unless it is on power with an external display (clamshell mode).
  Keep the lid open, or use clamshell mode.
- The services restart after a crash and start again at login.
- Don't also run `run-scheduler` in a terminal: a second scheduler refuses to start while one is alive.

Remove:

    make uninstall
