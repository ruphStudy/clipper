"""APScheduler jobs: daily scout and weekly report."""
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

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


JOBS = {"daily_scout": job_daily_scout, "weekly_report": job_weekly_report}


def build_scheduler() -> BlockingScheduler:
    """Scheduler with scout at 08:40 + 20:40 daily and report Mondays 09:10."""
    tz = settings.TIMEZONE
    sched = BlockingScheduler(timezone=tz, job_defaults=JOB_DEFAULTS)
    sched.add_job(job_daily_scout, CronTrigger(hour=8, minute=40, timezone=tz), id="daily_scout_am",
                  name="daily scout (morning)", **JOB_DEFAULTS)
    sched.add_job(job_daily_scout, CronTrigger(hour=20, minute=40, timezone=tz), id="daily_scout_pm",
                  name="daily scout (evening)", **JOB_DEFAULTS)
    sched.add_job(job_weekly_report, CronTrigger(day_of_week="mon", hour=9, minute=10, timezone=tz),
                  id="weekly_report", name="weekly report", **JOB_DEFAULTS)
    return sched


def next_runs(sched: BlockingScheduler, now: datetime | None = None) -> list[tuple[str, datetime]]:
    """Next fire time of every job (works before the scheduler starts)."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(ZoneInfo(settings.TIMEZONE))
    return [(job.id, job.trigger.get_next_fire_time(None, now)) for job in sched.get_jobs()]
