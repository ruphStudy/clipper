"""ClipFlow CLI entry point."""
import argparse
import shutil
import sys

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
    for key in SECRETS:
        value = getattr(settings, key)
        print(f"{key}: {'SET (' + masked(value) + ')' if value else 'MISSING'}")
    if shutil.which("ffmpeg"):
        print("ffmpeg: OK")
    else:
        print("ffmpeg: MISSING (warning)")
    log.info("health check done")
    return 0


def main(argv=None) -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(prog="clipflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="run health check")
    sub.add_parser("version", help="print version")
    args = parser.parse_args(argv)
    if args.command == "version":
        print(f"ClipFlow {VERSION}")
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(main())
