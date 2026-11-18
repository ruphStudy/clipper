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
    "META_APP_ID",
    "META_APP_SECRET",
    "YOUTUBE_API_KEY",
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
    with get_session() as s:
        from app.scout.scorer import effective_typical_views
        tv, source = effective_typical_views(s)
    print(f"(est $/clip uses {tv:,.0f} typical views — {source})")
    print("rank | score | type | name | $cpm/1K | est $/clip | $left | days_left | creators | reasons")
    for i, c in enumerate(rows, 1):
        name = c.name if len(c.name) <= 40 else c.name[:39] + "…"
        cpm = f"${c.cpm_rate:g}" if c.cpm_rate is not None else "-"
        est = f"${c.est_per_clip:.2f}" if c.est_per_clip is not None else "-"
        left = f"${c.budget_left:,.0f}" if c.budget_left is not None else "-"
        days = f"{c.days_left:g}" if c.days_left is not None else "-"
        creators = c.creators_count if c.creators_count is not None else "-"
        reasons = "; ".join(c.score_reasons or [])
        ctype = c.campaign_type or "-"
        print(f"{i} | {c.score:.1f} | {ctype} | {name} | {cpm} | {est} | {left} | {days} | {creators} | {reasons}")
    return 0


def campaign_set(campaign_id: int, status: str, notes: str | None) -> int:
    """Manually set a campaign's status (and notes)."""
    from app.db import get_session, init_db
    from app.db.enums import CampaignStatus
    from app.db.models import Campaign

    try:
        new_status = CampaignStatus(status)
    except ValueError:
        print(f"error: unknown status '{status}' (use: {', '.join(s.value for s in CampaignStatus)})")
        return 1
    ensure_dirs()
    init_db()
    with get_session() as s:
        c = s.get(Campaign, campaign_id)
        if c is None:
            print(f"error: no campaign with id {campaign_id}")
            return 1
        c.status = new_status
        if notes is not None:
            c.notes = notes
        name = c.name
    print(f"#{campaign_id} {name} → {new_status.value}")
    return 0


def campaign_list(status: str | None) -> int:
    """List campaigns, optionally filtered by status."""
    from sqlalchemy import select

    from app.db import get_session, init_db
    from app.db.enums import CampaignStatus
    from app.db.models import Campaign

    ensure_dirs()
    init_db()
    q = select(Campaign).order_by(Campaign.score.desc(), Campaign.id)
    if status:
        try:
            q = q.where(Campaign.status == CampaignStatus(status))
        except ValueError:
            print(f"error: unknown status '{status}'")
            return 1
    with get_session() as s:
        rows = s.scalars(q).all()
    print("id | status | score | name | url")
    for c in rows:
        print(f"{c.id} | {c.status.value} | {(c.score or 0):.1f} | {c.name} | {c.url}")
    if not rows:
        print("(none)")
    return 0


def scout_report(no_send: bool) -> int:
    """Build (and optionally send) the weekly report."""
    from app.db import get_session, init_db
    from app.scout.report import build_report, send_weekly_report, strip_tags

    ensure_dirs()
    init_db()
    if no_send:
        with get_session() as s:
            text, csv_path = build_report(s)
        print(strip_tags(text))
        print(f"CSV: {csv_path}")
        return 0
    ok = send_weekly_report()
    print("SENT" if ok else "NOT SENT")
    return 0


