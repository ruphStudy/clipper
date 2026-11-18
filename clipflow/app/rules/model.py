"""Rules dataclass, defaults, normalization and validation."""
import json
import re
from dataclasses import dataclass, field, fields
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.config import csv_list, settings
from app.db.models import AppSetting, Campaign

DEFAULTS_KEY = "default_rules"
KNOWN_PLATFORMS = ("youtube", "instagram", "tiktok", "x")
DEFAULT_TEMPLATE = "{hook}\n\n{credit}\n{mentions} {hashtags} {disclosure}"
BUILTIN_DEFAULTS: dict = {
    "clip_min_sec": 15,
    "clip_max_sec": 60,
    "hashtags": [],
    "mentions": [],
    "banned_words": [],
    "caption_template": DEFAULT_TEMPLATE,
    "credit_text": "",
    "disclosure_tag": "",
    "max_posts_per_day": 3,
    "burn_captions": True,
    "language": "en",
}
# Rules field -> Campaign column, where the names differ.
CAMPAIGN_FIELD = {"hashtags": "required_hashtags", "mentions": "required_mentions"}


@dataclass
class Rules:
    """Effective posting rules for one campaign."""

    clip_min_sec: int = 15
    clip_max_sec: int = 60
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    banned_words: list[str] = field(default_factory=list)
    post_platforms: list[str] = field(default_factory=list)
    caption_template: str = DEFAULT_TEMPLATE
    credit_text: str = ""
    disclosure_tag: str = ""
    max_posts_per_day: int = 3
    burn_captions: bool = True
    language: str = "en"
    confirmed: bool = False


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _split(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p for p in re.split(r"[\s,]+", value) if p]
    return [str(v) for v in value]


def normalize_hashtags(value) -> list[str]:
    """'#Tag', 'tag ' -> '#tag' (lowercase, deduped, order kept)."""
    return _dedupe("#" + re.sub(r"\s+", "", t).lstrip("#").lower() for t in _split(value)
                   if re.sub(r"\s+", "", t).lstrip("#"))


def normalize_mentions(value) -> list[str]:
    """'Handle', '@handle' -> '@handle' (deduped, order kept)."""
    return _dedupe("@" + re.sub(r"\s+", "", t).lstrip("@").lower() for t in _split(value)
                   if re.sub(r"\s+", "", t).lstrip("@"))


def normalize_banned(value) -> list[str]:
    """Lowercase, trimmed, deduped; comma/newline separated (phrases allowed)."""
    items = re.split(r"[,\n]+", value) if isinstance(value, str) else (value or [])
    return _dedupe(re.sub(r"\s+", " ", str(w)).strip().lower() for w in items)


def load_defaults(session: Optional[Session] = None) -> dict:
    """Built-in defaults overridden by app_settings 'default_rules' JSON."""
    out = dict(BUILTIN_DEFAULTS)
    if session is None:
        return out
    row = session.scalar(select(AppSetting).where(AppSetting.key == DEFAULTS_KEY))
    if row and row.value:
        try:
            stored = json.loads(row.value)
        except ValueError:
            stored = {}
        out.update({k: v for k, v in stored.items() if k in BUILTIN_DEFAULTS and v is not None})
    return out


def save_defaults(session: Session, values: dict) -> None:
    """Persist default rules (known keys only)."""
    data = json.dumps({k: values[k] for k in BUILTIN_DEFAULTS if k in values})
    row = session.scalar(select(AppSetting).where(AppSetting.key == DEFAULTS_KEY))
    if row is None:
        session.add(AppSetting(key=DEFAULTS_KEY, value=data))
    else:
        row.value = data


def default_post_platforms(campaign: Campaign) -> list[str]:
    """Campaign platforms ∩ MY_PLATFORMS (in MY_PLATFORMS order)."""
    allowed = {p.lower() for p in (campaign.platforms or [])}
    return [p for p in csv_list(settings.MY_PLATFORMS) if p in allowed]


def get_rules(campaign: Campaign, defaults: Optional[dict] = None) -> Rules:
    """Effective rules: campaign values override defaults; None means 'use default'."""
    if defaults is None:
        defaults = load_defaults(object_session(campaign))
    values = {}
    for name in BUILTIN_DEFAULTS:
        own = getattr(campaign, CAMPAIGN_FIELD.get(name, name), None)
        # required_hashtags/mentions are created as [] by the scout, so an empty list also means "unset".
        unset = own is None or (name in CAMPAIGN_FIELD and own == [])
        values[name] = defaults[name] if unset else own
    rules = Rules(
        clip_min_sec=int(values["clip_min_sec"]),
        clip_max_sec=int(values["clip_max_sec"]),
        hashtags=normalize_hashtags(values["hashtags"]),
        mentions=normalize_mentions(values["mentions"]),
        banned_words=normalize_banned(values["banned_words"]),
        post_platforms=list(campaign.post_platforms) if campaign.post_platforms is not None
        else default_post_platforms(campaign),
        caption_template=values["caption_template"] or DEFAULT_TEMPLATE,
        credit_text=values["credit_text"] or "",
        disclosure_tag=values["disclosure_tag"] or "",
        max_posts_per_day=int(values["max_posts_per_day"]),
        burn_captions=bool(values["burn_captions"]),
        language=values["language"] or "en",
        confirmed=campaign.rules_confirmed_at is not None,
    )
    return rules


def validate(rules: Rules, check_platforms: bool = True) -> list[str]:
    """Return human-readable validation errors (empty = valid)."""
    errors = []
    if not (5 <= rules.clip_min_sec < rules.clip_max_sec <= 180):
        errors.append("Clip length must satisfy 5 ≤ min < max ≤ 180 seconds")
    if check_platforms:
        unknown = [p for p in rules.post_platforms if p not in KNOWN_PLATFORMS]
        if unknown:
            errors.append(f"Unknown post platforms: {', '.join(unknown)}")
        if not rules.post_platforms:
            errors.append("Choose at least one platform to post to")
    if len(rules.hashtags) > 30:
        errors.append("At most 30 hashtags")
    if "{hook}" not in (rules.caption_template or ""):
        errors.append("Caption template must contain {hook}")
    if rules.max_posts_per_day < 1:
        errors.append("Max posts per day must be at least 1")
    return errors


def rules_field_names() -> list[str]:
    return [f.name for f in fields(Rules)]
