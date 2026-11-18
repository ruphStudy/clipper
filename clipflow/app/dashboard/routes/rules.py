"""Per-campaign rules and default-rules settings."""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.clipper.compliance import recheck_rules_changed
from app.dashboard.deps import get_db, get_engine_for, redirect, render
from app.dashboard.detail import allowed_platforms, detail_context, rules_from_form
from app.db import get_session
from app.db.models import Account, Campaign
from app.rules import Rules, get_rules, load_defaults, validate
from app.rules.extract import suggest_from_text
from app.rules.model import BUILTIN_DEFAULTS, normalize_hashtags, normalize_mentions, save_defaults

router = APIRouter()
SECRET_KEYS = ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DASHBOARD_PASSWORD", "APIFY_TOKEN",
               "YOUTUBE_CLIENT_SECRETS_PATH", "META_APP_ID", "META_APP_SECRET",
               "YOUTUBE_API_KEY")


async def _form(request: Request) -> dict:
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}
    if "post_platforms_present" in form or "post_platforms" in form:
        data["post_platforms"] = form.getlist("post_platforms")
    return data


def _campaign(db: Session, campaign_id: int) -> Campaign:
    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Campaign not found")
    return c


@router.post("/campaigns/{campaign_id}/rules")
async def save_rules(campaign_id: int, request: Request, background: BackgroundTasks,
                     db: Session = Depends(get_db)):
    c = _campaign(db, campaign_id)
    data = await _form(request)
    data.setdefault("post_platforms", [])
    rules, errors = rules_from_form(data, get_rules(c))
    errors += validate(rules)
    bad = [p for p in rules.post_platforms if p not in allowed_platforms(c)]
    if bad:
        errors.append(f"Campaign does not accept: {', '.join(bad)}")
    if errors:
        resp = render(request, "campaign_detail.html", **detail_context(db, c, rules, errors))
        resp.status_code = 400
        return resp
    c.clip_min_sec, c.clip_max_sec = rules.clip_min_sec, rules.clip_max_sec
    c.required_hashtags, c.required_mentions = rules.hashtags, rules.mentions
    c.banned_words, c.post_platforms = rules.banned_words, rules.post_platforms
    c.caption_template, c.credit_text, c.disclosure_tag = rules.caption_template, rules.credit_text, rules.disclosure_tag
    c.max_posts_per_day, c.burn_captions, c.language = rules.max_posts_per_day, rules.burn_captions, rules.language
    c.rules_confirmed_at = datetime.now(timezone.utc)
    db.commit()
    background.add_task(recheck_rules_changed, c.id, get_engine_for(request))
    return redirect(f"/campaigns/{c.id}#rules", "Rules saved and confirmed · re-checking rendered clips")


@router.post("/campaigns/{campaign_id}/rules/apply-suggestions")
def apply_suggestions(campaign_id: int, request: Request, background: BackgroundTasks,
                      db: Session = Depends(get_db)):
    c = _campaign(db, campaign_id)
    sug = suggest_from_text("\n".join(t for t in (c.rules_text, c.description) if t))
    applied = []
    if sug.get("hashtags"):
        c.required_hashtags = normalize_hashtags(list(c.required_hashtags or []) + sug["hashtags"])
        applied.append("hashtags")
    if sug.get("mentions"):
        c.required_mentions = normalize_mentions(list(c.required_mentions or []) + sug["mentions"])
        applied.append("mentions")
    for key in ("clip_min_sec", "clip_max_sec", "disclosure_tag"):
        if key in sug:
            setattr(c, key, sug[key])
            applied.append(key.replace("_", " "))
    msg = f"Applied: {', '.join(applied)} — review and save to confirm" if applied else "No suggestions to apply"
    if applied:
        db.commit()
        background.add_task(recheck_rules_changed, c.id, get_engine_for(request))
    return redirect(f"/campaigns/{c.id}#rules", msg)


def _settings_page(request: Request, rules: Rules, errors: list[str], status: int = 200):
    summary = [("MY_PLATFORMS", settings.MY_PLATFORMS), ("MY_NICHES", settings.MY_NICHES or "–"),
               ("MY_CAMPAIGN_TYPES", settings.MY_CAMPAIGN_TYPES), ("TYPICAL_VIEWS", settings.TYPICAL_VIEWS),
               ("TIMEZONE", settings.TIMEZONE)]
    secrets = [(k, "SET" if getattr(settings, k, "") else "MISSING") for k in SECRET_KEYS]
    with get_session(get_engine_for(request)) as s:
        accounts = s.scalars(select(Account).order_by(Account.platform, Account.id)).all()
    resp = render(request, "settings.html", rules=rules, rules_errors=errors, summary=summary, secrets=secrets,
                  accounts=accounts, nav="settings")
    resp.status_code = status
    return resp


def _defaults_rules(db: Session) -> Rules:
    d = load_defaults(db)
    return Rules(clip_min_sec=int(d["clip_min_sec"]), clip_max_sec=int(d["clip_max_sec"]),
                 hashtags=normalize_hashtags(d["hashtags"]), mentions=normalize_mentions(d["mentions"]),
                 banned_words=list(d["banned_words"]), caption_template=d["caption_template"],
                 credit_text=d["credit_text"], disclosure_tag=d["disclosure_tag"],
                 max_posts_per_day=int(d["max_posts_per_day"]), burn_captions=bool(d["burn_captions"]),
                 language=d["language"])


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    return _settings_page(request, _defaults_rules(db), [])


@router.post("/settings")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    data = await _form(request)
    data.pop("post_platforms", None)
    rules, errors = rules_from_form(data, _defaults_rules(db))
    errors += validate(rules, check_platforms=False)
    if errors:
        return _settings_page(request, rules, errors, 400)
    save_defaults(db, {k: getattr(rules, k) for k in BUILTIN_DEFAULTS})
    return redirect("/settings", "Default rules saved")
