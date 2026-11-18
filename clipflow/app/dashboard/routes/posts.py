"""Posts list, manual YouTube upload, account activation."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dashboard.deps import get_db, get_engine_for, redirect, render, safe_back
from app.db.enums import ClipStatus, Platform, PostStatus
from app.db.models import Account, Campaign, Clip, Post
from app.posting.manual import caption_text, mark_posted
from app.config import settings
from app.posting.modes import is_paused, set_paused
from app.posting.submit import mark_submitted
from app.tracker.service import set_manual_views
from app.scout.scorer import utc
from app.uploader.instagram import upload_instagram
from app.uploader.youtube import upload_youtube

router = APIRouter()
STATUSES = [s.value for s in PostStatus]


TABS = {
    "tosubmit": ([PostStatus.posted], "posted_asc"),
    "upcoming": ([PostStatus.scheduled, PostStatus.uploading], "time_asc"),
    "manual": ([PostStatus.manual_pending], "time_asc"),
    "posted": ([PostStatus.posted, PostStatus.submitted], "posted_desc"),
    "failed": ([PostStatus.failed], "updated_desc"),
}


def _bounce(post: Post, msg: str):
    return redirect(f"/posts?tab={'manual' if post.status == PostStatus.manual_pending else 'upcoming'}", msg)


def _release_clip(db: Session, clip_id: int, except_post: int) -> None:
    """A clip with no remaining posts goes back to approved (so it can be planned again)."""
    others = db.scalar(select(func.count()).select_from(Post).where(Post.clip_id == clip_id, Post.id != except_post))
    clip = db.get(Clip, clip_id)
    if clip is not None and not others and clip.status == ClipStatus.scheduled:
        clip.status = ClipStatus.approved


@router.get("/posts")
def posts_page(request: Request, tab: str = "", db: Session = Depends(get_db)):
    to_submit_n = db.scalar(select(func.count()).select_from(Post).where(Post.status == PostStatus.posted)) or 0
    if tab not in TABS:
        tab = "tosubmit" if to_submit_n else "upcoming"
    statuses, order = TABS[tab]
    stmt = (select(Post, Clip, Account, Campaign.name).join(Clip, Post.clip_id == Clip.id)
            .join(Account, Post.account_id == Account.id).join(Campaign, Clip.campaign_id == Campaign.id)
            .where(Post.status.in_(statuses)))
    stmt = stmt.order_by({"time_asc": Post.scheduled_at.asc(), "posted_desc": Post.posted_at.desc(),
                          "posted_asc": Post.posted_at.asc(), "updated_desc": Post.updated_at.desc()}[order],
                         Post.id.desc())
    rows = db.execute(stmt.limit(300)).all()
    counts = {t: db.scalar(select(func.count()).select_from(Post).where(Post.status.in_(st))) or 0
              for t, (st, _) in TABS.items()}
    captions = {}
    if tab == "manual":
        for post, clip, _a, _n in rows:
            captions[post.id] = caption_text(clip, post.platform.value, db.get(Campaign, clip.campaign_id))
    submit_info = {}
    if tab == "tosubmit":
        now = datetime.now(timezone.utc)
        deadline = timedelta(minutes=settings.SUBMIT_DEADLINE_MIN)
        for post, clip, _a, _n in rows:
            left = (utc(post.posted_at) or now) + deadline - now
            secs = int(left.total_seconds())
            camp = db.get(Campaign, clip.campaign_id)
            submit_info[post.id] = {"late": secs <= 0, "left": f"{max(secs, 0) // 60:02d}:{max(secs, 0) % 60:02d}",
                                    "campaign_url": camp.url if camp else ""}
    next_due = db.scalar(select(func.min(Post.scheduled_at)).where(Post.status == PostStatus.scheduled))
    return render(request, "posts.html", rows=rows, tab=tab, counts=counts, captions=captions,
                  submit_info=submit_info, paused=is_paused(db), next_due=next_due, nav="posts")


@router.post("/posts/{post_id}/{action}")
def post_action(post_id: int, action: str, request: Request, url: str = Form(""), views: str = Form(""),
                db: Session = Depends(get_db)):
    post = db.get(Post, post_id)
    if post is None:
        raise HTTPException(404, "Post not found")
    now = datetime.now(timezone.utc)
    if action == "mark-posted":
        db.commit()
        ok, msg = mark_posted(post_id, url, engine=get_engine_for(request))
        return redirect("/posts?tab=posted" if ok else "/posts?tab=manual", msg)
    if action == "views":
        db.commit()
        try:
            value = int(views.replace(",", "").strip())
        except ValueError:
            return redirect("/posts?tab=posted", "Views must be a whole number")
        ok, msg = set_manual_views(post_id, value, engine=get_engine_for(request))
        return redirect("/posts?tab=posted", msg)
    if action == "submitted":
        db.commit()
        ok, msg = mark_submitted(post_id, engine=get_engine_for(request))
        return redirect("/posts?tab=tosubmit", msg)
    if action == "cancel":
        if post.status not in (PostStatus.scheduled, PostStatus.manual_pending, PostStatus.failed):
            return _bounce(post, f"Post #{post_id} is {post.status.value}; cannot cancel")
        clip_id = post.clip_id
        _release_clip(db, clip_id, post_id)
        db.delete(post)
        return redirect("/posts?tab=upcoming", f"Post #{post_id} cancelled")
    if post.status != PostStatus.scheduled:
        return _bounce(post, f"Post #{post_id} is {post.status.value}; only scheduled posts can be moved")
    base = utc(post.scheduled_at) or now
    if action == "now":
        post.scheduled_at = now
        msg = f"Post #{post_id} will go out within 5 minutes"
    elif action == "later":
        post.scheduled_at = max(base, now) + timedelta(hours=1)
        msg = f"Post #{post_id} moved +1h"
    elif action == "tomorrow":
        post.scheduled_at = base + timedelta(days=1)
        msg = f"Post #{post_id} moved to tomorrow"
    else:
        raise HTTPException(400, "Unknown action")
    return redirect("/posts?tab=upcoming", msg)


@router.post("/posting/toggle")
def posting_toggle(request: Request, db: Session = Depends(get_db)):
    paused = not is_paused(db)
    db.commit()
    set_paused(paused, get_engine_for(request))
    return redirect("/posts", "Posting paused" if paused else "Posting resumed")


@router.post("/clips/{clip_id}/upload-youtube")
def clip_upload_youtube(clip_id: int, request: Request, background: BackgroundTasks,
                        account_id: int = Form(...), privacy: str = Form(""), db: Session = Depends(get_db)):
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(404, "Clip not found")
    account = db.get(Account, account_id)
    if account is None or account.platform != Platform.youtube:
        return redirect(f"/clips/{clip_id}", "Choose a YouTube account")
    if clip.status not in (ClipStatus.approved, ClipStatus.scheduled):
        return redirect(f"/clips/{clip_id}", f"Clip is {clip.status.value}; approve it first")
    background.add_task(upload_youtube, clip_id, account_id, privacy or None, get_engine_for(request))
    return redirect(f"/clips/{clip_id}", f"Upload to YouTube ({account.handle}) started — see Posts")


@router.post("/clips/{clip_id}/upload-instagram")
def clip_upload_instagram(clip_id: int, request: Request, background: BackgroundTasks,
                          account_id: int = Form(...), db: Session = Depends(get_db)):
    clip = db.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(404, "Clip not found")
    account = db.get(Account, account_id)
    if account is None or account.platform != Platform.instagram:
        return redirect(f"/clips/{clip_id}", "Choose an Instagram account")
    if clip.status not in (ClipStatus.approved, ClipStatus.scheduled):
        return redirect(f"/clips/{clip_id}", f"Clip is {clip.status.value}; approve it first")
    background.add_task(upload_instagram, clip_id, account_id, get_engine_for(request))
    return redirect(f"/clips/{clip_id}", f"Upload to Instagram (@{account.handle}) started — see Posts")


@router.post("/accounts/{account_id}/toggle")
def account_toggle(account_id: int, back: Optional[str] = Form(None), db: Session = Depends(get_db)):
    acct = db.get(Account, account_id)
    if acct is None:
        raise HTTPException(404, "Account not found")
    acct.is_active = not acct.is_active
    return redirect(safe_back(back, "/settings#accounts"),
                    f"{acct.handle} {'activated' if acct.is_active else 'deactivated'}")
