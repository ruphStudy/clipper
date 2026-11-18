"""APScheduler jobs: daily scout and weekly report."""
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.logger import get_logger
from app.notify import notify_error
from app.scout.schema import ScoutError

log = get_logger("clipflow.scheduler")

JOB_DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}


def job_daily_scout() -> None:
    """Fetch + score campaigns; never raises."""
    from app.db import init_db
    from app.scout.service import run_scout

    try:
        init_db()
        r = run_scout("auto")
        log.info("daily scout ok: fetched=%d new=%d ended=%d", r.fetched, r.new, r.ended)
    except ScoutError:
        pass  # already logged and alerted by run_scout
    except Exception as exc:  # noqa: BLE001
        log.exception("daily scout crashed")
        notify_error("daily_scout", exc)


def job_weekly_report() -> None:
    """Send the weekly report; never raises."""
    from app.scout.report import send_weekly_report

    try:
        send_weekly_report()
    except Exception as exc:  # noqa: BLE001
        log.exception("weekly report crashed")
        notify_error("weekly_report", exc)


def job_download_queue() -> None:
    """Every 20 min: recover stuck rows, re-queue due retries, download a few; never raises."""
    from app.downloader.service import download_pending, reset_stuck, retry_due

    try:
        reset_stuck()
        retry_due()
        summary = download_pending(limit=3)
        log.info("download queue: %s", summary)
    except Exception as exc:  # noqa: BLE001
        log.exception("download queue crashed")
        notify_error("download_queue", exc)


def job_cleanup() -> None:
    """Daily disk cleanup; never raises."""
    from app.downloader.cleanup import run_cleanup

    try:
        run_cleanup()
    except Exception as exc:  # noqa: BLE001
        log.exception("cleanup crashed")
        notify_error("cleanup", exc)


def job_transcribe() -> None:
    """Every 15 min: transcribe one downloaded source; never raises."""
    from app.clipper.transcribe_service import transcribe_pending

    try:
        summary = transcribe_pending(limit=1)
        if summary["done"] or summary["failed"]:
            log.info("transcribe: %s", summary)
    except Exception as exc:  # noqa: BLE001
        log.exception("transcribe job crashed")
        notify_error("transcribe", exc)


def job_moments() -> None:
    """Every 15 min: pick moments for up to 3 transcribed sources; never raises."""
    from app.clipper.moments_service import pick_pending

    try:
        summary = pick_pending(limit=3)
        if summary["sources"]:
            log.info("moments: %s", summary)
    except Exception as exc:  # noqa: BLE001
        log.exception("moments job crashed")
        notify_error("moments", exc)


def job_render() -> None:
    """Every 10 min: render a few candidate clips; never raises."""
    from app.clipper.render import render_pending

    try:
        summary = render_pending()
        if summary["rendered"] or summary["failed"]:
            log.info("render: %s", summary)
    except Exception as exc:  # noqa: BLE001
        log.exception("render job crashed")
        notify_error("render", exc)


def job_review_digest() -> bool:
    """Daily: tell Telegram how many clips wait for review (only if any). Returns True if sent."""
    from app.notify import notify
    from app.review.service import stats_today

    try:
        st = stats_today()
        if not st["waiting"]:
            return False
        body = f"Approved today: {st['approved']} · Rejected: {st['rejected']}"
        if settings.DASHBOARD_PUBLIC_URL:
            body += f"\n{settings.DASHBOARD_PUBLIC_URL.rstrip('/')}/review"
        return bool(notify(f"🎬 {st['waiting']} clips waiting for review", body))
    except Exception as exc:  # noqa: BLE001
        log.exception("review digest crashed")
        notify_error("review_digest", exc)
        return False


def job_plan_posts() -> None:
    """Every 30 min: put approved clips into posting slots; never raises."""
    from app.posting.planner import plan_posts

    try:
        n = plan_posts()
        if n:
            log.info("planned %d posts", n)
    except Exception as exc:  # noqa: BLE001
        log.exception("plan posts crashed")
        notify_error("plan_posts", exc)


def job_execute_posts() -> None:
    """Every 5 min: publish due posts / hand off manual ones; never raises."""
    from app.posting.executor import execute_due

    try:
        summary = execute_due()
        if any(v for k, v in summary.items() if k != "paused"):
            log.info("execute posts: %s", summary)
    except Exception as exc:  # noqa: BLE001
        log.exception("execute posts crashed")
        notify_error("execute_posts", exc)


def job_telegram_commands() -> None:
    """Every minute: handle owner commands sent to the bot (no-op when Telegram is disabled)."""
    from app.notify import get_notifier
    from app.notify.commands import poll_commands

    try:
        if get_notifier().enabled:
            poll_commands()
    except Exception as exc:  # noqa: BLE001
        log.exception("telegram commands crashed: %s", type(exc).__name__)


def job_submit_deadlines() -> None:
    """Every minute: submit reminders / 'window passed' messages; never raises."""
    from app.posting.submit import check_submit_deadlines

    try:
        out = check_submit_deadlines()
        if out["reminded"] or out["missed"]:
            log.info("submit deadlines: %s", out)
    except Exception as exc:  # noqa: BLE001
        log.exception("submit deadlines crashed: %s", type(exc).__name__)


