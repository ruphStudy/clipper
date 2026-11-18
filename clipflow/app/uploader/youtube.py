"""Upload approved clips to YouTube Shorts. Never raises to callers; never logs secrets.

Unaudited Google API projects get every upload force-locked to private by YouTube. We detect and report
that (post.visibility = "private_locked") instead of fighting it.
"""
import json
import socket
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, or_, select

from app.config import settings
from app.db import get_session
from app.db.enums import ClipStatus, Platform, PostStatus
from app.db.models import Account, AppSetting, Clip, Post
from app.logger import get_logger
from app.notify import notify
from app.uploader.common import (ACTIVE_POST, AuthError, check_preconditions, finish_post_failure,
                                 finish_post_success, get_setting, set_setting, start_post)
from app.uploader.youtube_auth import build_service, get_credentials

log = get_logger("clipflow.youtube")

PT = ZoneInfo("America/Los_Angeles")
CHUNK = 8 * 1024 * 1024
RETRY_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 5
TAGS_MAX_CHARS = 450
BLOCK_KEY = "youtube_quota_block_until"
LOCKED = "private_locked"
LOCK_MSG = "locked private: API project not audited"


def pt_midnight(now: datetime) -> datetime:
    """Start of the current YouTube quota day (Pacific time), as UTC."""
    local = now.astimezone(PT)
    return datetime.combine(local.date(), dtime.min, tzinfo=PT).astimezone(timezone.utc)


def next_pt_midnight(now: datetime) -> datetime:
    local = now.astimezone(PT)
    return datetime.combine(local.date() + timedelta(days=1), dtime.min, tzinfo=PT).astimezone(timezone.utc)


def uploads_today(session, now: datetime, account_id: Optional[int] = None) -> int:
    """YouTube uploads in the current PT day (posted/submitted today, or in flight)."""
    start = pt_midnight(now)
    stmt = select(func.count()).select_from(Post).where(
        Post.platform == Platform.youtube,
        or_(Post.status == PostStatus.uploading,
            (Post.status.in_([PostStatus.posted, PostStatus.submitted])) & (Post.posted_at >= start)))
    if account_id is not None:
        stmt = stmt.where(Post.account_id == account_id)
    return session.scalar(stmt) or 0


def tags_from_hashtags(hashtags: list[str]) -> list[str]:
    """Hashtags without '#', capped so the comma-joined total stays within 450 chars."""
    out, total = [], 0
    for tag in hashtags or []:
        t = tag.lstrip("#").strip()
        if not t or t in out:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if total + cost > TAGS_MAX_CHARS:
            break
        out.append(t)
        total += cost
    return out


def _error_reason(exc) -> tuple[Optional[int], str]:
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = ""
    try:
        data = json.loads(exc.content.decode() if isinstance(exc.content, bytes) else exc.content)
        errors = (data.get("error") or {}).get("errors") or []
        reason = errors[0].get("reason", "") if errors else (data.get("error") or {}).get("status", "")
    except Exception:  # noqa: BLE001
        pass
    return (int(status) if status else None), reason


def _upload_with_retry(request, sleep: Callable[[float], None]) -> dict:
    """Resumable upload loop; retries 5xx and socket errors with exponential backoff (max 5)."""
    from googleapiclient.errors import HttpError

    response, retries = None, 0
    while response is None:
        try:
            _status, response = request.next_chunk()
        except HttpError as exc:
            status, _ = _error_reason(exc)
            if status not in RETRY_STATUS:
                raise
            problem = f"HTTP {status}"
        except (socket.error, ConnectionError, TimeoutError) as exc:
            problem = type(exc).__name__
        else:
            continue
        retries += 1
        if retries > MAX_RETRIES:
            raise RuntimeError(f"upload failed after {MAX_RETRIES} retries ({problem})")
        wait = min(2 ** retries, 64)
        log.warning("youtube upload chunk failed (%s), retry %d in %ss", problem, retries, wait)
        sleep(wait)
    return response


def _warn_locked_once(now: datetime, engine: Optional[Engine]) -> None:
    key = "youtube_lock_warned_" + now.astimezone(PT).strftime("%Y-%m-%d")
    with get_session(engine) as s:
        if get_setting(s, key):
            return
        set_setting(s, key, now.isoformat())
    try:
        notify("YouTube uploads are locked to private",
               "Your Google API project is not audited, so YouTube forces every API upload to private. "
               "Fix: apply for the YouTube API audit (then set YOUTUBE_API_AUDITED=true), or switch YouTube to "
               "manual mode and publish from YouTube Studio.", level="warning")
    except Exception as exc:  # noqa: BLE001
        log.warning("lock notify failed: %s", type(exc).__name__)


def _quota_block(session, now: datetime) -> Optional[str]:
    block = get_setting(session, BLOCK_KEY)
    if block:
        try:
            until = datetime.fromisoformat(block)
            if now < until:
                return f"YouTube quota exhausted until {until.astimezone(PT):%Y-%m-%d %H:%M} PT"
        except ValueError:
            pass
    return None


