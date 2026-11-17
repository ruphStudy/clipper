"""Rule-based campaign scorer (0-100, offline, no AI)."""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import csv_list, settings
from app.db.enums import CampaignStatus
from app.db.models import Campaign, CampaignSnapshot
from app.logger import get_logger

log = get_logger("clipflow.scorer")

BURN_WINDOW = timedelta(days=7)
MIN_BURN_SPAN = timedelta(hours=12)
FRESH_FUNDING = timedelta(days=14)
MAX_REASONS = 4


@dataclass
class ScoreConfig:
    """Scoring knobs (defaults come from settings)."""

    platforms: list[str] = field(default_factory=lambda: csv_list(settings.MY_PLATFORMS))
    niches: list[str] = field(default_factory=lambda: csv_list(settings.MY_NICHES))
    campaign_types: list[str] = field(default_factory=lambda: csv_list(settings.MY_CAMPAIGN_TYPES))
    typical_views: float = field(default_factory=lambda: float(settings.TYPICAL_VIEWS))
    min_cpm: float = field(default_factory=lambda: float(settings.MIN_CPM))
    min_budget_left: float = field(default_factory=lambda: float(settings.MIN_BUDGET_LEFT))
    min_days_left: float = field(default_factory=lambda: float(settings.MIN_DAYS_LEFT))


@dataclass
class ScoreResult:
    """Outcome of scoring one campaign."""

    score: float
    breakdown: dict
    reasons: list[str]
    est_per_clip: float
    burn_per_day: Optional[float]
    days_left: Optional[float]


def utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Treat naive datetimes (SQLite) as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def burn_rate(campaign: Campaign, snapshots: Iterable[CampaignSnapshot],
              now: Optional[datetime] = None) -> tuple[Optional[float], Optional[float]]:
    """Return (burn_per_day, days_left) from the oldest snapshot in the last 7 days."""
    now = utc(now) or datetime.now(timezone.utc)
    recent = [s for s in snapshots
              if s.budget_spent is not None and utc(s.captured_at) is not None
              and now - utc(s.captured_at) <= BURN_WINDOW]
    if not recent or campaign.budget_spent is None:
        return None, None
    oldest = min(recent, key=lambda s: utc(s.captured_at))
    newest_points = [(utc(s.captured_at), s.budget_spent) for s in recent]
    if campaign.last_seen_at is not None:
        newest_points.append((utc(campaign.last_seen_at), campaign.budget_spent))
    t_now, spent_now = max(newest_points, key=lambda p: p[0])
    span = t_now - utc(oldest.captured_at)
    if span < MIN_BURN_SPAN:
        return None, None
    burn = (spent_now - oldest.budget_spent) / (span.total_seconds() / 86400)
    burn = round(burn, 2)
    if burn <= 0 or campaign.budget_left is None:
        return burn, None
    return burn, round(campaign.budget_left / burn, 1)


def est_per_clip(campaign: Campaign, cfg: Optional[ScoreConfig] = None) -> float:
    """Expected $ per clip at TYPICAL_VIEWS."""
    cfg = cfg or ScoreConfig()
    if campaign.cpm_rate is None:
        return 0.0
    est = cfg.typical_views / 1000 * campaign.cpm_rate
    if campaign.min_payout is not None and est < campaign.min_payout:
        return 0.0
    if campaign.max_payout_per_clip is not None:
        est = min(est, campaign.max_payout_per_clip)
    return round(est, 2)


def _niche_match(campaign: Campaign, niches: list[str]) -> bool:
    hay = " ".join(x for x in (campaign.name, campaign.brand, campaign.description) if x).lower()
    return any(k in hay for k in niches)


