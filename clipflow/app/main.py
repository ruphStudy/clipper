"""ClipFlow CLI entry point."""
import argparse
import shutil
import sys
from pathlib import Path

from sqlalchemy import inspect

from app.config import ensure_dirs, masked, settings
from app.logger import get_logger

VERSION = "0.1.0"
SECRETS = (
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DASHBOARD_PASSWORD",
    "YOUTUBE_CLIENT_SECRETS_PATH",
    "INSTAGRAM_ACCESS_TOKEN",
    "INSTAGRAM_USER_ID",
)


def check() -> int:
    """Run the health check."""
    ensure_dirs()
    log = get_logger("clipflow.check")
    print(f"app: env={settings.APP_ENV} log_level={settings.LOG_LEVEL} tz={settings.TIMEZONE}")
    print(
        f"paths: OK ({settings.DATA_DIR}, {settings.DOWNLOAD_DIR}, "
        f"{settings.CLIPS_DIR}, {settings.LOG_DIR})"
    )
    db_ok = Path(settings.DB_PATH).is_file()
    print(f"db: {'OK' if db_ok else 'MISSING'} ({settings.DB_PATH})")
    for key in SECRETS:
        value = getattr(settings, key)
        print(f"{key}: {'SET (' + masked(value) + ')' if value else 'MISSING'}")
    if shutil.which("ffmpeg"):
        print("ffmpeg: OK")
    else:
        print("ffmpeg: MISSING (warning)")
    log.info("health check done")
    return 0


def initdb() -> int:
    """Create the database and all tables."""
    from app.db import init_db

    ensure_dirs()
    engine = init_db()
    tables = inspect(engine).get_table_names()
    print(f"DB ready: {settings.DB_PATH} ({len(tables)} tables)")
    get_logger("clipflow.db").info("database initialised")
    return 0


def main(argv=None) -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(prog="clipflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="run health check")
    sub.add_parser("version", help="print version")
    sub.add_parser("initdb", help="create database tables")
    args = parser.parse_args(argv)
    if args.command == "version":
        print(f"ClipFlow {VERSION}")
        return 0
    if args.command == "initdb":
        return initdb()
    return check()


if __name__ == "__main__":
    sys.exit(main())