def upload_youtube(clip_id: int, account_id: int, privacy: Optional[str] = None, engine: Optional[Engine] = None,
                   service_factory: Optional[Callable] = None, sleep: Callable[[float], None] = time.sleep,
                   now: Optional[datetime] = None) -> tuple[Optional[Post], str]:
    """Upload one approved clip. Returns (post or None if refused, message). Never raises."""
    now = now or datetime.now(timezone.utc)
    privacy = (privacy or settings.YOUTUBE_PRIVACY or "public").lower()
    if privacy not in ("public", "unlisted", "private"):
        return None, f"invalid privacy {privacy}"
    try:
        with get_session(engine) as s:
            clip, account = s.get(Clip, clip_id), s.get(Account, account_id)
            ok, refusal = check_preconditions(clip, account, Platform.youtube, settings.YOUTUBE_DAILY_UPLOAD_CAP,
                                              lambda: uploads_today(s, now), s)
            refusal = refusal or _quota_block(s, now)
            if not ok or refusal:
                return None, refusal
            post_id = start_post(s, clip, account, Platform.youtube).id
            caption = (clip.platform_captions or {}).get("youtube") or {}
            title = (caption.get("title") or clip.hook_title or "New clip")[:100]
            description = (caption.get("description") or clip.caption or "")[:5000]
            tags, file_path = tags_from_hashtags(clip.hashtags), clip.file_path
            s.expunge(account)
    except Exception as exc:  # noqa: BLE001
        log.error("youtube upload setup failed: %s", type(exc).__name__)
        return None, f"setup failed: {type(exc).__name__}"

    body = {"snippet": {"title": title, "description": description, "tags": tags,
                        "categoryId": str(settings.YOUTUBE_CATEGORY_ID)},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "containsSyntheticMedia": False}}
    try:
        creds = get_credentials(account, engine)
        youtube = (service_factory or build_service)(creds)
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(file_path, mimetype="video/mp4", chunksize=CHUNK, resumable=True)
        response = _upload_with_retry(youtube.videos().insert(part="snippet,status", body=body, media_body=media),
                                      sleep)
        video_id = response["id"]
        visibility, error = privacy, None
        try:
            items = youtube.videos().list(id=video_id, part="status").execute().get("items") or []
            st = items[0].get("status", {}) if items else {}
            actual = st.get("privacyStatus") or privacy
            rejected = st.get("uploadStatus") == "rejected" or st.get("rejectionReason")
            if (privacy in ("public", "unlisted") and actual == "private") or rejected:
                visibility, error = LOCKED, LOCK_MSG
                if rejected and st.get("rejectionReason"):
                    error = f"{LOCK_MSG} (rejection: {st['rejectionReason']})"
            else:
                visibility = actual
        except Exception as exc:  # noqa: BLE001 — the upload itself succeeded
            log.warning("youtube status check failed for %s: %s", video_id, type(exc).__name__)
        post = finish_post_success(post_id, clip_id, video_id, f"https://www.youtube.com/shorts/{video_id}",
                                   visibility, error, now, engine)
        if visibility == LOCKED:
            _warn_locked_once(now, engine)
        log.info("clip %d uploaded to YouTube as %s (%s)", clip_id, video_id, visibility)
        return post, f"Uploaded #{clip_id} → https://www.youtube.com/shorts/{video_id} ({visibility})"
    except AuthError as exc:
        return finish_post_failure(post_id, f"auth: {exc}", engine), f"auth: {exc}"
    except Exception as exc:  # noqa: BLE001
        status, reason = _error_reason(exc) if hasattr(exc, "resp") else (None, "")
        if reason in ("quotaExceeded", "uploadLimitExceeded"):
            until = next_pt_midnight(now)
            with get_session(engine) as s:
                set_setting(s, BLOCK_KEY, until.isoformat())
            msg = f"youtube {reason}: blocked until {until.astimezone(PT):%Y-%m-%d %H:%M} PT"
        elif status in (401, 403) and reason in ("authError", "forbidden", "insufficientPermissions", ""):
            msg = f"youtube auth error HTTP {status}"
        elif status:
            msg = f"youtube HTTP {status} {reason}".strip()
        else:
            msg = f"youtube upload failed: {str(exc)[:150] if isinstance(exc, RuntimeError) else type(exc).__name__}"
        return finish_post_failure(post_id, msg, engine), msg


def default_account(engine: Optional[Engine] = None) -> Optional[Account]:
    """First active YouTube account without an auth error."""
    with get_session(engine) as s:
        acct = s.scalar(select(Account).where(Account.platform == Platform.youtube, Account.is_active.is_(True))
                        .order_by(Account.auth_error.is_not(None), Account.id))
        if acct is not None:
            s.expunge(acct)
        return acct
