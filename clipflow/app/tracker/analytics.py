"""Performance analytics over live posts (pure: reads the DB, returns plain dicts)."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.enums import PostStatus
from app.db.models import Account, Campaign, CampaignEarning, Clip, Post, Source
from app.scout.scorer import utc

LIVE = (PostStatus.posted, PostStatus.submitted)
FLOP_VIEWS = 500
FLOP_AGE = timedelta(days=3)


def hook_bucket(hook: Optional[str]) -> str:
    n = len(hook or "")
    return "≤30" if n <= 30 else "31–45" if n <= 45 else "46+"


def _group(rows: list[dict], key: Callable[[dict], object], label: str) -> list[dict]:
    groups: dict = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for k, items in groups.items():
        views = sum(r["views"] for r in items)
        est = sum(r["est"] for r in items)
        out.append({label: k, "posts": len(items), "views": views, "avg_views": round(views / len(items)),
                    "est": round(est, 2), "est_per_post": round(est / len(items), 2),
                    "hit_rate": round(100 * sum(1 for r in items if r["est"] > 0) / len(items), 1)})
    return sorted(out, key=lambda r: (-r["views"], str(r[label])))


def compute(session: Session, days: int = 30, now: Optional[datetime] = None) -> dict:
    """Totals and breakdowns for posts that went live in the last `days` days."""
    now = utc(now) or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    tz = ZoneInfo(settings.TIMEZONE)
    q = (select(Post, Clip, Campaign, Account, Source.moments_method)
         .join(Clip, Post.clip_id == Clip.id).join(Campaign, Clip.campaign_id == Campaign.id)
         .join(Account, Post.account_id == Account.id).outerjoin(Source, Clip.source_id == Source.id)
         .where(Post.status.in_(LIVE), Post.posted_at >= start))
    rows = []
    for post, clip, campaign, account, method in session.execute(q):
        posted = utc(post.posted_at)
        rows.append({"id": post.id, "platform": post.platform.value, "account": f"{post.platform.value} {account.handle}",
                     "campaign": campaign.name, "campaign_id": campaign.id, "views": post.views or 0,
                     "est": float(post.earnings_est or 0), "hour": posted.astimezone(tz).hour, "posted_at": posted,
                     "layout": clip.layout or "?", "method": method or "?", "hook": hook_bucket(clip.hook_title),
                     "url": post.post_url, "likes": post.likes})
    actual_rows = session.execute(select(CampaignEarning.campaign_id, Campaign.name, func.sum(CampaignEarning.amount_usd))
                                  .join(Campaign, CampaignEarning.campaign_id == Campaign.id)
                                  .where(CampaignEarning.period_end >= start.date())
                                  .group_by(CampaignEarning.campaign_id, Campaign.name)).all()
    actual = {name: float(total) for _cid, name, total in actual_rows}

    by_campaign = _group(rows, lambda r: r["campaign"], "campaign")
    names = {r["campaign"] for r in by_campaign}
    for r in by_campaign:
        r["actual"] = round(actual.get(r["campaign"], 0.0), 2)
    for name, amount in actual.items():  # payouts for campaigns without posts in range
        if name not in names:
            by_campaign.append({"campaign": name, "posts": 0, "views": 0, "avg_views": 0, "est": 0.0,
                                "est_per_post": 0.0, "hit_rate": 0.0, "actual": round(amount, 2)})
    by_hour = sorted(_group(rows, lambda r: r["hour"], "hour"), key=lambda r: r["hour"])
    aged = [r for r in rows if now - r["posted_at"] >= FLOP_AGE]
    return {
        "days": days,
        "totals": {"posts": len(rows), "views": sum(r["views"] for r in rows),
                   "est": round(sum(r["est"] for r in rows), 2), "actual": round(sum(actual.values()), 2)},
        "by_campaign": by_campaign,
        "by_platform": _group(rows, lambda r: r["platform"], "platform"),
        "by_account": _group(rows, lambda r: r["account"], "account"),
        "by_slot_hour": by_hour,
        "by_layout": _group(rows, lambda r: r["layout"], "layout"),
        "by_method": _group(rows, lambda r: r["method"], "method"),
        "by_hook_len": sorted(_group(rows, lambda r: r["hook"], "hook_len"),
                              key=lambda r: ["≤30", "31–45", "46+"].index(r["hook_len"])),
        "top_posts": sorted(rows, key=lambda r: -r["views"])[:10],
        "flop_rate": round(100 * sum(1 for r in aged if r["views"] < FLOP_VIEWS) / len(aged), 1) if aged else None,
        "est_vs_actual": [{"campaign": r["campaign"], "est": r["est"], "actual": r["actual"],
                           "ratio": round(r["actual"] / r["est"], 2) if r["est"] else None}
                          for r in by_campaign if r["actual"] > 0],
        "rows": rows,
    }
