"""Publish approved clips as Instagram Reels (Graph API, resumable upload). Never raises; never logs tokens."""
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Engine, func, or_, select

from app.config import settings
from app.db import get_session
from app.db.enums import Platform, PostStatus
from app.db.models import Account, Clip, Post
from app.logger import get_logger
from app.uploader.common import (AuthError, check_preconditions, finish_post_failure, finish_post_success,
                                 get_setting, set_setting, start_post)
from app.uploader.instagram_auth import GraphError, graph_call, load_token, mark_reconnect

log = get_logger("clipflow.instagram")

RUPLOAD = "https://rupload.facebook.com/ig-api-upload/{version}/{container}"
UPLOAD_TIMEOUT = 600.0
POLL_EVERY = 10
POLL_MAX = 60  # 10 minutes
READ_CHUNK = 1024 * 1024
BLOCK_HOURS = 24
API_RATE_CODES = {4, 17, 32, 613}


def _block_key(account_id: int) -> str:
    return f"ig_block_until_{account_id}"


def uploads_today(session, now: datetime, account_id: int) -> int:
    """Instagram posts for an account in the current day (TIMEZONE), including ones in flight."""
    tz = ZoneInfo(settings.TIMEZONE)
    start = datetime.combine(now.astimezone(tz).date(), dtime.min, tzinfo=tz).astimezone(timezone.utc)
    return session.scalar(select(func.count()).select_from(Post).where(
        Post.platform == Platform.instagram, Post.account_id == account_id,
        or_(Post.status == PostStatus.uploading,
            (Post.status.in_([PostStatus.posted, PostStatus.submitted])) & (Post.posted_at >= start)))) or 0


def _file_chunks(path: str) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        while chunk := fh.read(READ_CHUNK):
            yield chunk


def _rupload(client: httpx.Client, uri: str, token: str, path: str) -> None:
    """Stream the file to rupload.facebook.com; one retry from offset 0 on a network error."""
    size = Path(path).stat().st_size
    headers = {"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}
    for attempt in (1, 2):
        try:
            resp = client.post(uri, content=_file_chunks(path), headers=headers, timeout=UPLOAD_TIMEOUT)
        except httpx.TransportError as exc:
            if attempt == 1:
                log.warning("instagram upload network error (%s); retrying from offset 0", type(exc).__name__)
                continue
            raise GraphError(f"upload network error: {type(exc).__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or not body.get("success", resp.status_code < 300):
            info = body.get("debug_info") or body.get("error") or {}
            raise GraphError(f"upload failed: {str(info.get('message') or 'HTTP ' + str(resp.status_code))[:150]}",
                             info.get("code"), info.get("error_subcode"), resp.status_code)
        return


def _wait_ready(client: httpx.Client, container: str, token: str, sleep: Callable[[float], None]) -> None:
    for i in range(POLL_MAX):
        data = graph_call(client, "GET", container, token, params={"fields": "status_code,status"})
        code = data.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise GraphError(f"container {code.lower()}: {str(data.get('status') or '')[:150]}".rstrip(": "))
        if i < POLL_MAX - 1:
            sleep(POLL_EVERY)
    raise GraphError("processing timeout")


def upload_instagram(clip_id: int, account_id: int, engine: Optional[Engine] = None,
                     client: Optional[httpx.Client] = None, sleep: Callable[[float], None] = time.sleep,
                     now: Optional[datetime] = None) -> tuple[Optional[Post], str]:
    """Publish one approved clip as a Reel. Returns (post or None if refused, message). Never raises."""
    now = now or datetime.now(timezone.utc)
    own = client is None
    client = client or httpx.Client(timeout=UPLOAD_TIMEOUT)
    post_id = None
    try:
        with get_session(engine) as s:
            clip, account = s.get(Clip, clip_id), s.get(Account, account_id)
            ok, refusal = check_preconditions(clip, account, Platform.instagram, settings.INSTAGRAM_DAILY_CAP,
                                              lambda: uploads_today(s, now, account_id), s)
            if not ok:
                return None, refusal
            block = get_setting(s, _block_key(account_id))
            if block:
                try:
                    until = datetime.fromisoformat(block)
                    if now < until:
                        return None, f"instagram account @{account.handle} rate-limited until {until:%Y-%m-%d %H:%M} UTC"
                except ValueError:
                    pass
            caption = ((clip.platform_captions or {}).get("instagram") or {}).get("caption") or clip.caption or ""
            file_path = clip.file_path
            s.expunge(account)
        try:
            ig_user_id, token = load_token(account)
        except AuthError as exc:
            mark_reconnect(account_id, engine)
            return None, f"auth: {exc}"

        limit = graph_call(client, "GET", f"{ig_user_id}/content_publishing_limit", token,
                           params={"fields": "quota_usage,config"}).get("data") or [{}]
        usage = limit[0].get("quota_usage") or 0
        total = (limit[0].get("config") or {}).get("quota_total")
        if total is not None and usage >= total:
            return None, "instagram publish limit reached"

        with get_session(engine) as s:
            post_id = start_post(s, s.get(Clip, clip_id), s.get(Account, account_id), Platform.instagram).id

        created = graph_call(client, "POST", f"{ig_user_id}/media", token, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
            "share_to_feed": "true" if settings.INSTAGRAM_SHARE_TO_FEED else "false", "thumb_offset": "1000"})
        container = str(created["id"])
        uri = created.get("uri") or RUPLOAD.format(version=settings.META_GRAPH_VERSION, container=container)
        _rupload(client, uri, token, file_path)
        _wait_ready(client, container, token, sleep)
        media_id = str(graph_call(client, "POST", f"{ig_user_id}/media_publish", token,
                                  data={"creation_id": container})["id"])
        try:
            meta = graph_call(client, "GET", media_id, token, params={"fields": "permalink,media_product_type"})
            permalink = meta.get("permalink")
        except GraphError as exc:  # published anyway; the link can be filled later
            log.warning("instagram permalink lookup failed: %s", exc)
            permalink = None
        post = finish_post_success(post_id, clip_id, media_id, permalink, "public", None, now, engine)
        log.info("clip %d published to Instagram as %s", clip_id, media_id)
        return post, f"Published #{clip_id} → {permalink or media_id}"
    except GraphError as exc:
        if exc.code == 190:
            mark_reconnect(account_id, engine)
            msg = "auth: instagram token invalid (run ig-connect)"
        elif exc.code == 9 or exc.subcode == 2207042:
            with get_session(engine) as s:
                set_setting(s, _block_key(account_id), (now + timedelta(hours=BLOCK_HOURS)).isoformat())
            msg = "rate limited (account blocked for 24h)"
        elif exc.code in API_RATE_CODES:
            msg = "api rate limit, retry later"
        else:
            msg = f"instagram: {exc}"[:200]
        if post_id is None:
            return None, msg
        return finish_post_failure(post_id, msg, engine), msg
    except Exception as exc:  # noqa: BLE001
        msg = f"instagram upload failed: {type(exc).__name__}"
        log.error(msg)
        return (finish_post_failure(post_id, msg, engine) if post_id else None), msg
    finally:
        if own:
            client.close()


def default_account(engine: Optional[Engine] = None) -> Optional[Account]:
    """First active Instagram account (accounts without auth errors first)."""
    with get_session(engine) as s:
        acct = s.scalar(select(Account).where(Account.platform == Platform.instagram, Account.is_active.is_(True))
                        .order_by(Account.auth_error.is_not(None), Account.id))
        if acct is not None:
            s.expunge(acct)
        return acct
