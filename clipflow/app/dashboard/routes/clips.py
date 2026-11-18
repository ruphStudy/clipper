"""Clips: grid, detail with player, video/thumbnail streaming (approve/reject comes in S5)."""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.clipper.compliance import check_clip, compliance_state
from app.dashboard.deps import get_db, get_engine_for, redirect, render
from app.rules.caption import LIMITS, YT_TITLE_MAX
from app.db.enums import ClipStatus, Platform
from app.db.models import Account, Campaign, Clip

router = APIRouter()
STATUSES = [s.value for s in ClipStatus]
PAGE_LIMIT = 120
COMPLIANCE_FILTERS = ("ok", "warn", "error")


def _clip(db: Session, clip_id: int) -> Clip:
    c = db.get(Clip, clip_id)
    if c is None:
        raise HTTPException(404, "Clip not found")
    return c


def _media(path: Optional[str]) -> Path:
    """Serve only files inside CLIPS_DIR that exist."""
    if not path:
        raise HTTPException(404, "No file")
    p = Path(path).resolve()
    root = Path(settings.CLIPS_DIR).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "File not available")
    return p


@router.get("/clips")
def clip_grid(request: Request, status: str = "rendered", source: Optional[int] = None, compliance: str = "",
              db: Session = Depends(get_db)):
    stmt = select(Clip, Campaign.name).join(Campaign, Clip.campaign_id == Campaign.id)
    if status in STATUSES:
        stmt = stmt.where(Clip.status == ClipStatus(status))
    else:
        status = "all"
    if source:
        stmt = stmt.where(Clip.source_id == source)
    rows = db.execute(stmt.order_by(Clip.rendered_at.desc(), Clip.moment_score.desc(), Clip.id.desc())
                      .limit(PAGE_LIMIT if compliance not in COMPLIANCE_FILTERS else PAGE_LIMIT * 5)).all()
    if compliance in COMPLIANCE_FILTERS:
        rows = [r for r in rows if compliance_state(r[0].compliance) == compliance][:PAGE_LIMIT]
    else:
        compliance = ""
    return render(request, "clips.html", rows=rows, status=status, statuses=STATUSES, source=source,
                  compliance=compliance, nav="clips")


@router.get("/clips/{clip_id}")
def clip_detail(clip_id: int, request: Request, db: Session = Depends(get_db)):
    c = _clip(db, clip_id)
    previews = []
    for platform, out in (c.platform_captions or {}).items():
        fields = []
        if out.get("title") is not None:
            fields.append(("title", out["title"], YT_TITLE_MAX))
        for name in ("description", "caption", "text"):
            if out.get(name) is not None:
                fields.append((name, out[name], LIMITS.get(platform, 2200)))
        previews.append((platform, fields))
    yt_accounts = db.scalars(select(Account).where(Account.platform == Platform.youtube, Account.is_active.is_(True))
                             .order_by(Account.id)).all()
    ig_accounts = db.scalars(select(Account).where(Account.platform == Platform.instagram,
                                                   Account.is_active.is_(True)).order_by(Account.id)).all()
    return render(request, "clip_detail.html", clip=c, campaign=db.get(Campaign, c.campaign_id), previews=previews,
                  yt_accounts=yt_accounts, ig_accounts=ig_accounts, nav="clips")


@router.post("/clips/{clip_id}/check")
def clip_recheck(clip_id: int, request: Request, db: Session = Depends(get_db)):
    _clip(db, clip_id)
    db.commit()
    r = check_clip(clip_id, autofix=True, engine=get_engine_for(request))
    msg = ("Compliance OK" if r["ok"] else f"Blocked: {len(r['errors'])} error(s)") + \
        (f", {len(r['warnings'])} warning(s)" if r["warnings"] else "")
    return redirect(f"/clips/{clip_id}", msg)


@router.get("/clips/{clip_id}/video")
def clip_video(clip_id: int, download: int = 0, db: Session = Depends(get_db)):
    path = _media(_clip(db, clip_id).file_path)
    return FileResponse(path, media_type="video/mp4", filename=f"clipflow_{clip_id}.mp4" if download else path.name,
                        content_disposition_type="attachment" if download else "inline")


@router.get("/clips/{clip_id}/thumb")
def clip_thumb(clip_id: int, db: Session = Depends(get_db)):
    return FileResponse(_media(_clip(db, clip_id).thumb_path), media_type="image/jpeg")
