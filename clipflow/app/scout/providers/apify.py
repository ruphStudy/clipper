"""Apify actor fallback provider."""
import hashlib
import re
from datetime import datetime
from typing import Optional

import httpx

from app.config import settings
from app.logger import get_logger
from app.scout.providers.base import USER_AGENT, Provider
from app.scout.schema import CampaignDTO, ScoutError

log = get_logger("clipflow.scout.apify")

RUN_URL = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
TIMEOUT = 300.0
_EXP_RE = re.compile(r"(exp_[A-Za-z0-9]+)")


def _f(value) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def map_item(item: dict) -> CampaignDTO:
    """Map one Apify dataset item to a CampaignDTO."""
    url = item.get("campaignUrl") or ""
    if not url or not item.get("title"):
        raise ScoutError("apify item without campaignUrl/title")
    m = _EXP_RE.search(url)
    external_id = m.group(1) if m else hashlib.sha1(url.encode()).hexdigest()
    return CampaignDTO(
        external_id=external_id,
        name=str(item["title"]).strip(),
        url=url,
        brand=item.get("brand"),
        campaign_type=item.get("campaignType") or item.get("type"),
        payout_type=item.get("payoutType"),
        platforms=item.get("socialPlatforms") or [],
        cpm_rate=_f(item.get("rewardPerThousandUsd")),
        budget_total=_f(item.get("totalBudgetUsd")),
        budget_spent=_f(item.get("budgetSpentUsd")),
        budget_left=_f(item.get("budgetLeftUsd")),
        progress_pct=_f(item.get("progressPercentage")),
        creators_count=item.get("creatorsCount"),
        requires_application=item.get("requiresApplication"),
        is_verified=item.get("isVerified"),
        description=item.get("description"),
        funded_at=_dt(item.get("fundedAt")),
        is_live=item.get("isLive", True),
    )


class ApifyProvider(Provider):
    """Runs the configured Apify actor synchronously."""

    name = "apify"

    def __init__(self, client: Optional[httpx.Client] = None, token: Optional[str] = None,
                 actor: Optional[str] = None) -> None:
        self.token = settings.APIFY_TOKEN if token is None else token
        self.actor = actor or settings.APIFY_ACTOR
        self.client = client or httpx.Client(timeout=TIMEOUT)

    def fetch(self) -> list[CampaignDTO]:
        """Call the actor and map its dataset items."""
        if not self.token:
            raise ScoutError("APIFY_TOKEN not set")
        url = RUN_URL.format(actor=self.actor)
        try:
            resp = self.client.post(
                url,
                json={"maxItems": 300, "sortBy": "budgetLeft"},
                headers={"Authorization": f"Bearer {self.token}", "User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ScoutError(f"apify network error: {type(exc).__name__}") from exc
        if resp.status_code not in (200, 201):
            raise ScoutError(f"apify HTTP {resp.status_code}")
        try:
            items = resp.json()
        except ValueError as exc:
            raise ScoutError("apify returned non-JSON") from exc
        if not isinstance(items, list):
            raise ScoutError("apify returned unexpected payload")
        dtos: dict[str, CampaignDTO] = {}
        for item in items:
            try:
                dto = map_item(item)
            except ScoutError as exc:
                log.warning("skipping apify item: %s", exc)
                continue
            dtos.setdefault(dto.external_id, dto)
        return list(dtos.values())
