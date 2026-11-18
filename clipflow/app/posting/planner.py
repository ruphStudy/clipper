"""Plan approved clips into posting slots across platforms and accounts."""
import random
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.db.enums import ClipStatus, Platform, PostStatus
from app.db.models import Account, Campaign, Clip, Post
from app.logger import get_logger
from app.posting.modes import in_submit_window, is_paused, post_mode, slots
from app.rules import get_rules
from app.scout.scorer import utc
from app.uploader.common import get_setting

log = get_logger("clipflow.planner")

LEAD = timedelta(minutes=10)
CROSS_PLATFORM_GAP = timedelta(minutes=30)
LIVE_STATUSES = (PostStatus.scheduled, PostStatus.uploading, PostStatus.manual_pending, PostStatus.posted,
                 PostStatus.submitted)


@dataclass
class Booked:
    """A post occupying a time (planned or done)."""

    account_id: int
    campaign_id: int
    clip_id: int
    platform: str
    at: datetime


def _local_day(dt: datetime, tz: ZoneInfo) -> date:
    return dt.astimezone(tz).date()


def _blocked(session: Session, account: Account, now: datetime) -> bool:
    keys = {"youtube": "youtube_quota_block_until", "instagram": f"ig_block_until_{account.id}"}
    raw = get_setting(session, keys.get(account.platform.value, "")) if account.platform.value in keys else None
    if not raw:
        return False
    try:
        return now < datetime.fromisoformat(raw)
    except ValueError:
        return False


def usable_accounts(session: Session, platform: str, mode: str, now: datetime) -> list[Account]:
    """Active accounts able to post on a platform in the given mode."""
    accts = session.scalars(select(Account).where(Account.platform == Platform(platform), Account.is_active.is_(True))
                            .order_by(Account.id)).all()
    if mode == "manual":
        return list(accts)
    return [a for a in accts if a.credentials_ref and not a.auth_error and not _blocked(session, a, now)]


def _round_robin(clips: list[Clip]) -> list[Clip]:
    """Interleave campaigns (each campaign keeps its oldest-approved-first order)."""
    groups: "OrderedDict[int, list[Clip]]" = OrderedDict()
    for c in clips:
        groups.setdefault(c.campaign_id, []).append(c)
    out: list[Clip] = []
    while any(groups.values()):
        for cid in list(groups):
            if groups[cid]:
                out.append(groups[cid].pop(0))
    return out


def jittered(slot_dt: datetime, seed: str) -> datetime:
    """Slot time ± SLOT_JITTER_MIN minutes, deterministic for a given seed."""
    j = settings.SLOT_JITTER_MIN
    offset = random.Random(seed).randint(-j, j) if j > 0 else 0
    return slot_dt + timedelta(minutes=offset)


def _candidate_times(platform: str, clip_id: int, account_id: int, now: datetime, tz: ZoneInfo,
                     days: Optional[int] = None) -> list[datetime]:
    """Jittered slot times inside the submit window (incl. the 15-min margin), soonest first."""
    today = now.astimezone(tz).date()
    out = []
    for d in range(settings.PLAN_DAYS_AHEAD + 1 if days is None else days):
        day = today + timedelta(days=d)
        for t in slots(platform):
            base = datetime.combine(day, t, tzinfo=tz)
            at = jittered(base, f"{clip_id}:{platform}:{account_id}:{day.isoformat()}:{t}").astimezone(timezone.utc)
            if in_submit_window(at):
                out.append(at)
    return sorted(out)


def next_valid_slot(platform: str, clip_id: int, account_id: int, now: datetime) -> datetime:
    """Next slot after now (+lead) that is inside the submit window; falls back to +1 day."""
    tz = ZoneInfo(settings.TIMEZONE)
    for at in _candidate_times(platform, clip_id, account_id, now, tz, days=8):
        if at > now + LEAD:
            return at
    return now + timedelta(days=1)


def _fits(at: datetime, account: Account, clip: Clip, platform: str, max_campaign_per_day: int, booked: list[Booked],
          now: datetime, tz: ZoneInfo) -> bool:
    if at <= now + LEAD:
        return False
    day = _local_day(at, tz)
    gap = timedelta(minutes=settings.MIN_GAP_MIN)
    acct_day = camp_day = 0
    for b in booked:
        same_day = _local_day(b.at, tz) == day
        if b.account_id == account.id:
            if abs(b.at - at) < gap:
                return False
            acct_day += same_day
        if b.campaign_id == clip.campaign_id and same_day:
            camp_day += 1
        if b.clip_id == clip.id and b.platform != platform and abs(b.at - at) < CROSS_PLATFORM_GAP:
            return False
    return acct_day < (account.daily_post_limit or 1) and camp_day < max_campaign_per_day


def _load_booked(session: Session, now: datetime) -> list[Booked]:
    horizon_lo = now - timedelta(days=1)
    rows = session.execute(
        select(Post, Clip.campaign_id).join(Clip, Post.clip_id == Clip.id)
        .where(Post.status.in_(LIVE_STATUSES),
               or_(Post.scheduled_at >= horizon_lo, Post.posted_at >= horizon_lo))).all()
    out = []
    for post, campaign_id in rows:
        at = utc(post.posted_at) if post.status in (PostStatus.posted, PostStatus.submitted) and post.posted_at \
            else utc(post.scheduled_at) or utc(post.posted_at)
        if at is not None:
            out.append(Booked(post.account_id, campaign_id, post.clip_id, post.platform.value, at))
    return out


def plan_posts(now: Optional[datetime] = None, engine: Optional[Engine] = None) -> int:
    """Create scheduled Posts for approved clips; returns how many posts were created."""
    now = utc(now) or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.TIMEZONE)
    created = 0
    with get_session(engine) as s:
        if is_paused(s):
            log.info("posting paused: nothing planned")
            return 0
        clips = s.scalars(select(Clip).where(Clip.status == ClipStatus.approved)
                          .order_by(Clip.approved_at, Clip.id)).all()
        booked = _load_booked(s, now)
        mode_cache: dict[str, str] = {}
        acct_cache: dict[str, list[Account]] = {}
        for clip in _round_robin(list(clips)):
            rules = get_rules(s.get(Campaign, clip.campaign_id))
            existing = {p.platform.value for p in s.scalars(select(Post).where(Post.clip_id == clip.id))}
            targets = []
            for platform in rules.post_platforms:
                if platform not in mode_cache:
                    mode_cache[platform] = post_mode(platform, s)
                    acct_cache[platform] = usable_accounts(s, platform, mode_cache[platform], now)
                if acct_cache[platform] and slots(platform):
                    targets.append(platform)
            if not targets:
                continue
            for platform in targets:
                if platform in existing:
                    continue
                best: Optional[tuple[datetime, Account]] = None
                for account in acct_cache[platform]:
                    for at in _candidate_times(platform, clip.id, account.id, now, tz):
                        if _fits(at, account, clip, platform, rules.max_posts_per_day, booked, now, tz):
                            if best is None or at < best[0]:
                                best = (at, account)
                            break
                if best is None:
                    continue
                at, account = best
                s.add(Post(clip_id=clip.id, account_id=account.id, platform=Platform(platform),
                           status=PostStatus.scheduled, scheduled_at=at, mode=mode_cache[platform], attempts=0))
                booked.append(Booked(account.id, clip.campaign_id, clip.id, platform, at))
                existing.add(platform)
                created += 1
            if all(p in existing for p in targets):
                clip.status = ClipStatus.scheduled
        s.flush()
    if created:
        log.info("planned %d posts", created)
    return created
