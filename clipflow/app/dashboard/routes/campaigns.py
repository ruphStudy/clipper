"""Campaign list, detail and status updates."""
import math
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.dashboard.deps import get_db, redirect, render, safe_back
from app.db.enums import CampaignStatus
from app.db.models import Campaign, CampaignSnapshot

router = APIRouter()

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


@router.get("/campaigns/{campaign_id}")
def campaign_detail(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Campaign not found")
    snaps = db.scalars(select(CampaignSnapshot).where(CampaignSnapshot.campaign_id == c.id)
                       .order_by(CampaignSnapshot.captured_at.desc()).limit(10)).all()
    return render(request, "campaign_detail.html", c=c, snaps=snaps, statuses=STATUSES, nav="campaigns")


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
