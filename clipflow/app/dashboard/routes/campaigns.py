"""Campaign list, detail and status updates."""
import math
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.dashboard.deps import get_db, redirect, render, safe_back
from app.dashboard.detail import detail_context
from app.db.enums import CampaignStatus
from app.db.models import Campaign
from app.scout.scorer import ScoreConfig, score_campaign
from app.scout.service import MANUAL_PREFIX
from app.sources.links import parse_links

router = APIRouter()
MANUAL_PLATFORMS = ["youtube", "instagram", "tiktok", "x"]

PAGE_SIZE = 50
SORTS = {
    "score": Campaign.score.desc(),
    "cpm": Campaign.cpm_rate.desc(),
    "budget_left": Campaign.budget_left.desc(),
    "first_seen": Campaign.first_seen_at.desc(),
}
STATUSES = [s.value for s in CampaignStatus]


@router.get("/campaigns")
def campaign_list(request: Request, status: str = "", type: str = "clipping", q: str = "",
                  sort: str = "score", page: int = 1, db: Session = Depends(get_db)):
    stmt = select(Campaign)
    if status == "all":
        pass
    elif status in STATUSES:
        stmt = stmt.where(Campaign.status == CampaignStatus(status))
    else:
        status = ""
        stmt = stmt.where(Campaign.status != CampaignStatus.ended)
    if type and type != "all":
        stmt = stmt.where(func.lower(Campaign.campaign_type) == type.lower())
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Campaign.name.ilike(like), Campaign.brand.ilike(like)))
    sort = sort if sort in SORTS else "score"
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 1), pages)
    rows = db.scalars(stmt.order_by(SORTS[sort], Campaign.id).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    types = sorted({t for (t,) in db.execute(select(Campaign.campaign_type).distinct()) if t})
    return render(request, "campaigns.html", rows=rows, total=total, page=page, pages=pages,
                  f={"status": status, "type": type, "q": q, "sort": sort}, statuses=STATUSES,
                  types=types, sorts=list(SORTS), back=_back(request), nav="campaigns")


def _back(request: Request) -> str:
    """Current list URL without the flash message, for post-action redirects."""
    params = {k: v for k, v in request.query_params.items() if k != "msg"}
    return request.url.path + ("?" + urlencode(params) if params else "")


def _opt_float(value: Optional[str], field: str, errors: list[str]) -> Optional[float]:
    if value is None or not value.strip():
        return None
    try:
        v = float(value)
    except ValueError:
        errors.append(f"{field} must be a number")
        return None
    if v < 0:
        errors.append(f"{field} must be ≥ 0")
    return v


@router.get("/campaigns/new")
def campaign_new_form(request: Request):
    return render(request, "campaign_new.html", form={}, errors=[], platforms=MANUAL_PLATFORMS, nav="campaigns")


@router.post("/campaigns/new")
def campaign_new(request: Request, name: str = Form(""), url: str = Form(""), brand: str = Form(""),
                 platforms: list[str] = Form([]), cpm_rate: str = Form(""), min_payout: str = Form(""),
                 max_payout_per_clip: str = Form(""), budget_left: str = Form(""),
                 requires_application: Optional[str] = Form(None), notes: str = Form(""),
                 db: Session = Depends(get_db)):
    errors: list[str] = []
    if not name.strip():
        errors.append("Name is required")
    url = url.strip()
    if not url:
        errors.append("URL is required")
    elif not parse_links(url)[0]:
        errors.append("URL must be a valid http(s) link")
    nums = {k: _opt_float(v, k, errors) for k, v in
            {"cpm_rate": cpm_rate, "min_payout": min_payout, "max_payout_per_clip": max_payout_per_clip,
             "budget_left": budget_left}.items()}
    chosen = [p for p in platforms if p in MANUAL_PLATFORMS]
    if errors:
        form = {"name": name, "url": url, "brand": brand, "platforms": chosen, "notes": notes,
                "requires_application": bool(requires_application),
                "cpm_rate": cpm_rate, "min_payout": min_payout, "max_payout_per_clip": max_payout_per_clip,
                "budget_left": budget_left}
        resp = render(request, "campaign_new.html", form=form, errors=errors, platforms=MANUAL_PLATFORMS,
                      nav="campaigns")
        resp.status_code = 400
        return resp
    now = datetime.now(timezone.utc)
    c = Campaign(external_id=f"{MANUAL_PREFIX}{uuid.uuid4().hex[:10]}", name=name.strip(), url=url,
                 brand=brand.strip() or None, platforms=chosen, campaign_type="clipping",
                 payout_type="cpm" if nums["cpm_rate"] is not None else None,
                 requires_application=bool(requires_application), notes=notes.strip() or None,
                 status=CampaignStatus.joined, is_live=True, first_seen_at=now, last_seen_at=now, **nums)
    r = score_campaign(c, [], ScoreConfig(), now)
    c.score, c.score_breakdown, c.score_reasons = r.score, r.breakdown, r.reasons
    c.est_per_clip, c.burn_per_day, c.days_left, c.scored_at = r.est_per_clip, r.burn_per_day, r.days_left, now
    db.add(c)
    db.flush()
    return redirect(f"/campaigns/{c.id}", f"Campaign #{c.id} created (joined)")


@router.get("/campaigns/{campaign_id}")
def campaign_detail(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Campaign not found")
    return render(request, "campaign_detail.html", **detail_context(db, c))


@router.post("/campaigns/{campaign_id}/status")
def campaign_set_status(campaign_id: int, status: str = Form(...), notes: Optional[str] = Form(None),
                        back: Optional[str] = Form(None), db: Session = Depends(get_db)):
    if status not in STATUSES:
        raise HTTPException(400, f"Invalid status '{status}'")
    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Campaign not found")
    c.status = CampaignStatus(status)
    if notes is not None:
        c.notes = notes.strip() or None
    return redirect(safe_back(back, f"/campaigns/{campaign_id}"), f"#{c.id} {c.name[:60]} → {status}")