def run_scheduler(force: bool = False) -> int:
    """Start the blocking scheduler (single instance, heartbeat, keep-awake, clean shutdown)."""
    import os
    import signal

    from app.db import init_db
    from app.notify import notify
    from app.scheduler.jobs import add_heartbeat_job, build_scheduler, next_runs
    from app.scheduler.lock import check_can_start, clear_heartbeat, hostname, write_heartbeat
    from app.scheduler.power import start_keep_awake

    ensure_dirs()
    init_db()
    log = get_logger("clipflow.scheduler")
    ok, reason = check_can_start(force=force)
    if not ok:
        print(f"refusing to start: {reason}")
        log.error("refusing to start: %s", reason)
        return 2
    write_heartbeat()
    sched = build_scheduler()
    add_heartbeat_job(sched)
    for job_id, when in next_runs(sched):
        log.info("job %s next run: %s", job_id, when)
    caffeinate = start_keep_awake(os.getpid())

    def _stop(signum, _frame):
        log.info("received %s; shutting down", signal.Signals(signum).name)
        try:
            sched.shutdown(wait=False)
        except Exception:  # noqa: BLE001 — not running yet / already stopped
            raise SystemExit(0)

    previous = {sig: signal.signal(sig, _stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        notify("ClipFlow scheduler started", f"▶️ ClipFlow scheduler started on {hostname()}")
    except Exception:  # noqa: BLE001
        pass
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        clear_heartbeat()
        if caffeinate is not None:
            caffeinate.terminate()
        log.info("scheduler stopped")
        try:
            notify("ClipFlow scheduler stopped", "⏹ ClipFlow scheduler stopped")
        except Exception:  # noqa: BLE001
            pass
    return 0


def dashboard(host: str, port: int) -> int:
    """Run the web dashboard with uvicorn."""
    if host not in ("127.0.0.1", "localhost", "::1") and not settings.DASHBOARD_PASSWORD:
        print(f"Refusing to start on {host}: set DASHBOARD_PASSWORD in .env before exposing the dashboard "
              "(or use --host 127.0.0.1).")
        return 1
    import uvicorn

    from app.dashboard.app import create_app
    from app.db import init_db

    ensure_dirs()
    init_db()
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


def download_cmd(source_id: int) -> int:
    """Download one source now."""
    from app.db import get_session, init_db
    from app.db.models import Source
    from app.downloader.service import download_source

    ensure_dirs()
    init_db()
    status = download_source(source_id)
    with get_session() as s:
        src = s.get(Source, source_id)
        if src is None:
            print(f"source #{source_id}: not found")
            return 1
        size = Path(src.local_path).stat().st_size / 1024 ** 2 if src.local_path and Path(src.local_path).exists() else 0
        detail = (f"{src.duration_sec:.0f}s, {size:.1f} MB, {src.local_path}" if status == "downloaded"
                  else (src.error or ""))
    print(f"source #{source_id}: {status}" + (f" — {detail}" if detail else ""))
    return 0 if status in ("downloaded", "processed") else 1


def download_pending_cmd(limit: int) -> int:
    """Download the oldest pending sources."""
    from app.db import init_db
    from app.downloader.service import download_pending

    ensure_dirs()
    init_db()
    r = download_pending(limit=limit)
    print(f"downloaded={r['downloaded']} failed={r['failed']} expanded={r['expanded']} skipped={r['skipped']}")
    return 0


def download_reset_cmd() -> int:
    """Return stuck downloads to pending."""
    from app.db import init_db
    from app.downloader.service import reset_stuck

    ensure_dirs()
    init_db()
    print(f"reset {reset_stuck()} stuck downloads to pending")
    return 0


def cleanup_cmd(dry_run: bool) -> int:
    """Run disk cleanup and print the summary."""
    from app.db import init_db
    from app.downloader.cleanup import run_cleanup

    ensure_dirs()
    init_db()
    r = run_cleanup(dry_run=dry_run)
    tag = " (dry run: nothing deleted)" if dry_run else ""
    print(f"files_deleted={r['files_deleted']} gb_freed={r['gb_freed']} orphans={r['orphans']} "
          f"warnings={'; '.join(r['warnings']) or 'none'}{tag}")
    return 0


def queue_status_cmd() -> int:
    """Print source counts by status, next retry, and disk usage."""
    from zoneinfo import ZoneInfo

    from sqlalchemy import func, select

    from app.db import get_session, init_db
    from app.db.enums import SourceStatus
    from app.db.models import Source
    from app.downloader.media import disk_usage_gb, volume_used_pct
    from app.scout.scorer import utc

    ensure_dirs()
    init_db()
    with get_session() as s:
        counts = dict(s.execute(select(Source.status, func.count()).group_by(Source.status)).all())
        nxt = s.scalar(select(func.min(Source.next_retry_at)).where(
            Source.status == SourceStatus.failed, Source.error_permanent.is_not(True)))
    parts = [f"{st.value}={counts.get(st, 0)}" for st in SourceStatus]
    when = utc(nxt).astimezone(ZoneInfo(settings.TIMEZONE)).strftime("%d %b %H:%M") if nxt else "-"
    print("sources: " + " ".join(parts))
    print(f"next retry: {when} | downloads {disk_usage_gb(settings.DOWNLOAD_DIR):.2f} GB | "
          f"clips {disk_usage_gb(settings.CLIPS_DIR):.2f} GB | volume {volume_used_pct():.0f}% used "
          f"(limit {settings.MAX_DISK_GB} GB)")
    return 0


def _transcript_line(source_id: int, seconds: float) -> str:
    from app.db import get_session
    from app.db.models import Source

    with get_session() as s:
        src = s.get(Source, source_id)
        if src is None:
            return f"source #{source_id}: not found"
        if src.transcript_path:
            return (f"source #{source_id}: transcribed — {src.word_count} words, speech "
                    f"{(src.speech_ratio or 0) * 100:.0f}%, language {src.language}, {seconds:.1f}s")
        why = src.error or f"not eligible (status {src.status.value}, attempts {src.transcribe_attempts or 0})"
        return f"source #{source_id}: not transcribed — {why}"


def transcribe_cmd(source_id: int) -> int:
    """Transcribe one source now."""
    import time

    from app.clipper.transcribe_service import transcribe_source
    from app.db import init_db

    ensure_dirs()
    init_db()
    started = time.monotonic()
    ok = transcribe_source(source_id)
    print(_transcript_line(source_id, time.monotonic() - started))
    return 0 if ok else 1


def transcribe_pending_cmd(limit: int) -> int:
    """Transcribe the oldest pending transcriptions."""
    import time

    from app.clipper.transcribe_service import transcribe_pending
    from app.db import init_db

    ensure_dirs()
    init_db()
    started = time.monotonic()
    r = transcribe_pending(limit=limit)
    print(f"transcribed={r['done']} failed={r['failed']} in {time.monotonic() - started:.1f}s")
    return 0


def moments_cmd(source_id: int) -> int:
    """Pick moments for one source and list its candidates."""
    from sqlalchemy import select

    from app.clipper.moments import fmt_ts
    from app.clipper.moments_service import pick_moments
    from app.db import get_session, init_db
    from app.db.enums import ClipStatus
    from app.db.models import Clip, Source

    ensure_dirs()
    init_db()
    created = pick_moments(source_id)
    with get_session() as s:
        src = s.get(Source, source_id)
        if src is None:
            print(f"source #{source_id}: not found")
            return 1
        clips = s.scalars(select(Clip).where(Clip.source_id == source_id, Clip.status == ClipStatus.candidate)
                          .order_by(Clip.moment_score.desc())).all()
        print(f"source #{source_id}: {created} new candidates ({src.moments_method or '-'}), {len(clips)} total"
              + (f" — {src.error}" if src.error and src.error.startswith("moments:") else ""))
        for c in clips:
            print(f"{fmt_ts(c.start_sec)}–{fmt_ts(c.end_sec)} | {c.moment_score:g} | {c.hook_title or '-'}")
    return 0


def ai_usage_cmd() -> int:
    """This month's AI usage."""
    from app.ai.usage import month_usage
    from app.db import init_db

    ensure_dirs()
    init_db()
    u = month_usage()
    print(f"this month: {u['calls']} calls, {u['input_tokens']} in / {u['output_tokens']} out tokens, "
          f"${u['spend']:.4f} of ${u['budget']:g} budget")
    return 0


def render_cmd(clip_id: int) -> int:
    """Render one clip now."""
    import time

    from app.clipper.render import render_clip
    from app.db import get_session, init_db
    from app.db.models import Clip

    ensure_dirs()
    init_db()
    started = time.monotonic()
    ok = render_clip(clip_id)
    took = time.monotonic() - started
    with get_session() as s:
        c = s.get(Clip, clip_id)
        if c is None:
            print(f"clip #{clip_id}: not found")
            return 1
        if ok:
            size = Path(c.file_path).stat().st_size / 1024 ** 2
            print(f"clip #{clip_id}: rendered — layout {c.layout}, {c.duration_sec:.1f}s, {size:.1f} MB, "
                  f"{took:.1f}s → {c.file_path}")
        else:
            why = c.reject_reason or f"status {c.status.value}, attempts {c.render_attempts or 0}"
            print(f"clip #{clip_id}: not rendered — {why}")
    return 0 if ok else 1


def render_pending_cmd(limit: int | None) -> int:
    """Render the best candidate clips."""
    import time

    from app.clipper.render import render_pending
    from app.db import init_db

    ensure_dirs()
    init_db()
    started = time.monotonic()
    r = render_pending(limit=limit)
    print(f"rendered={r['rendered']} failed={r['failed']} in {time.monotonic() - started:.1f}s")
    return 0


def check_clip_cmd(clip_id: int) -> int:
    """Compliance for one clip (≤6 lines)."""
    from app.clipper.compliance import check_clip
    from app.db import init_db

    ensure_dirs()
    init_db()
    r = check_clip(clip_id)
    print(f"clip #{clip_id}: {'OK' if r['ok'] else 'BLOCKED'} — {len(r['errors'])} errors, {len(r['warnings'])} warnings")
    lines = [f"✗ {e}" for e in r["errors"]] + [f"⚠ {w}" for w in r["warnings"]]
    for line in lines[:4]:
        print(f"  {line}")
    if len(lines) > 4:
        print(f"  … {len(lines) - 4} more")
    return 0 if r["ok"] else 1


def check_clips_cmd(status: str) -> int:
    """Compliance summary for all clips with a status."""
    from sqlalchemy import select

    from app.clipper.compliance import check_clip
    from app.db import get_session, init_db
    from app.db.enums import ClipStatus
    from app.db.models import Clip

    ensure_dirs()
    init_db()
    try:
        wanted = ClipStatus(status)
    except ValueError:
        print(f"error: unknown clip status '{status}'")
        return 1
    with get_session() as s:
        ids = list(s.scalars(select(Clip.id).where(Clip.status == wanted).order_by(Clip.id)))
    ok = errs = warns = 0
    for cid in ids:
        r = check_clip(cid)
        ok += r["ok"]
        errs += bool(r["errors"])
        warns += bool(r["warnings"])
    print(f"checked={len(ids)} ok={ok} with_errors={errs} with_warnings={warns} (status {status})")
    return 0


def review_cmd(args) -> int:
    """review-stats / approve / reject."""
    from app.db import init_db
    from app.review import service

    ensure_dirs()
    init_db()
    if args.command == "review-stats":
        st = service.stats_today()
        print(f"waiting={st['waiting']} approved_today={st['approved']} rejected_today={st['rejected']}")
        return 0
    if args.command == "approve":
        ok, msg = service.approve(args.clip_id)
    else:
        ok, msg = service.reject(args.clip_id, " ".join(args.reason))
    print(msg)
    return 0 if ok else 1


def youtube_cmd(args) -> int:
    """yt-auth / yt-whoami / yt-upload / accounts (never prints tokens)."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db import get_session, init_db
    from app.db.models import Account
    from app.uploader.youtube import default_account, upload_youtube, uploads_today
    from app.uploader.youtube_auth import AuthError, authorize, whoami

    ensure_dirs()
    init_db()
    if args.command == "accounts":
        now = datetime.now(timezone.utc)
        with get_session() as s:
            rows = s.scalars(select(Account).order_by(Account.platform, Account.id)).all()
            print("id | platform | handle | active | auth | uploads today")
            from app.uploader.instagram import uploads_today as ig_uploads_today

            for a in rows:
                auth = a.auth_error or ("ok" if a.credentials_ref else "not authorized")
                today = {"youtube": lambda: uploads_today(s, now, a.id),
                         "instagram": lambda: ig_uploads_today(s, now, a.id)}.get(a.platform.value, lambda: "-")()
                print(f"{a.id} | {a.platform.value} | {a.handle} | {'yes' if a.is_active else 'no'} | {auth} | {today}")
            if not rows:
                print("(no accounts — run yt-auth <handle> or ig-connect <token>)")
        return 0
    if args.command == "yt-auth":
        try:
            acct = authorize(args.handle)
        except AuthError as exc:
            print(f"error: {exc}")
            return 1
        print(f"Authorized: {acct.display_name} ({acct.channel_id})")
        return 0
    if args.command == "yt-whoami":
        with get_session() as s:
            acct = s.get(Account, args.account) if args.account else None
        acct = acct or default_account()
        if acct is None:
            print("no active YouTube account (run yt-auth <handle>)")
            return 1
        try:
            me = whoami(acct)
        except AuthError as exc:
            print(f"account #{acct.id} {acct.handle}: {exc}")
            return 1
        print(f"account #{acct.id} {acct.handle}: {me['title']} ({me['channel_id']})")
        return 0
    acct_id = args.account or getattr(default_account(), "id", None)
    if acct_id is None:
        print("no active YouTube account (run yt-auth <handle>)")
        return 1
    post, msg = upload_youtube(args.clip_id, acct_id, privacy=args.privacy)
    print(msg)
    return 0 if post is not None and post.status.value == "posted" else 1


def instagram_cmd(args) -> int:
    """ig-connect / ig-check / ig-upload (never prints tokens)."""
    from datetime import datetime, timezone

    import httpx

    from app.db import get_session, init_db
    from app.db.models import Account
    from app.uploader.common import AuthError
    from app.uploader.instagram import default_account, upload_instagram
    from app.uploader.instagram_auth import GraphError, connect_instagram, debug_token, graph_call, load_token

    ensure_dirs()
    init_db()
    if args.command == "ig-connect":
        try:
            acct = connect_instagram(args.token, args.page)
        except AuthError as exc:
            print(f"error: {exc}")
            return 1
        print(f"Connected: @{acct.handle} via page {acct.display_name}")
        return 0
    with get_session() as s:
        acct = s.get(Account, args.account) if args.account else None
    acct = acct or default_account()
    if acct is None:
        print("no active Instagram account (run ig-connect <token>)")
        return 1
    if args.command == "ig-upload":
        post, msg = upload_instagram(args.clip_id, acct.id)
        print(msg)
        return 0 if post is not None and post.status.value == "posted" else 1
    try:
        info = debug_token(acct)
        ig_user_id, token = load_token(acct)
        with httpx.Client(timeout=30) as client:
            limit = (graph_call(client, "GET", f"{ig_user_id}/content_publishing_limit", token,
                                params={"fields": "quota_usage,config"}).get("data") or [{}])[0]
    except (AuthError, GraphError) as exc:
        print(f"@{acct.handle}: {exc}")
        return 1
    exp = info["expires_at"]
    expiry = "never" if not exp else datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"@{acct.handle}: token valid, expires {expiry}, scopes: {', '.join(info['scopes']) or '-'}")
    print(f"publishing quota: {limit.get('quota_usage', '?')} / {(limit.get('config') or {}).get('quota_total', '?')} "
          f"per {(limit.get('config') or {}).get('quota_duration', '?')}s")
    return 0


def posting_cmd(args) -> int:
    """account-add / account-toggle / plan-posts / execute-posts / mark-posted / posting."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from sqlalchemy import func, select

    from app.db import get_session, init_db
    from app.db.enums import Platform, PostStatus
    from app.db.models import Account, Post
    from app.posting.executor import execute_due
    from app.posting.manual import mark_posted
    from app.posting.modes import is_paused, post_mode, set_paused
    from app.posting.planner import plan_posts

    ensure_dirs()
    init_db()
    if args.command == "account-add":
        with get_session() as s:
            exists = s.scalar(select(Account).where(Account.platform == Platform(args.platform),
                                                    Account.handle == args.handle))
            if exists:
                print(f"account #{exists.id} {args.platform} {args.handle} already exists")
                return 1
            acct = Account(platform=Platform(args.platform), handle=args.handle, is_active=True)
            s.add(acct)
            s.flush()
            print(f"added account #{acct.id} {args.platform} {args.handle} (mode {post_mode(args.platform, s)})")
        return 0
    if args.command == "account-toggle":
        with get_session() as s:
            acct = s.get(Account, args.account_id)
            if acct is None:
                print(f"account #{args.account_id} not found")
                return 1
            acct.is_active = not acct.is_active
            print(f"account #{acct.id} {acct.handle}: {'active' if acct.is_active else 'inactive'}")
        return 0
    if args.command == "plan-posts":
        print(f"planned {plan_posts()} posts")
        return 0
    if args.command == "execute-posts":
        r = execute_due()
        print("posting paused" if r["paused"] else
              " ".join(f"{k}={v}" for k, v in r.items() if k != "paused"))
        return 0
    if args.command == "mark-posted":
        ok, msg = mark_posted(args.post_id, args.url)
        print(msg)
        return 0 if ok else 1
    if args.action in ("pause", "resume"):
        set_paused(args.action == "pause")
        print("posting paused" if args.action == "pause" else "posting resumed")
        return 0
    tz = ZoneInfo(settings.TIMEZONE)
    with get_session() as s:
        counts = dict(s.execute(select(Post.status, func.count()).group_by(Post.status)).all())
        nxt = s.scalar(select(func.min(Post.scheduled_at)).where(Post.status == PostStatus.scheduled))
        paused = is_paused(s)
        modes = ", ".join(f"{p}={post_mode(p, s)}" for p in ("youtube", "instagram", "tiktok", "x"))
    from app.scout.scorer import utc
    when = utc(nxt).astimezone(tz).strftime("%d %b %H:%M") if nxt else "-"
    print(f"posting {'PAUSED' if paused else 'active'} | modes: {modes}")
    print("posts: " + " ".join(f"{st.value}={counts.get(st, 0)}" for st in PostStatus) + f" | next due: {when}")
    return 0


def submit_cmd(args) -> int:
    """mark-submitted / submit-pending."""
    from sqlalchemy import select

    from app.db import get_session, init_db
    from app.db.models import Account, Campaign, Clip
    from app.posting.submit import mark_submitted, to_submit

    ensure_dirs()
    init_db()
    if args.command == "mark-submitted":
        ok, msg = mark_submitted(args.post_id)
        print(msg)
        return 0 if ok else 1
    with get_session() as s:
        rows = to_submit(s)
        if not rows:
            print("nothing to submit")
            return 0
        print("post | platform | account | age | campaign | link")
        for post, age, late in rows:
            acct = s.get(Account, post.account_id)
            clip = s.get(Clip, post.clip_id)
            camp = s.get(Campaign, clip.campaign_id) if clip else None
            print(f"#{post.id} | {post.platform.value} | {acct.handle if acct else '?'} | {age} min"
                  f"{' LATE' if late else ''} | {camp.name if camp else '?'} | {post.post_url or '-'}")
    return 0


def tracker_cmd(args) -> int:
    """track-stats / set-views / stats / add-earnings."""
    from datetime import date

    from app.db import get_session, init_db
    from app.db.models import Campaign, CampaignEarning
    from app.tracker.analytics import compute
    from app.tracker.service import set_manual_views, track_due

    ensure_dirs()
    init_db()
    if args.command == "track-stats":
        r = track_due()
        print(f"checked={r['checked']} updated={r['updated']} errors={r['errors']}")
        return 0
    if args.command == "set-views":
        ok, msg = set_manual_views(args.post_id, args.views)
        print(msg)
        return 0 if ok else 1
    if args.command == "add-earnings":
        try:
            period = date.fromisoformat(args.date) if args.date else date.today()
        except ValueError:
            print("error: --date must be YYYY-MM-DD")
            return 1
        with get_session() as s:
            camp = s.get(Campaign, args.campaign_id)
            if camp is None:
                print(f"campaign #{args.campaign_id} not found")
                return 1
            s.add(CampaignEarning(campaign_id=camp.id, amount_usd=args.amount, period_end=period, note=args.note))
            print(f"recorded ${args.amount:.2f} for #{camp.id} {camp.name} (period end {period})")
        return 0
    with get_session() as s:
        d = compute(s, args.days)
    t = d["totals"]
    print(f"last {args.days} days: {t['posts']} posts · {t['views']:,} views · est ${t['est']:.2f} · actual ${t['actual']:.2f}"
          + (f" · flop rate {d['flop_rate']}%" if d["flop_rate"] is not None else ""))
    print("campaign | posts | views | avg | est $ | $/post | hit% | actual $")
    for r in d["by_campaign"][:10]:
        print(f"{r['campaign'][:32]} | {r['posts']} | {r['views']:,} | {r['avg_views']:,} | {r['est']:.2f} | "
              f"{r['est_per_post']:.2f} | {r['hit_rate']} | {r['actual']:.2f}")
    print("platform | posts | views | avg")
    for r in d["by_platform"]:
        print(f"{r['platform']} | {r['posts']} | {r['views']:,} | {r['avg_views']:,}")
    print("hour | posts | avg views")
    for r in d["by_slot_hour"]:
        print(f"{r['hour']:02d}:00 | {r['posts']} | {r['avg_views']:,}")
    return 0


def run_job(name: str) -> int:
    """Run one scheduler job now."""
    from app.scheduler.jobs import JOBS

    ensure_dirs()
    JOBS[name]()
    print(f"job {name} done")
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
    cset = sub.add_parser("campaign-set", help="set a campaign's status")
    cset.add_argument("id", type=int)
    cset.add_argument("status")
    cset.add_argument("--notes", default=None)
    clist = sub.add_parser("campaign-list", help="list campaigns")
    clist.add_argument("--status", default=None)
    rep = sub.add_parser("scout-report", help="weekly campaign report")
    rep.add_argument("--no-send", action="store_true")
    rs = sub.add_parser("run-scheduler", help="run scheduled jobs (blocking)")
    rs.add_argument("--force", action="store_true", help="start even if another scheduler looks alive")
    rj = sub.add_parser("run-job", help="run one job now")
    rj.add_argument("job", choices=["daily_scout", "weekly_report", "download_queue", "cleanup", "transcribe",
                                         "moments", "render", "review_digest", "plan_posts",
                                         "execute_posts", "telegram_commands", "submit_deadlines",
                                         "track_stats", "learn"])
    dash = sub.add_parser("dashboard", help="run the web dashboard")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8000)
    dl = sub.add_parser("download", help="download one source")
    dl.add_argument("source_id", type=int)
    dlp = sub.add_parser("download-pending", help="download the oldest pending sources")
    dlp.add_argument("--limit", type=int, default=5)
    sub.add_parser("download-reset-stuck", help="move stuck downloads back to pending")
    cl = sub.add_parser("cleanup", help="delete old/unneeded media files")
    cl.add_argument("--dry-run", action="store_true")
    sub.add_parser("queue-status", help="download queue and disk usage")
    tr = sub.add_parser("transcribe", help="transcribe one downloaded source")
    tr.add_argument("source_id", type=int)
    trp = sub.add_parser("transcribe-pending", help="transcribe the oldest downloaded sources")
    trp.add_argument("--limit", type=int, default=1)
    mo = sub.add_parser("moments", help="pick clip moments for one source")
    mo.add_argument("source_id", type=int)
    sub.add_parser("ai-usage", help="AI calls and spend this month")
    rc = sub.add_parser("render", help="render one candidate clip")
    rc.add_argument("clip_id", type=int)
    rp = sub.add_parser("render-pending", help="render the best candidate clips")
    rp.add_argument("--limit", type=int, default=None)
    cc = sub.add_parser("check-clip", help="run compliance checks on one clip")
    cc.add_argument("clip_id", type=int)
    ccs = sub.add_parser("check-clips", help="run compliance checks on many clips")
    ccs.add_argument("--status", default="rendered")
    sub.add_parser("review-stats", help="today's review numbers")
    ap = sub.add_parser("approve", help="approve a rendered clip")
    ap.add_argument("clip_id", type=int)
    rj2 = sub.add_parser("reject", help="reject a clip with a reason")
    rj2.add_argument("clip_id", type=int)
    rj2.add_argument("reason", nargs="+", help="boring | 'bad cut' | 'bad captions' | off-brief | duplicate | "
                                              "quality | 'other: text'")
    ya = sub.add_parser("yt-auth", help="authorize a YouTube channel (opens a browser)")
    ya.add_argument("handle")
    yw = sub.add_parser("yt-whoami", help="show the authorized YouTube channel")
    yw.add_argument("--account", type=int, default=None)
    yu = sub.add_parser("yt-upload", help="upload an approved clip to YouTube")
    yu.add_argument("clip_id", type=int)
    yu.add_argument("--account", type=int, default=None)
    yu.add_argument("--privacy", choices=["public", "unlisted", "private"], default=None)
    sub.add_parser("accounts", help="list posting accounts")
    ic = sub.add_parser("ig-connect", help="connect an Instagram account from a short-lived user token")
    ic.add_argument("token")
    ic.add_argument("--page", default=None, help="Facebook Page name or IG username to pick")
    ik = sub.add_parser("ig-check", help="check the Instagram token and publishing quota")
    ik.add_argument("--account", type=int, default=None)
    iu = sub.add_parser("ig-upload", help="publish an approved clip as an Instagram Reel")
    iu.add_argument("clip_id", type=int)
    iu.add_argument("--account", type=int, default=None)
    aa = sub.add_parser("account-add", help="add an account without credentials (manual posting)")
    aa.add_argument("platform", choices=["youtube", "instagram", "tiktok", "x"])
    aa.add_argument("handle")
    at = sub.add_parser("account-toggle", help="activate/deactivate an account")
    at.add_argument("account_id", type=int)
    sub.add_parser("plan-posts", help="schedule approved clips into posting slots")
    sub.add_parser("execute-posts", help="publish due posts now")
    mp = sub.add_parser("mark-posted", help="record a manually published post")
    mp.add_argument("post_id", type=int)
    mp.add_argument("url")
    pg = sub.add_parser("posting", help="pause / resume / status of automatic posting")
    pg.add_argument("action", choices=["pause", "resume", "status"])
    ms = sub.add_parser("mark-submitted", help="mark a live post as submitted on Content Rewards")
    ms.add_argument("post_id", type=int)
    sub.add_parser("submit-pending", help="live posts not submitted yet")
    sub.add_parser("track-stats", help="refresh views/likes of live posts now")
    sv = sub.add_parser("set-views", help="enter views by hand (TikTok/X)")
    sv.add_argument("post_id", type=int)
    sv.add_argument("views", type=int)
    st = sub.add_parser("stats", help="performance tables")
    st.add_argument("--days", type=int, default=30)
    ae = sub.add_parser("add-earnings", help="record an actual payout for a campaign")
    ae.add_argument("campaign_id", type=int)
    ae.add_argument("amount", type=float)
    ae.add_argument("--date", default=None)
    ae.add_argument("--note", default=None)
    args = parser.parse_args(argv)
    if args.command in ("track-stats", "set-views", "stats", "add-earnings"):
        return tracker_cmd(args)
    if args.command in ("mark-submitted", "submit-pending"):
        return submit_cmd(args)
    if args.command in ("account-add", "account-toggle", "plan-posts", "execute-posts", "mark-posted", "posting"):
        return posting_cmd(args)
    if args.command in ("ig-connect", "ig-check", "ig-upload"):
        return instagram_cmd(args)
    if args.command in ("yt-auth", "yt-whoami", "yt-upload", "accounts"):
        return youtube_cmd(args)
    if args.command in ("review-stats", "approve", "reject"):
        return review_cmd(args)
    if args.command == "check-clip":
        return check_clip_cmd(args.clip_id)
    if args.command == "check-clips":
        return check_clips_cmd(args.status)
    if args.command == "render":
        return render_cmd(args.clip_id)
    if args.command == "render-pending":
        return render_pending_cmd(args.limit)
    if args.command == "moments":
        return moments_cmd(args.source_id)
    if args.command == "ai-usage":
        return ai_usage_cmd()
    if args.command == "transcribe":
        return transcribe_cmd(args.source_id)
    if args.command == "transcribe-pending":
        return transcribe_pending_cmd(args.limit)
    if args.command == "cleanup":
        return cleanup_cmd(args.dry_run)
    if args.command == "queue-status":
        return queue_status_cmd()
    if args.command == "download":
        return download_cmd(args.source_id)
    if args.command == "download-pending":
        return download_pending_cmd(args.limit)
    if args.command == "download-reset-stuck":
        return download_reset_cmd()
    if args.command == "dashboard":
        return dashboard(args.host, args.port)
    if args.command == "campaign-set":
        return campaign_set(args.id, args.status, args.notes)
    if args.command == "campaign-list":
        return campaign_list(args.status)
    if args.command == "scout-report":
        return scout_report(args.no_send)
    if args.command == "run-scheduler":
        return run_scheduler(args.force)
    if args.command == "run-job":
        return run_job(args.job)
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
