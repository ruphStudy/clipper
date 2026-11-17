"""Normalized campaign record returned by every provider."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class ScoutError(Exception):
    """Raised when a provider cannot fetch or parse campaigns."""


@dataclass
class CampaignDTO:
    """Provider-agnostic campaign data."""

    external_id: str
    name: str
    url: str
    brand: Optional[str] = None
    campaign_type: Optional[str] = None
    payout_type: Optional[str] = None
    platforms: list[str] = field(default_factory=list)
    cpm_rate: Optional[float] = None
    budget_total: Optional[float] = None
    budget_spent: Optional[float] = None
    budget_left: Optional[float] = None
    progress_pct: Optional[float] = None
    min_payout: Optional[float] = None
    max_payout_per_clip: Optional[float] = None
    creators_count: Optional[int] = None
    requires_application: Optional[bool] = None
    is_verified: Optional[bool] = None
    description: Optional[str] = None
    rules_text: Optional[str] = None
    funded_at: Optional[datetime] = None
    is_live: Optional[bool] = True

    def __post_init__(self) -> None:
        self.platforms = sorted({str(p).strip().lower() for p in (self.platforms or []) if p})
        if self.budget_left is None and self.budget_total is not None and self.budget_spent is not None:
            self.budget_left = round(max(self.budget_total - self.budget_spent, 0.0), 2)
        if self.progress_pct is None and self.budget_total and self.budget_spent is not None:
            self.progress_pct = round(self.budget_spent / self.budget_total * 100, 2)
