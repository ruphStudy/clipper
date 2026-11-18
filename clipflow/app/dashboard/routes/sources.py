"""Footage sources: add to campaign, retry, delete, list."""
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dashboard.deps import clip_counts, get_db, get_engine_for, redirect, render, safe_back
from app.clipper.moments_service import pick_moments
from app.clipper.transcribe_service import transcribe_source
from app.downloader.service import download_source, reset_for_manual_retry
from app.db.enums import CampaignStatus, SourceStatus
from app.db.models import Campaign, Source
from app.sources.links import detect_kind, parse_links

router = APIRouter()
STATUSES = [s.value for s in SourceStatus]


@router.post("/campaigns/{campaign_id}/sources")
def add_sources(campaign_id: int, links: str = Form(""), mark_joined: Optional[str] = Form(None),
                db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Campaign not found")
    valid, invalid = parse_links(links)
    existing = set(db.scalars(select(Source.url).where(Source.campaign_id == c.id)))
    added = skipped = 0
    for url in valid:
        if url in existing:
            skipped += 1
            continue
        db.add(Source(campaign_id=c.id, url=url, kind=detect_kind(url), status=SourceStatus.pending))
        existing.add(url)
        added += 1
    msg = f"Added {added}, skipped {skipped} duplicates, {len(invalid)} invalid"
    if mark_joined and c.status != CampaignStatus.joined:
        c.status = CampaignStatus.joined
        msg += " · campaign marked joined"
    return redirect(f"/campaigns/{c.id}", msg)


@router.post("/sources/{source_id}/retry")
def retry_source(source_id: int, back: Optional[str] = Form(None), db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "Source not found")
    target = safe_back(back, f"/campaigns/{s.campaign_id}")
    if s.status != SourceStatus.failed:
        return redirect(target, f"Source #{s.id} is {s.status.value}; only failed sources can be retried")
    reset_for_manual_retry(s)
    return redirect(target, f"Source #{s.id} queued again")


@router.post("/sources/{source_id}/download")
def start_download(source_id: int, request: Request, background: BackgroundTasks,
                   back: Optional[str] = Form(None), db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "Source not found")
    target = safe_back(back, f"/campaigns/{s.campaign_id}")
    if s.status not in (SourceStatus.pending, SourceStatus.failed):
        return redirect(target, f"Source #{s.id} is {s.status.value}; nothing to download")
    reset_for_manual_retry(s)
    db.commit()
    background.add_task(download_source, s.id, get_engine_for(request))
    return redirect(target, "Download started")


@router.post("/sources/{source_id}/transcribe")
def start_transcribe(source_id: int, request: Request, background: BackgroundTasks,
                     back: Optional[str] = Form(None), db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "Source not found")
    target = safe_back(back, f"/campaigns/{s.campaign_id}")
    if s.status != SourceStatus.downloaded or s.transcript_path:
        return redirect(target, f"Source #{s.id} is not ready for transcription")
    s.transcribe_attempts = 0  # manual request: give it fresh attempts
    db.commit()
    background.add_task(transcribe_source, s.id, get_engine_for(request))
    return redirect(target, "Transcription started")


@router.post("/sources/{source_id}/moments")
def start_moments(source_id: int, request: Request, background: BackgroundTasks,
                  back: Optional[str] = Form(None), db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "Source not found")
    target = safe_back(back, f"/campaigns/{s.campaign_id}")
    if s.status != SourceStatus.downloaded or s.moments_at is not None:
        return redirect(target, f"Source #{s.id} is not ready for moment picking")
    s.moments_attempts = 0  # manual request: fresh attempts
    db.commit()
    background.add_task(pick_moments, s.id, get_engine_for(request))
    return redirect(target, "Moment picking started")


@router.post("/sources/{source_id}/delete")
def delete_source(source_id: int, back: Optional[str] = Form(None), db: Session = Depends(get_db)):
    s = db.get(Source, source_id)
    if s is None:
        raise HTTPException(404, "Source not found")
    target = safe_back(back, f"/campaigns/{s.campaign_id}")
    db.delete(s)
    return redirect(target, f"Source #{source_id} deleted")


@router.get("/sources")
def list_sources(request: Request, status: str = "", db: Session = Depends(get_db)):
    stmt = select(Source, Campaign.name).join(Campaign, Source.campaign_id == Campaign.id)
    if status in STATUSES:
        stmt = stmt.where(Source.status == SourceStatus(status))
    else:
        status = ""
    rows = db.execute(stmt.order_by(Source.updated_at.desc(), Source.id.desc()).limit(500)).all()
    back = "/sources" + (f"?status={status}" if status else "")
    return render(request, "sources.html", rows=rows, status=status, statuses=STATUSES, back=back, nav="sources",
                  clip_counts=clip_counts(db, [src.id for src, _ in rows]))
