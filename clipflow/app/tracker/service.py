"""Track views/likes/comments of live posts, estimate earnings, fire milestones, learn typical views."""
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import Engine, func, select

from app.config import csv_list, settings
from app.db import get_session
from app.db.enums import PostStatus
from app.db.models import Campaign, Clip, Post, PostStat
from app.logger import get_logger
from app.notify import esc, get_notifier
from app.scout.scorer import utc
from app.uploader.common import get_setting, set_setting

log = get_logger("clipflow.tracker")

LIVE = (PostStatus.posted, PostStatus.submitted)
CADENCE = ((timedelta(hours=48), timedelta(hours=2)), (timedelta(days=7), timedelta(hours=12)))
SLOW_CADENCE = timedelta(hours=24)
STAT_ROW_EVERY = timedelta(hours=24)
TYPICAL_KEY = "typical_views"
LEARN_MIN_AGE = timedelta(days=3)
LEARN_MIN_SAMPLES = 10


def milestones() -> list[int]:
    out = []
    for v in csv_list(settings.VIEW_MILESTONES):
        try:
            out.append(int(float(v)))
        except ValueError:
            continue
    return sorted(set(out))


def short_views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}M"
    if n >= 1000:
        return f"{n / 1000:g}K"
    return str(n)


def check_interval(age: timedelta) -> timedelta:
    for limit, every in CADENCE:
        if age < limit:
            return every
    return SLOW_CADENCE


def due_posts(session, now: datetime) -> list[Post]:
    """Live posts within TRACK_DAYS whose next check is due (2h <48h, 12h <7d, else 24h)."""
    since = now - timedelta(days=settings.TRACK_DAYS)
    rows = session.scalars(select(Post).where(Post.status.in_(LIVE), Post.posted_at >= since)
                           .order_by(Post.posted_at)).all()
    due = []
    for p in rows:
        age = now - utc(p.posted_at)
        last = utc(p.last_checked_at)
        if last is None or now - last >= check_interval(age):
            due.append(p)
    return due


def estimate_earnings(post: Post, campaign: Optional[Campaign]) -> float:
    """CPM estimate: views/1000*cpm; 0 below min_payout; capped at max_payout_per_clip. Non-CPM → 0."""
    if campaign is None or (campaign.payout_type or "cpm") != "cpm" or not campaign.cpm_rate:
        return 0.0
    raw = (post.views or 0) / 1000 * campaign.cpm_rate
    if campaign.min_payout and raw < campaign.min_payout:
        return 0.0
    if campaign.max_payout_per_clip is not None:
        raw = min(raw, campaign.max_payout_per_clip)
    return round(raw, 2)


def _apply(session, post: Post, views: int, likes: Optional[int], comments: Optional[int], now: datetime,
           notifier) -> bool:
    """Write stats, maybe a post_stats row, earnings and milestones. Returns True if views changed."""
    changed = (post.views or 0) != views
    post.views, post.likes, post.comments = views, likes, comments
    post.last_checked_at, post.stats_error = now, None
    last_row = session.scalar(select(PostStat).where(PostStat.post_id == post.id)
                              .order_by(PostStat.checked_at.desc()).limit(1))
    if last_row is None or last_row.views != views or now - utc(last_row.checked_at) >= STAT_ROW_EVERY:
        session.add(PostStat(post_id=post.id, views=views, checked_at=now))
    clip = session.get(Clip, post.clip_id)
    campaign = session.get(Campaign, clip.campaign_id) if clip else None
    post.earnings_est = estimate_earnings(post, campaign)
    crossed = [m for m in milestones() if views >= m > (post.milestone_hit or 0)]
    if crossed:
        top = max(crossed)
        post.milestone_hit = top
        if notifier.enabled:
            try:
                notifier.send_message(f"🔥 #{post.id} hit {short_views(top)} views on {esc(post.platform.value)} — "
                                      f"{esc(campaign.name if campaign else '')}")
            except Exception as exc:  # noqa: BLE001
                log.warning("milestone notify failed: %s", type(exc).__name__)
    return changed


def _default_fetchers() -> dict[str, Callable]:
    from app.tracker.fetchers import instagram_stats, youtube_stats
    return {"youtube": youtube_stats, "instagram": instagram_stats}


