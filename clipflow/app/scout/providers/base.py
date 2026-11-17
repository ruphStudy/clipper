"""Provider interface."""
from abc import ABC, abstractmethod

from app.scout.schema import CampaignDTO

USER_AGENT = "ClipFlow/0.1 personal campaign tracker"


class Provider(ABC):
    """A source of campaigns."""

    name: str = "base"
    # True when fetch() could not see the full catalog (e.g. only first pages);
    # the service then skips "missing => ended" logic.
    partial: bool = False

    @abstractmethod
    def fetch(self) -> list[CampaignDTO]:
        """Return all currently visible campaigns."""