def score_campaign(campaign: Campaign, snapshots: Iterable[CampaignSnapshot] = (),
                   cfg: Optional[ScoreConfig] = None, now: Optional[datetime] = None) -> ScoreResult:
    """Score one campaign (pure; does not modify it)."""
    cfg = cfg or ScoreConfig()
    now = utc(now) or datetime.now(timezone.utc)
    burn, days_left = burn_rate(campaign, snapshots, now)
    est = est_per_clip(campaign, cfg)
    platforms = {p.lower() for p in (campaign.platforms or [])}
    overlap = [p for p in cfg.platforms if p in platforms]
    budget_left = campaign.budget_left or 0.0

    filters: list[str] = []
    if campaign.is_live is False or campaign.status == CampaignStatus.ended:
        filters.append("ended / not live")
    if cfg.campaign_types and (campaign.campaign_type or "").lower() not in cfg.campaign_types:
        filters.append("not a clipping campaign")
    if cfg.platforms and not overlap:
        filters.append("not on your platforms")
    if budget_left < cfg.min_budget_left:
        filters.append(f"low budget left (${budget_left:,.0f})")
    if campaign.payout_type == "cpm" and campaign.cpm_rate is not None and campaign.cpm_rate < cfg.min_cpm:
        filters.append(f"CPM below minimum (${campaign.cpm_rate:g})")
    if days_left is not None and days_left < cfg.min_days_left:
        filters.append(f"budget almost gone: ~{days_left:g} days left")
    if filters:
        return ScoreResult(0.0, {}, filters[:MAX_REASONS], est, burn, days_left)

    creators = campaign.creators_count
    bd = {
        "earnings": min(est / 10, 1) * 30,
        "budget": min(math.log10(budget_left + 1) / math.log10(50001), 1) * 15,
        "runway": 5.0 if days_left is None else min(days_left / 30, 1) * 10,
        "competition": 7.5 if creators is None else min((budget_left / max(creators, 1)) / 500, 1) * 15,
        "trust": (5 if campaign.is_verified else 0) + (3 if campaign.payout_type == "cpm" else 0)
        + (2 if (campaign.rules_text or campaign.description) else 0),
        "platform": (len(overlap) / len(cfg.platforms) * 10) if cfg.platforms else 10.0,
        "niche": 5.0 if not cfg.niches else (10.0 if _niche_match(campaign, cfg.niches) else 0.0),
    }
    funded = utc(campaign.funded_at)
    fresh = funded is not None and now - funded <= FRESH_FUNDING
    bd["bonus"] = (5 if fresh else 0) - (5 if campaign.requires_application else 0)
    bd = {k: round(v, 2) for k, v in bd.items()}
    score = round(max(0.0, min(100.0, sum(bd.values()))), 1)

    reasons: list[str] = []
    if est >= 5:
        reasons.append(f"high $/clip (${est:.2f})")
    elif est == 0:
        reasons.append("no est. earnings at your views")
    if days_left is not None and days_left < 10:
        reasons.append(f"budget burning fast: ~{days_left:g} days left")
    if creators is not None and budget_left / max(creators, 1) < 50:
        reasons.append(f"crowded ({creators} creators)")
    if campaign.requires_application:
        reasons.append("needs application")
    if cfg.platforms and len(overlap) < len(cfg.platforms):
        reasons.append(f"only {', '.join(overlap)} of your platforms")
    if cfg.niches and bd["niche"] == 10:
        reasons.append("matches your niche")
    if fresh:
        reasons.append("freshly funded")
    return ScoreResult(score, bd, reasons[:MAX_REASONS], est, burn, days_left)


def score_all(session: Session, cfg: Optional[ScoreConfig] = None, now: Optional[datetime] = None) -> int:
    """Score every non-ended campaign; only touches score fields."""
    cfg = cfg or ScoreConfig()
    now = utc(now) or datetime.now(timezone.utc)
    campaigns = session.scalars(select(Campaign).where(Campaign.status != CampaignStatus.ended)).all()
    snaps: dict[int, list[CampaignSnapshot]] = {}
    for snap in session.scalars(select(CampaignSnapshot).where(CampaignSnapshot.captured_at >= now - BURN_WINDOW)):
        snaps.setdefault(snap.campaign_id, []).append(snap)
    for c in campaigns:
        r = score_campaign(c, snaps.get(c.id, []), cfg, now)
        c.score = r.score
        c.score_breakdown = r.breakdown
        c.score_reasons = r.reasons
        c.est_per_clip = r.est_per_clip
        c.burn_per_day = r.burn_per_day
        c.days_left = r.days_left
        c.scored_at = now
    log.info("scored %d campaigns", len(campaigns))
    return len(campaigns)
