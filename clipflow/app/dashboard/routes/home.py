"""Home page and global actions."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import csv_list, settings
from app.dashboard.deps import get_db, get_engine_for, redirect, render
from app.db.enums import CampaignStatus, ClipStatus, SourceStatus
from app.db.models import Campaign, Clip, Post, Source
from app.logger import get_logger
from app.scout import service as scout_service
from app.scout.report import get_last_report_at

router = APIRouter()
log = get_logger("clipflow.dashboard")


def _count(session: Session, stmt) -> int:
    return session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    types = csv_list(settings.MY_CAMPAIGN_TYPES)
    not_ended = Campaign.status != CampaignStatus.ended
    live_q = select(Campaign.id).where(not_ended, or_(Campaign.is_live.is_(None), Campaign.is_live.is_(True)))
    if types:
        live_q = live_q.where(func.lower(Campaign.campaign_type).in_(types))
    tz = ZoneInfo(settings.TIMEZONE)
    day_start = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz).astimezone(timezone.utc)
    stats = [
        ("Live clipping campaigns", _count(db, live_q)),
        ("Scored > 0", _count(db, select(Campaign.id).where(not_ended, Campaign.score > 0))),
        ("Selected", _count(db, select(Campaign.id).where(Campaign.status == CampaignStatus.selected))),
        ("Joined", _count(db, select(Campaign.id).where(Campaign.status == CampaignStatus.joined))),
        ("Sources pending", _count(db, select(Source.id).where(Source.status == SourceStatus.pending))),
        ("Clips awaiting review", _count(db, select(Clip.id).where(Clip.status == ClipStatus.rendered))),
        ("Posts today", _count(db, select(Post.id).where(Post.posted_at >= day_start))),
    ]
    return render(request, "home.html", stats=stats,
                  last_scout=db.scalar(select(func.max(Campaign.last_seen_at))),
                  last_report=get_last_report_at(db), nav="home")


@router.post("/actions/scout")
def action_scout(request: Request):
    try:
        r = scout_service.run_scout("auto", engine=get_engine_for(request))
        msg = f"Scout done: fetched {r.fetched}, new {r.new}"
    except Exception as exc:  # noqa: BLE001
        log.error("dashboard scout failed: %s", exc)
        msg = f"Scout failed: {type(exc).__name__}: {str(exc)[:200]}"
    return redirect("/", msg)