def job_track_stats() -> None:
    """Every 30 min: refresh views of live posts; never raises."""
    from app.tracker.service import track_due

    try:
        track_due()
    except Exception as exc:  # noqa: BLE001
        log.exception("track stats crashed")
        notify_error("track_stats", exc)


def job_learn() -> None:
    """Daily: learn typical views from real posts, then re-score campaigns; never raises."""
    from app.db import get_session
    from app.scout.scorer import score_all
    from app.tracker.service import learn_typical_views

    try:
        learn_typical_views()
        with get_session() as s:
            score_all(s)
    except Exception as exc:  # noqa: BLE001
        log.exception("learn job crashed")
        notify_error("learn", exc)


JOBS = {"daily_scout": job_daily_scout, "weekly_report": job_weekly_report,
        "download_queue": job_download_queue, "cleanup": job_cleanup, "transcribe": job_transcribe,
        "moments": job_moments, "render": job_render, "review_digest": job_review_digest,
        "plan_posts": job_plan_posts, "execute_posts": job_execute_posts, "telegram_commands": job_telegram_commands,
        "submit_deadlines": job_submit_deadlines, "track_stats": job_track_stats, "learn": job_learn}


def build_scheduler() -> BlockingScheduler:
    """Scout 08:40 + 20:40, report Mon 09:10, download queue every 20 min, cleanup 03:20, transcribe + moments every 15 min, render every 10 min, review digest 19:10, posting (plan 30 min, execute 5 min), bot commands 1 min."""
    tz = settings.TIMEZONE
    sched = BlockingScheduler(timezone=tz, job_defaults=JOB_DEFAULTS)
    sched.add_job(job_daily_scout, CronTrigger(hour=8, minute=40, timezone=tz), id="daily_scout_am",
                  name="daily scout (morning)", **JOB_DEFAULTS)
    sched.add_job(job_daily_scout, CronTrigger(hour=20, minute=40, timezone=tz), id="daily_scout_pm",
                  name="daily scout (evening)", **JOB_DEFAULTS)
    sched.add_job(job_weekly_report, CronTrigger(day_of_week="mon", hour=9, minute=10, timezone=tz),
                  id="weekly_report", name="weekly report", **JOB_DEFAULTS)
    sched.add_job(job_download_queue, IntervalTrigger(minutes=20, timezone=tz), id="download_queue",
                  name="download queue", **JOB_DEFAULTS)
    sched.add_job(job_cleanup, CronTrigger(hour=3, minute=20, timezone=tz), id="cleanup",
                  name="disk cleanup", **JOB_DEFAULTS)
    sched.add_job(job_transcribe, IntervalTrigger(minutes=15, timezone=tz), id="transcribe",
                  name="transcribe", **JOB_DEFAULTS)
    sched.add_job(job_moments, IntervalTrigger(minutes=15, timezone=tz), id="moments",
                  name="moment picker", **JOB_DEFAULTS)
    sched.add_job(job_render, IntervalTrigger(minutes=10, timezone=tz), id="render",
                  name="clip renderer", **JOB_DEFAULTS)
    sched.add_job(job_review_digest, CronTrigger(hour=19, minute=10, timezone=tz), id="review_digest",
                  name="review digest", **JOB_DEFAULTS)
    sched.add_job(job_plan_posts, IntervalTrigger(minutes=30, timezone=tz), id="plan_posts",
                  name="plan posts", **JOB_DEFAULTS)
    sched.add_job(job_execute_posts, IntervalTrigger(minutes=5, timezone=tz), id="execute_posts",
                  name="execute posts", **JOB_DEFAULTS)
    sched.add_job(job_telegram_commands, IntervalTrigger(minutes=1, timezone=tz), id="telegram_commands",
                  name="telegram commands", **JOB_DEFAULTS)
    sched.add_job(job_submit_deadlines, IntervalTrigger(minutes=1, timezone=tz), id="submit_deadlines",
                  name="submit deadlines", **JOB_DEFAULTS)
    sched.add_job(job_track_stats, IntervalTrigger(minutes=30, timezone=tz), id="track_stats",
                  name="track stats", **JOB_DEFAULTS)
    sched.add_job(job_learn, CronTrigger(hour=4, minute=10, timezone=tz), id="learn",
                  name="learn typical views + rescore", **JOB_DEFAULTS)
    return sched


def job_heartbeat() -> None:
    """Every 60 s: refresh the single-instance heartbeat; on macOS also check power (every 10 min)."""
    from app.scheduler.lock import write_heartbeat
    from app.scheduler.power import check_power

    try:
        write_heartbeat()
        check_power()
    except Exception as exc:  # noqa: BLE001
        log.warning("heartbeat failed: %s", type(exc).__name__)


def add_heartbeat_job(sched: BlockingScheduler) -> None:
    """Infrastructure job (not part of build_scheduler's business jobs)."""
    sched.add_job(job_heartbeat, IntervalTrigger(seconds=60, timezone=settings.TIMEZONE), id="heartbeat",
                  name="heartbeat", **JOB_DEFAULTS)


def next_runs(sched: BlockingScheduler, now: datetime | None = None) -> list[tuple[str, datetime]]:
    """Next fire time of every job (works before the scheduler starts)."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(ZoneInfo(settings.TIMEZONE))
    return [(job.id, job.trigger.get_next_fire_time(None, now)) for job in sched.get_jobs()]
