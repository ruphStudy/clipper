"""Shared context builders for the campaign detail page and rules forms."""
from dataclasses import asdict, replace
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dashboard.deps import clip_counts
from app.db.enums import CampaignStatus
from app.db.models import Campaign, CampaignSnapshot, Source
from app.rules import Rules, get_rules
from app.rules.caption import check_text, render_for_platform
from app.rules.extract import suggest_from_text
from app.rules.model import KNOWN_PLATFORMS, normalize_banned, normalize_hashtags, normalize_mentions

SAMPLE_HOOK = "This moment changed everything 😳"
STATUSES = [s.value for s in CampaignStatus]


def _int(value: Optional[str], label: str, errors: list[str], default: int) -> int:
    if value is None or not str(value).strip():
        errors.append(f"{label} is required")
        return default
    try:
        return int(float(value))
    except ValueError:
        errors.append(f"{label} must be a whole number")
        return default


def rules_from_form(form: dict, base: Rules) -> tuple[Rules, list[str]]:
    """Build Rules from submitted form fields (strings); returns (rules, parse errors)."""
    errors: list[str] = []
    rules = replace(
        base,
        clip_min_sec=_int(form.get("clip_min_sec"), "Min seconds", errors, base.clip_min_sec),
        clip_max_sec=_int(form.get("clip_max_sec"), "Max seconds", errors, base.clip_max_sec),
        hashtags=normalize_hashtags(form.get("hashtags") or ""),
        mentions=normalize_mentions(form.get("mentions") or ""),
        banned_words=normalize_banned(form.get("banned_words") or ""),
        caption_template=(form.get("caption_template") or "").replace("\r\n", "\n").strip(),
        credit_text=(form.get("credit_text") or "").strip(),
        disclosure_tag=(form.get("disclosure_tag") or "").strip(),
        max_posts_per_day=_int(form.get("max_posts_per_day"), "Max posts per day", errors, base.max_posts_per_day),
        burn_captions=bool(form.get("burn_captions")),
        language=(form.get("language") or "en").strip() or "en",
    )
    if "post_platforms" in form:
        rules.post_platforms = [p.lower() for p in form["post_platforms"]]
    return rules, errors


def allowed_platforms(c: Campaign) -> list[str]:
    own = {p.lower() for p in (c.platforms or [])}
    return [p for p in KNOWN_PLATFORMS if not own or p in own]


def rules_context(c: Campaign, rules: Optional[Rules] = None, errors: Optional[list[str]] = None) -> dict:
    """Rules form state, regex suggestions and caption previews."""
    rules = rules or get_rules(c)
    text = "\n".join(t for t in (c.rules_text, c.description) if t)
    suggestions = suggest_from_text(text) if text else {}
    previews = []
    for p in rules.post_platforms:
        try:
            out = render_for_platform(rules, SAMPLE_HOOK, p)
        except ValueError:
            continue
        body = out.get("description") or out.get("caption") or out.get("text") or ""
        previews.append({"out": out, "body": body, "violations": check_text(rules, body)})
    return {"rules": rules, "rules_errors": errors or [], "suggestions": suggestions, "previews": previews,
            "allowed_platforms": allowed_platforms(c), "known_platforms": KNOWN_PLATFORMS,
            "rules_dict": asdict(rules)}


def detail_context(db: Session, c: Campaign, rules: Optional[Rules] = None,
                   errors: Optional[list[str]] = None) -> dict:
    """Everything the campaign detail template needs."""
    snaps = db.scalars(select(CampaignSnapshot).where(CampaignSnapshot.campaign_id == c.id)
                       .order_by(CampaignSnapshot.captured_at.desc()).limit(10)).all()
    sources = db.scalars(select(Source).where(Source.campaign_id == c.id).order_by(Source.id)).all()
    ctx = {"c": c, "snaps": snaps, "statuses": STATUSES, "sources_rows": [(s, c.name) for s in sources],
           "clip_counts": clip_counts(db, [s.id for s in sources]), "nav": "campaigns"}
    ctx.update(rules_context(c, rules, errors))
    ctx["perf"] = campaign_performance(db, c.id)
    return ctx


def campaign_performance(db: Session, campaign_id: int) -> dict:
    """All-time posts/views/est for a campaign plus recorded payouts."""
    from sqlalchemy import func

    from app.db.enums import PostStatus
    from app.db.models import CampaignEarning, Clip, Post

    posts, views, est = db.execute(
        select(func.count(Post.id), func.coalesce(func.sum(Post.views), 0), func.coalesce(func.sum(Post.earnings_est), 0.0))
        .join(Clip, Post.clip_id == Clip.id)
        .where(Clip.campaign_id == campaign_id, Post.status.in_([PostStatus.posted, PostStatus.submitted]))).one()
    payouts = db.scalars(select(CampaignEarning).where(CampaignEarning.campaign_id == campaign_id)
                         .order_by(CampaignEarning.period_end.desc())).all()
    return {"posts": posts, "views": int(views), "est": round(float(est), 2),
            "actual": round(sum(p.amount_usd for p in payouts), 2), "payouts": payouts}
