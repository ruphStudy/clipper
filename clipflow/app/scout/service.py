"""Campaign scout: fetch from a provider and upsert into the campaigns table."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from sqlalchemy import Engine, delete, func, select

from app.config import settings
from app.db import get_session
from app.db.enums import CampaignStatus
from app.db.models import Campaign, CampaignSnapshot
from app.logger import get_logger
from app.notify import notify_error
from app.scout.providers.apify import ApifyProvider
from app.scout.providers.base import Provider
from app.scout.providers.whop_direct import WhopDirectProvider
from app.scout.schema import CampaignDTO, ScoutError
from app.scout.scorer import score_all, utc

log = get_logger("clipflow.scout")

SNAPSHOT_INTERVAL = timedelta(hours=6)
SNAPSHOT_RETENTION = timedelta(days=60)

USER_LOCKED_STATUSES = {CampaignStatus.selected, CampaignStatus.joined, CampaignStatus.paused}
AUTO_END_STATUSES = {CampaignStatus.discovered, CampaignStatus.selected}
NEVER_END_STATUSES = {CampaignStatus.joined, CampaignStatus.paused}


@dataclass
class ScoutResult:
    """Summary of a scout run."""

    fetched: int = 0
    new: int = 0
    updated: int = 0
    ended: int = 0
    provider_used: str = ""
    campaigns: list[CampaignDTO] = field(default_factory=list, repr=False)


class AutoProvider(Provider):
    """Direct first; Apify if direct fails and APIFY_TOKEN is set."""

    name = "auto"

    def __init__(self, direct: Optional[Provider] = None, apify: Optional[Provider] = None,
                 apify_enabled: Optional[bool] = None) -> None:
        self.direct = direct or WhopDirectProvider()
        self.apify = apify or ApifyProvider()
        self.apify_enabled = bool(settings.APIFY_TOKEN) if apify_enabled is None else apify_enabled
        self.used = self.direct

    @property
    def partial(self) -> bool:  # type: ignore[override]
        return self.used.partial

    def fetch(self) -> list[CampaignDTO]:
        self.used = self.direct
        try:
            return self.direct.fetch()
        except ScoutError as exc:
            if not self.apify_enabled:
                raise
            log.warning("direct provider failed (%s); falling back to apify", exc)
            self.used = self.apify
            return self.apify.fetch()


def get_provider(name: str = "auto") -> Provider:
    """Return a provider by name: direct | apify | auto."""
    if name == "direct":
        return WhopDirectProvider()
    if name == "apify":
        return ApifyProvider()
    if name == "auto":
        return AutoProvider()
    raise ValueError(f"unknown provider: {name}")


def _apply(row: Campaign, dto: CampaignDTO) -> None:
    """Copy provider-owned fields from the DTO onto the row."""
    data = asdict(dto)
    data.pop("external_id")
    for key, value in data.items():
        setattr(row, key, value)


def _upsert(dtos: list[CampaignDTO], partial: bool, engine: Optional[Engine]) -> tuple[int, int, int]:
    now = datetime.now(timezone.utc)
    new = updated = ended = 0
    seen = {d.external_id for d in dtos}
    with get_session(engine) as s:
        existing = {c.external_id: c for c in s.scalars(select(Campaign))}
        for dto in dtos:
            row = existing.get(dto.external_id)
            live = dto.is_live is not False
            if row is None:
                row = Campaign(external_id=dto.external_id, first_seen_at=now)
                _apply(row, dto)
                row.status = CampaignStatus.discovered if live else CampaignStatus.ended
                row.last_seen_at = now
                s.add(row)
                new += 1
                continue
            old_status = row.status
            _apply(row, dto)
            row.last_seen_at = now
            updated += 1
            if live:
                if old_status not in USER_LOCKED_STATUSES:
                    row.status = CampaignStatus.discovered
            elif old_status in NEVER_END_STATUSES:
                log.warning("campaign %s is no longer live but status=%s; not ending", row.name, old_status.value)
            elif old_status != CampaignStatus.ended:
                row.status = CampaignStatus.ended
                ended += 1
        if partial:
            log.info("provider returned a partial catalog; skipping missing-campaign ending")
        else:
            for ext_id, row in existing.items():
                if ext_id in seen:
                    continue
                if row.status in AUTO_END_STATUSES:
                    row.status = CampaignStatus.ended
                    row.is_live = False
                    ended += 1
                elif row.status in NEVER_END_STATUSES and row.is_live is not False:
                    row.is_live = False
                    log.warning("campaign %s (%s) missing from scout but status=%s; marked not live",
                                row.name, ext_id, row.status.value)
        s.flush()
        rows = s.scalars(select(Campaign).where(Campaign.external_id.in_(seen))).all() if seen else []
        record_snapshots(s, rows, now)
    return new, updated, ended


def record_snapshots(session, campaigns: list[Campaign], now: Optional[datetime] = None) -> int:
    """Insert one snapshot per campaign (max 1 per 6h) and prune ones older than 60 days."""
    now = now or datetime.now(timezone.utc)
    session.execute(delete(CampaignSnapshot).where(CampaignSnapshot.captured_at < now - SNAPSHOT_RETENTION))
    ids = [c.id for c in campaigns]
    latest: dict[int, datetime] = {}
    if ids:
        q = (select(CampaignSnapshot.campaign_id, func.max(CampaignSnapshot.captured_at))
             .where(CampaignSnapshot.campaign_id.in_(ids)).group_by(CampaignSnapshot.campaign_id))
        latest = {cid: utc(ts) for cid, ts in session.execute(q)}
    added = 0
    for c in campaigns:
        last = latest.get(c.id)
        if last is not None and now - last < SNAPSHOT_INTERVAL:
            continue
        session.add(CampaignSnapshot(campaign_id=c.id, budget_left=c.budget_left, budget_spent=c.budget_spent,
                                     creators_count=c.creators_count, progress_pct=c.progress_pct,
                                     captured_at=now))
        added += 1
    return added


def run_scout(provider: Union[str, Provider] = "auto", dry_run: bool = False,
              engine: Optional[Engine] = None) -> ScoutResult:
    """Fetch campaigns and (unless dry_run) upsert them."""
    prov = get_provider(provider) if isinstance(provider, str) else provider
    try:
        dtos = prov.fetch()
    except ScoutError as exc:
        log.error("scout failed (%s): %s", prov.name, exc)
        notify_error("scout", exc)
        raise
    used = prov.used.name if isinstance(prov, AutoProvider) else prov.name
    result = ScoutResult(fetched=len(dtos), provider_used=used, campaigns=dtos)
    if not dry_run:
        result.new, result.updated, result.ended = _upsert(dtos, prov.partial, engine)
        with get_session(engine) as s:
            score_all(s)
    log.info("scout done: provider=%s fetched=%d new=%d updated=%d ended=%d dry_run=%s",
             used, result.fetched, result.new, result.updated, result.ended, dry_run)
    return result
