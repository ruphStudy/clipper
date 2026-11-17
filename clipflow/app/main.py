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
    tg_ok = bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID)
    print(f"telegram: {'OK' if tg_ok else 'MISSING'}")
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


def telegram_chatid() -> int:
    """Print chat ids seen by the bot."""
    from app.notify import TelegramNotifier

    if not settings.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        return 0
    chats: dict = {}
    for update in TelegramNotifier().get_updates():
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if chat and "id" in chat:
                chats[chat["id"]] = chat
    if not chats:
        print("Send /start to your bot in Telegram, then run again.")
        return 0
    for chat_id, chat in chats.items():
        name = chat.get("title") or " ".join(
            p for p in (chat.get("first_name"), chat.get("last_name")) if p
        )
        user = f" @{chat['username']}" if chat.get("username") else ""
        print(f"{chat_id}  {name}{user} ({chat.get('type', '?')})")
    return 0


def telegram_test() -> int:
    """Send a test message."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.notify import TelegramNotifier

    notifier = TelegramNotifier()
    if not notifier.enabled:
        print("DISABLED (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
        return 0
    now = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %Z")
    ok = notifier.send_message(f"✅ ClipFlow connected\n{now}")
    print("SENT" if ok else "FAILED")
    return 0


def scout_fetch(provider: str, dry_run: bool) -> int:
    """Run the campaign scout and print a summary."""
    from app.db import init_db
    from app.scout.schema import ScoutError
    from app.scout.service import run_scout

    ensure_dirs()
    if not dry_run:
        init_db()
    try:
        r = run_scout(provider=provider, dry_run=dry_run)
    except ScoutError as exc:
        print(f"scout FAILED: {exc}")
        return 1
    tag = " (dry-run)" if dry_run else ""
    print(f"provider={r.provider_used} fetched={r.fetched} new={r.new} "
          f"updated={r.updated} ended={r.ended}{tag}")
    top = sorted(r.campaigns, key=lambda c: c.budget_left or 0, reverse=True)[:5]
    for c in top:
        cpm = f"${c.cpm_rate:g}/1K" if c.cpm_rate is not None else (c.payout_type or "-")
        left = f"${c.budget_left:,.0f} left" if c.budget_left is not None else "? left"
        creators = c.creators_count if c.creators_count is not None else "?"
        print(f"{c.name} | {cpm} | {left} | {creators} | {','.join(c.platforms)}")
    return 0


def scout_score() -> int:
    """Re-score every non-ended campaign."""
    from app.db import get_session, init_db
    from app.scout.scorer import score_all

    ensure_dirs()
    init_db()
    with get_session() as s:
        count = score_all(s)
    print(f"scored {count} campaigns")
    return 0


def scout_top(limit: int, platform: str | None) -> int:
    """Print the top campaigns by score."""
    from sqlalchemy import select

    from app.db import get_session, init_db
    from app.db.enums import CampaignStatus
    from app.db.models import Campaign

    ensure_dirs()
    init_db()
    with get_session() as s:
        rows = s.scalars(
            select(Campaign)
            .where(Campaign.score > 0, Campaign.status != CampaignStatus.ended)
            .order_by(Campaign.score.desc())
        ).all()
    if platform:
        rows = [c for c in rows if platform.lower() in [p.lower() for p in (c.platforms or [])]]
    rows = rows[:limit]
    if not rows:
        print("no scored campaigns (run scout-fetch or scout-score)")
        return 0
    print("rank | score | name | $cpm/1K | est $/clip | $left | days_left | creators | reasons")
    for i, c in enumerate(rows, 1):
        name = c.name if len(c.name) <= 40 else c.name[:39] + "…"
        cpm = f"${c.cpm_rate:g}" if c.cpm_rate is not None else "-"
        est = f"${c.est_per_clip:.2f}" if c.est_per_clip is not None else "-"
        left = f"${c.budget_left:,.0f}" if c.budget_left is not None else "-"
        days = f"{c.days_left:g}" if c.days_left is not None else "-"
        creators = c.creators_count if c.creators_count is not None else "-"
        reasons = "; ".join(c.score_reasons or [])
        print(f"{i} | {c.score:.1f} | {name} | {cpm} | {est} | {left} | {days} | {creators} | {reasons}")
    return 0


def main(argv=None) -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(prog="clipflow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="run health check")
    sub.add_parser("version", help="print version")
    sub.add_parser("initdb", help="create database tables")
    sub.add_parser("telegram-chatid", help="list chat ids that messaged the bot")
    sub.add_parser("telegram-test", help="send a test Telegram message")
    scout = sub.add_parser("scout-fetch", help="fetch public campaigns into the DB")
    scout.add_argument("--provider", choices=["auto", "direct", "apify"], default="auto")
    scout.add_argument("--dry-run", action="store_true")
    sub.add_parser("scout-score", help="re-score all campaigns")
    top = sub.add_parser("scout-top", help="show best-scored campaigns")
    top.add_argument("--limit", type=int, default=10)
    top.add_argument("--platform", default=None)
    args = parser.parse_args(argv)
    if args.command == "scout-fetch":
        return scout_fetch(args.provider, args.dry_run)
    if args.command == "scout-score":
        return scout_score()
    if args.command == "scout-top":
        return scout_top(args.limit, args.platform)
    if args.command == "telegram-chatid":
        return telegram_chatid()
    if args.command == "telegram-test":
        return telegram_test()
    if args.command == "version":
        print(f"ClipFlow {VERSION}")
        return 0
    if args.command == "initdb":
        return initdb()
    return check()


if __name__ == "__main__":
    sys.exit(main())
