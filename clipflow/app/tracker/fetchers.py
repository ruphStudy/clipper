"""Platform stats fetchers. Each returns {post_id: {"views","likes","comments"[, "platform_post_id"]}} or
{post_id: {"error": "..."}}. Never raises; never logs tokens or keys."""
import logging
from typing import Callable, Optional

import httpx
from sqlalchemy import Engine

from app.config import settings
from app.db import get_session
from app.db.models import Account, Post
from app.logger import get_logger
from app.posting.manual import normalize_post_url
from app.uploader.common import AuthError

log = get_logger("clipflow.tracker.fetch")
logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry the API key

YT_BATCH = 50
YT_API = "https://www.googleapis.com/youtube/v3/videos"
IG_VIEW_METRICS = ("views", "plays", "ig_reels_aggregated_all_plays_count")
_ig_views_metric: Optional[str] = None  # the first metric the API accepted (cached per process)


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _yt_from_stats(stats: dict) -> dict:
    return {"views": _int(stats.get("viewCount")) or 0, "likes": _int(stats.get("likeCount")),
            "comments": _int(stats.get("commentCount"))}


def _batches(items: list, n: int) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def youtube_stats(posts: list[Post], engine: Optional[Engine] = None, service_factory: Optional[Callable] = None,
                  client: Optional[httpx.Client] = None) -> dict:
    """Video statistics via the account's OAuth creds, else YOUTUBE_API_KEY (public stats)."""
    from app.uploader.youtube_auth import build_service, get_credentials

    out: dict = {}
    by_service: dict = {}
    keyless: list[Post] = []
    accounts: dict[int, Optional[object]] = {}
    for p in posts:
        if not p.platform_post_id:
            out[p.id] = {"error": "no video id"}
            continue
        if p.account_id not in accounts:
            with get_session(engine) as s:
                acct = s.get(Account, p.account_id)
                if acct is not None:
                    s.expunge(acct)
            service = None
            if acct is not None and acct.credentials_ref and not acct.auth_error:
                try:
                    service = (service_factory or build_service)(get_credentials(acct, engine))
                except (AuthError, Exception) as exc:  # noqa: BLE001
                    log.warning("youtube stats: account %s unusable (%s)", p.account_id, type(exc).__name__)
            accounts[p.account_id] = service
        service = accounts[p.account_id]
        if service is not None:
            by_service.setdefault(id(service), (service, []))[1].append(p)
        else:
            keyless.append(p)

    for service, group in by_service.values():
        for batch in _batches(group, YT_BATCH):
            try:
                resp = service.videos().list(part="statistics", id=",".join(p.platform_post_id for p in batch),
                                             maxResults=YT_BATCH).execute()
            except Exception as exc:  # noqa: BLE001
                for p in batch:
                    out[p.id] = {"error": f"youtube api: {type(exc).__name__}"}
                continue
            found = {it["id"]: it.get("statistics", {}) for it in resp.get("items") or []}
            for p in batch:
                out[p.id] = _yt_from_stats(found[p.platform_post_id]) if p.platform_post_id in found \
                    else {"error": "removed/private"}

    if keyless:
        if not settings.YOUTUBE_API_KEY:
            for p in keyless:
                out[p.id] = {"error": "no youtube credentials"}
            return out
        own = client is None
        client = client or httpx.Client(timeout=30)
        try:
            for batch in _batches(keyless, YT_BATCH):
                try:
                    resp = client.get(YT_API, params={"part": "statistics", "maxResults": YT_BATCH,
                                                      "id": ",".join(p.platform_post_id for p in batch),
                                                      "key": settings.YOUTUBE_API_KEY})
                    data = resp.json() if resp.status_code == 200 else None
                except (httpx.HTTPError, ValueError) as exc:
                    data, err = None, type(exc).__name__
                else:
                    err = f"HTTP {resp.status_code}"
                if data is None:
                    for p in batch:
                        out[p.id] = {"error": f"youtube api key: {err}"}
                    continue
                found = {it["id"]: it.get("statistics", {}) for it in data.get("items") or []}
                for p in batch:
                    out[p.id] = _yt_from_stats(found[p.platform_post_id]) if p.platform_post_id in found \
                        else {"error": "removed/private"}
        finally:
            if own:
                client.close()
    return out


def _insight_value(item: dict) -> Optional[int]:
    if "total_value" in item:
        return _int((item.get("total_value") or {}).get("value"))
    values = item.get("values") or []
    return _int(values[-1].get("value")) if values else None


def _ig_insights(client, media_id: str, token: str) -> dict:
    """views/likes/comments for one IG media, falling back across view metric names (error 100)."""
    from app.uploader.instagram_auth import GraphError, graph_call

    global _ig_views_metric
    metrics = [_ig_views_metric] if _ig_views_metric else list(IG_VIEW_METRICS)
    last_exc = None
    for metric in metrics:
        try:
            data = graph_call(client, "GET", f"{media_id}/insights", token,
                              params={"metric": f"{metric},likes,comments"})
        except GraphError as exc:
            if exc.code == 100 and len(metrics) > 1:
                last_exc = exc
                continue
            raise
        _ig_views_metric = metric
        vals = {item.get("name"): _insight_value(item) for item in data.get("data") or []}
        return {"views": vals.get(metric) or 0, "likes": vals.get("likes"), "comments": vals.get("comments")}
    raise last_exc or GraphError("no supported views metric")


def instagram_stats(posts: list[Post], engine: Optional[Engine] = None, client: Optional[httpx.Client] = None) -> dict:
    """Reel insights per post (page token of the post's account); resolves ids of manual posts by permalink."""
    from app.uploader.instagram_auth import GraphError, graph_call, load_token

    out: dict = {}
    own = client is None
    client = client or httpx.Client(timeout=30)
    tokens: dict[int, Optional[tuple[str, str]]] = {}
    media_cache: dict[int, dict[str, str]] = {}
    try:
        for p in posts:
            if p.account_id not in tokens:
                with get_session(engine) as s:
                    acct = s.get(Account, p.account_id)
                    if acct is not None:
                        s.expunge(acct)
                try:
                    tokens[p.account_id] = load_token(acct) if acct is not None else None
                except AuthError:
                    tokens[p.account_id] = None
            creds = tokens[p.account_id]
            if creds is None:
                out[p.id] = {"error": "no instagram credentials"}
                continue
            ig_user_id, token = creds
            try:
                media_id, resolved = p.platform_post_id, None
                if not media_id:
                    if p.account_id not in media_cache:
                        listing = graph_call(client, "GET", f"{ig_user_id}/media", token,
                                             params={"fields": "id,permalink", "limit": 50})
                        media_cache[p.account_id] = {
                            normalize_post_url("instagram", m.get("permalink") or "")[0]: m["id"]
                            for m in listing.get("data") or [] if m.get("permalink")}
                    target = normalize_post_url("instagram", p.post_url or "")[0]
                    media_id = resolved = media_cache[p.account_id].get(target)
                    if not media_id:
                        out[p.id] = {"error": "media not found by permalink"}
                        continue
                stats = _ig_insights(client, media_id, token)
                if resolved:
                    stats["platform_post_id"] = resolved
                out[p.id] = stats
            except GraphError as exc:
                out[p.id] = {"error": f"instagram: {str(exc)[:120]}"}
    finally:
        if own:
            client.close()
    return out