def track_due(now: Optional[datetime] = None, engine: Optional[Engine] = None,
              fetchers: Optional[dict[str, Callable]] = None, notifier=None) -> dict:
    """Fetch stats for due posts (API platforms only) and store them."""
    now = utc(now) or datetime.now(timezone.utc)
    fetchers = fetchers if fetchers is not None else _default_fetchers()
    notifier = notifier or get_notifier()
    summary = {"checked": 0, "updated": 0, "errors": 0}
    with get_session(engine) as s:
        due = due_posts(s, now)
        by_platform: dict[str, list[Post]] = {}
        for p in due:
            if p.platform.value in fetchers:
                by_platform.setdefault(p.platform.value, []).append(p)
        s.expunge_all()
    for platform, plist in by_platform.items():
        try:
            results = fetchers[platform](plist, engine=engine)
        except Exception as exc:  # noqa: BLE001
            log.error("%s stats fetch crashed: %s", platform, type(exc).__name__)
            results = {p.id: {"error": f"fetch failed: {type(exc).__name__}"} for p in plist}
        with get_session(engine) as s:
            for p in plist:
                post = s.get(Post, p.id)
                res = results.get(p.id) or {"error": "no result"}
                summary["checked"] += 1
                if "error" in res:
                    post.stats_error, post.last_checked_at = res["error"][:255], now
                    summary["errors"] += 1
                    continue
                if res.get("platform_post_id") and not post.platform_post_id:
                    post.platform_post_id = res["platform_post_id"]
                summary["updated"] += _apply(s, post, int(res.get("views") or 0), res.get("likes"),
                                             res.get("comments"), now, notifier)
    if summary["checked"]:
        log.info("stats: %s", summary)
    return summary


def set_manual_views(post_id: int, views: int, engine: Optional[Engine] = None, now: Optional[datetime] = None,
                     notifier=None) -> tuple[bool, str]:
    """Record views entered by hand (TikTok/X or any post)."""
    if views < 0:
        return False, "views must be ≥ 0"
    now = utc(now) or datetime.now(timezone.utc)
    with get_session(engine) as s:
        post = s.get(Post, post_id)
        if post is None:
            return False, f"post #{post_id} not found"
        if post.status not in LIVE:
            return False, f"post #{post_id} is {post.status.value}, not live"
        _apply(s, post, views, post.likes, post.comments, now, notifier or get_notifier())
        est = post.earnings_est
    return True, f"Post #{post_id}: {views:,} views (est ${est:.2f})"


def learn_typical_views(engine: Optional[Engine] = None, now: Optional[datetime] = None) -> dict:
    """Median views per platform of posts aged 3–TRACK_DAYS days; stored when ≥10 samples."""
    now = utc(now) or datetime.now(timezone.utc)
    with get_session(engine) as s:
        rows = s.execute(select(Post.platform, Post.views).where(
            Post.status.in_(LIVE), Post.stats_error.is_(None), Post.views.is_not(None),
            Post.posted_at <= now - LEARN_MIN_AGE, Post.posted_at >= now - timedelta(days=settings.TRACK_DAYS))).all()
        samples: dict[str, list[int]] = {}
        for platform, views in rows:
            samples.setdefault(platform.value, []).append(views)
        learned = {p: int(statistics.median(v)) for p, v in samples.items() if len(v) >= LEARN_MIN_SAMPLES}
        if learned:
            current = json.loads(get_setting(s, TYPICAL_KEY) or "{}")
            current.update(learned)
            set_setting(s, TYPICAL_KEY, json.dumps(current))
    if learned:
        log.info("learned typical views: %s", learned)
    return learned


def week_summary(session, now: Optional[datetime] = None) -> dict:
    """Small 7-day digest for the weekly report."""
    from app.tracker.analytics import compute

    data = compute(session, 7, now)
    best_campaign = max(data["by_campaign"], key=lambda r: r["est_per_post"], default=None)
    best_platform = max(data["by_platform"], key=lambda r: r["avg_views"], default=None)
    best_hour = max(data["by_slot_hour"], key=lambda r: r["avg_views"], default=None)
    learned = json.loads(get_setting(session, TYPICAL_KEY) or "{}")
    return {"totals": data["totals"], "best_campaign": best_campaign, "best_platform": best_platform,
            "best_hour": best_hour, "learned": learned}


def count_posts(session, since: datetime) -> int:
    return session.scalar(select(func.count()).select_from(Post).where(Post.status.in_(LIVE),
                                                                      Post.posted_at >= since)) or 0
