"""Per-campaign posting rules: one entry point, get_rules(campaign)."""
from app.rules.model import Rules, get_rules, load_defaults, validate

__all__ = ["Rules", "get_rules", "load_defaults", "validate"]
