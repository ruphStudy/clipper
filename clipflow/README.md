# ClipFlow

## Setup
    python3.11 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    cp .env.example .env

## Run
    .venv/bin/python -m app.main check
    .venv/bin/python -m app.main version
    .venv/bin/python -m pytest -q
