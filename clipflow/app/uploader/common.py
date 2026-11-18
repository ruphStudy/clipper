"""Shared uploader plumbing: preconditions, Post lifecycle, per-key app_settings helpers."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.db.enums import ClipStatus, Platform, PostStatus
from app.db.models import Account, AppSetting, Clip, Post
from app.logger import get_logger

log = get_logger("clipflow.uploader")

ACTIVE_POST = (PostStatus.uploading, PostStatus.posted, PostStatus.submitted)
LABELS = {Platform.youtube: "YouTube", Platform.instagram: "Instagram", Platform.tiktok: "TikTok", Platform.x: "X"}
REAUTH_CMD = {Platform.youtube: "yt-auth", Platform.instagram: "ig-connect"}


class AuthError(Exception):
    """The account must be re-authorized / re-connected."""


def get_setting(session: Session, key: str) -> Optional[str]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    return row.value if row else None


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def check_preconditions(clip: Optional[Clip], account: Optional[Account], platform: Platform, daily_cap: int,
                        count_today_fn: Callable[[], int], session: Session) -> tuple[bool, str]:
    """Common checks before any upload; (False, reason) refuses without touching Posts."""
    label = LABELS.get(platform, platform.value)
    if clip is None:
        return False, "clip not found"
    if clip.status not in (ClipStatus.approved, ClipStatus.scheduled):
        return False, f"clip is {clip.status.value}, not approved"
    comp = clip.compliance or {}
    if not comp.get("ok") or comp.get("errors"):
        return False, "clip has compliance errors"
    if not clip.file_path or not Path(clip.file_path).is_file():
        return False, "clip file missing"
    if account is None or account.platform != platform:
        return False, f"no such {label} account"
    if not account.is_active:
        return False, f"account {account.handle} is inactive"
    if account.auth_error:
        cmd = REAUTH_CMD.get(platform, "reconnect")
        return False, f"account {account.handle}: {account.auth_error} (run {cmd} {account.handle})"
    if count_today_fn() >= daily_cap:
        return False, f"daily {label} upload cap reached ({daily_cap})"
    dup = session.scalar(select(Post).where(Post.clip_id == clip.id, Post.account_id == account.id,
                                            Post.status.in_(ACTIVE_POST)))
    if dup is not None:
        return False, f"already {dup.status.value} to {account.handle} (post #{dup.id})"
    return True, ""


def start_post(session: Session, clip: Clip, account: Account, platform: Platform) -> Post:
    """Create or reuse the (clip, account) Post and mark it uploading."""
    post = session.scalar(select(Post).where(Post.clip_id == clip.id, Post.account_id == account.id))
    if post is None:
        post = Post(clip_id=clip.id, account_id=account.id, platform=platform)
        session.add(post)
    post.status, post.error, post.visibility = PostStatus.uploading, None, None
    session.flush()
    return post


def finish_post_success(post_id: int, clip_id: int, platform_post_id: str, post_url: Optional[str],
                        visibility: Optional[str], error: Optional[str] = None, now: Optional[datetime] = None,
                        engine: Optional[Engine] = None) -> Post:
    """Post posted; clip posted; the user gets the submit alert."""
    with get_session(engine) as s:
        post = s.get(Post, post_id)
        post.status, post.platform_post_id, post.post_url = PostStatus.posted, platform_post_id, post_url
        post.posted_at, post.visibility, post.error = now or datetime.now(timezone.utc), visibility, error
        clip = s.get(Clip, clip_id)
        if clip.status != ClipStatus.posted:
            clip.status = ClipStatus.posted
        s.flush()
        s.expunge(post)
    try:
        from app.posting.submit import send_submit_alert
        send_submit_alert(post_id, engine, now=now)
    except Exception as exc:  # noqa: BLE001 — an alert problem must never undo a successful post
        log.warning("submit alert failed for post %d: %s", post_id, type(exc).__name__)
    return post


def finish_post_failure(post_id: int, msg: str, engine: Optional[Engine] = None) -> Optional[Post]:
    """Post failed (the clip stays approved so it can be retried)."""
    log.warning("post %d failed: %s", post_id, msg)
    try:
        with get_session(engine) as s:
            post = s.get(Post, post_id)
            post.status, post.error = PostStatus.failed, msg[:200]
            s.flush()
            s.expunge(post)
            return post
    except Exception as exc:  # noqa: BLE001
        log.error("could not record post failure: %s", type(exc).__name__)
        return None
