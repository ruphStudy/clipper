"""Review UI: one clip at a time, bulk actions, undo."""
import re
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app.clipper.render import render_clip
from app.dashboard.deps import get_db, get_engine_for, redirect, render, safe_back
from app.db.models import Campaign, Clip
from app.review import service
from app.rules.caption import LIMITS, YT_TITLE_MAX

router = APIRouter()
CAPTION_FIELD = re.compile(r"^cap__(youtube|instagram|tiktok|x)__(title|description|caption|text)$")


def _caption_fields(clip: Clip) -> list[tuple[str, str, str, int]]:
    """[(platform, field, text, limit)] for the edit form."""
    out = []
    for platform, data in (clip.platform_captions or {}).items():
        for field in ("title", "description", "caption", "text"):
            if data.get(field) is not None:
                limit = YT_TITLE_MAX if (platform, field) == ("youtube", "title") else LIMITS.get(platform, 2200)
                out.append((platform, field, data[field], limit))
    return out


@router.get("/review")
def review_page(request: Request, id: Optional[int] = None, db: Session = Depends(get_db)):
    queue = service.review_queue(db)
    if not queue:
        return render(request, "review.html", clip=None, stats=service.stats_today(get_engine_for(request)),
                      nav="review")
    idx = next((i for i, c in enumerate(queue) if c.id == id), 0)
    clip = queue[idx]
    nxt = queue[(idx + 1) % len(queue)] if len(queue) > 1 else None
    return render(request, "review.html", clip=clip, campaign=db.get(Campaign, clip.campaign_id),
                  position=idx + 1, total=len(queue), next_id=nxt.id if nxt else None,
                  caption_fields=_caption_fields(clip), reasons=service.REJECT_REASONS, nav="review")


@router.post("/review/{clip_id}/approve")
async def review_approve(clip_id: int, request: Request):
    form = await request.form()
    edits: dict = {}
    for key in form.keys():
        m = CAPTION_FIELD.match(key)
        if m:
            edits.setdefault(m.group(1), {})[m.group(2)] = str(form.get(key) or "")
    ok, msg = service.approve(clip_id, edits or None, engine=get_engine_for(request))
    return redirect("/review" if ok else f"/review?id={clip_id}", msg)


@router.post("/review/{clip_id}/reject")
def review_reject(clip_id: int, request: Request, reason: str = Form(""), other: str = Form("")):
    if reason == "other":
        reason = f"other: {other}"
    ok, msg = service.reject(clip_id, reason, engine=get_engine_for(request))
    return redirect("/review" if ok else f"/review?id={clip_id}", msg)


@router.post("/review/{clip_id}/rerender")
def review_rerender(clip_id: int, request: Request, background: BackgroundTasks, hook: str = Form(""),
                    start_delta: float = Form(0), end_delta: float = Form(0)):
    engine = get_engine_for(request)
    ok, msg = service.rerender(clip_id, hook or None, start_delta, end_delta, engine=engine)
    if ok:
        background.add_task(render_clip, clip_id, engine)
        return redirect("/review", msg)
    return redirect(f"/review?id={clip_id}", msg)


@router.post("/clips/bulk")
async def clips_bulk(request: Request):
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    action = str(form.get("action") or "")
    back = safe_back(str(form.get("back") or ""), "/clips")
    engine = get_engine_for(request)
    if not ids:
        return redirect(back, "No clips selected")
    done = skipped = 0
    if action == "approve":
        for cid in ids:
            ok, _ = service.approve(cid, engine=engine)
            done, skipped = done + ok, skipped + (not ok)
        return redirect(back, f"Approved {done}" + (f", skipped {skipped} with errors" if skipped else ""))
    if action == "reject":
        reason = str(form.get("reason") or "")
        if service.normalize_reason(reason) is None:
            return redirect(back, "Choose a reject reason")
        for cid in ids:
            ok, _ = service.reject(cid, reason, engine=engine)
            done, skipped = done + ok, skipped + (not ok)
        return redirect(back, f"Rejected {done}" + (f", skipped {skipped}" if skipped else ""))
    raise HTTPException(400, "Unknown bulk action")


@router.post("/clips/{clip_id}/undo")
def clip_undo(clip_id: int, request: Request):
    _ok, msg = service.undo(clip_id, engine=get_engine_for(request))
    return redirect(f"/clips/{clip_id}", msg)
