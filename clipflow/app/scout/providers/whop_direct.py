"""Direct scraper for public Content Rewards discover pages (formerly Whop Content Rewards).

Content Rewards moved from whop.com/discover/content-rewards/ (now 404) to contentrewards.com.
The discover pages are Next.js apps that server-render the first pages of campaigns into
React flight data (``self.__next_f.push``) under ``"initialResults"``. Further pages load via
Next.js server actions with no public endpoint, so only the server-rendered pages are read
and the provider is marked ``partial`` when more pages exist.
"""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.logger import get_logger
from app.scout.providers.base import USER_AGENT, Provider
from app.scout.schema import CampaignDTO, ScoutError

log = get_logger("clipflow.scout.direct")

BASE_URL = "https://contentrewards.com"
DISCOVER_PAGES = {"clipping": f"{BASE_URL}/discover/clipping", "ugc": f"{BASE_URL}/discover/ugc"}
CAMPAIGN_URL = BASE_URL + "/c/campaigns/{id}"
TIMEOUT = 20.0
REQUEST_GAP_SEC = 1.5
KEEP_RAW_FILES = 10
_PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_DROP_KEYS = {"avatar", "avatarSeed", "thumbnail", "fundedAgo", "budgetSpentLabel", "budgetTotalLabel", "ratePer1kLabel"}


def extract_flight(html: str) -> str:
    """Concatenate the string chunks of Next.js flight data in an HTML page."""
    parts = []
    for raw in _PUSH_RE.findall(html):
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if len(item) > 1 and isinstance(item[1], str):
            parts.append(item[1])
    return "".join(parts)


def extract_pages(html: str) -> list[dict]:
    """Return the server-rendered result pages ({campaigns, nextCursor, ...})."""
    flight = extract_flight(html)
    if not flight:
        raise ScoutError("no Next.js flight data (self.__next_f) found; page structure changed")
    key = '"initialResults":'
    idx = flight.find(key)
    if idx == -1:
        raise ScoutError("'initialResults' not found in flight data; page structure changed")
    try:
        pages, _ = json.JSONDecoder().raw_decode(flight, idx + len(key))
    except ValueError as exc:
        raise ScoutError(f"could not decode initialResults JSON: {exc}") from exc
    if not isinstance(pages, list) or not all(isinstance(p, dict) and "campaigns" in p for p in pages):
        raise ScoutError("initialResults has an unexpected shape")
    return pages


def _s(value) -> Optional[str]:
    """Undo React flight escaping: strings starting with '$' are serialized as '$$...'."""
    if isinstance(value, str) and value.startswith("$$"):
        return value[1:]
    return value


def _f(value) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def map_campaign(c: dict, category: Optional[str] = None) -> CampaignDTO:
    """Map one raw Content Rewards campaign dict to a CampaignDTO."""
    if not c.get("id") or not c.get("title"):
        raise ScoutError("campaign without id/title")
    payout_type = c.get("type")
    launched = c.get("launchedAtMs")
    funded_at = datetime.fromtimestamp(launched / 1000, tz=timezone.utc) if launched else None
    return CampaignDTO(
        external_id=str(c["id"]),
        name=_s(str(c["title"]).strip()),
        url=CAMPAIGN_URL.format(id=c["id"]),
        brand=_s(c.get("brand")),
        campaign_type=_s(c.get("campaignType")) or category,
        payout_type=payout_type,
        platforms=c.get("platforms") or [],
        cpm_rate=_f(c.get("payoutSortRaw")) if payout_type == "cpm" else None,
        budget_total=_f(c.get("budgetTotalRaw")),
        budget_spent=_f(c.get("budgetSpentRaw")),
        budget_left=_f(c.get("availableBudgetRaw")),
        progress_pct=_f(c.get("progressPercentage")),
        creators_count=c.get("creatorCountRaw"),
        requires_application=c.get("requiresApplication"),
        is_verified=c.get("isVerified"),
        description=_s(c.get("description")),
        funded_at=funded_at,
        is_live=True,
    )


class WhopDirectProvider(Provider):
    """Reads public discover pages; free, no login."""

    name = "direct"

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        pages: Optional[dict[str, str]] = None,
        raw_dir: Optional[str] = None,
        request_gap: float = REQUEST_GAP_SEC,
    ) -> None:
        self.client = client or httpx.Client(
            timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self.pages = pages or DISCOVER_PAGES
        self.raw_dir = Path(raw_dir) if raw_dir else Path(settings.DATA_DIR) / "scout"
        self.request_gap = request_gap
        self.partial = False

    def _get(self, url: str) -> str:
        try:
            resp = self.client.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            raise ScoutError(f"network error fetching {url}: {exc}") from exc
        if resp.status_code != 200:
            raise ScoutError(f"HTTP {resp.status_code} from {url}")
        return resp.text

    def fetch(self) -> list[CampaignDTO]:
        """Fetch every configured discover page and map campaigns."""
        raw: list[dict] = []
        dtos: dict[str, CampaignDTO] = {}
        self.partial = False
        for i, (category, url) in enumerate(self.pages.items()):
            if i:
                time.sleep(self.request_gap)
            pages = extract_pages(self._get(url))
            if any(p.get("nextCursor") for p in pages):
                self.partial = True
            for page in pages:
                for c in page["campaigns"]:
                    try:
                        dto = map_campaign(c, category)
                    except ScoutError as exc:
                        log.warning("skipping campaign: %s", exc)
                        continue
                    prev = dtos.get(dto.external_id)
                    if prev is None:
                        dtos[dto.external_id] = dto
                    elif category == "clipping":
                        # Listed under both: clipping wins (we automate clipping).
                        prev.campaign_type = "clipping"
                    raw.append({"category": category, **{k: v for k, v in c.items() if k not in _DROP_KEYS}})
        if not dtos:
            raise ScoutError("discover pages parsed but contained no campaigns")
        if self.partial:
            log.info("direct scrape saw only server-rendered pages; more campaigns exist")
        self._save_raw(raw)
        return list(dtos.values())

    def _save_raw(self, raw: list[dict]) -> None:
        """Write a trimmed raw payload and keep only the newest files."""
        try:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            (self.raw_dir / f"raw_{stamp}.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            for old in sorted(self.raw_dir.glob("raw_*.json"))[:-KEEP_RAW_FILES]:
                old.unlink()
        except OSError as exc:
            log.warning("could not save raw scout payload: %s", exc)
