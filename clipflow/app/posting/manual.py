"""Manual posting: hand a post to the user (Telegram) and record the link they post back."""
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Engine, select

from app.config import settings
from app.db import get_session
from app.db.enums import ClipStatus, PostStatus
from app.db.models import Account, Campaign, Clip, Post
from app.logger import get_logger
from app.notify import esc, get_notifier
from app.rules import get_rules
from app.rules.caption import render_for_platform

log = get_logger("clipflow.manual")

MARKABLE = (PostStatus.manual_pending, PostStatus.scheduled, PostStatus.failed, PostStatus.uploading)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")


def normalize_post_url(platform: str, url: str) -> tuple[Optional[str], Optional[str], str]:
    """(normalized url, platform post id or None, error) — error is '' when valid."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return None, None, "not a link (must start with https://)"
    parts = urlsplit(url)
    host, path = _host(url), parts.path.rstrip("/")
    if platform == "youtube":
        if host == "youtu.be" and path.strip("/"):
            vid = path.strip("/").split("/")[0]
        elif host in ("youtube.com", "music.youtube.com"):
            m = re.match(r"^/(?:shorts|live|embed)/([\w-]{6,})", path)
            vid = m.group(1) if m else (parse_qs(parts.query).get("v") or [None])[0]
        else:
            return None, None, "not a YouTube link"
        if not vid:
            return None, None, "no video id in the YouTube link"
        return f"https://www.youtube.com/shorts/{vid}", vid, ""
    if platform == "instagram":
        m = re.match(r"^/(reels?|p|tv)/([\w-]+)", path) if host == "instagram.com" else None
        if not m:
            return None, None, "not an Instagram post/reel link"
        kind = "reel" if m.group(1).startswith("reel") else m.group(1)
        return f"https://www.instagram.com/{kind}/{m.group(2)}/", m.group(2), ""
    if platform == "tiktok":
        if host not in ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com"):
            return None, None, "not a TikTok link"
        m = re.search(r"/video/(\d+)", path)
        return f"https://{parts.hostname.lower()}{path}", (m.group(1) if m else None), ""
    if platform == "x":
        m = re.match(r"^/([\w]+)/status/(\d+)", path) if host in ("x.com", "twitter.com") else None
        if not m:
            return None, None, "not an X post link"
        return f"https://x.com/{m.group(1)}/status/{m.group(2)}", m.group(2), ""
    return None, None, f"unknown platform {platform}"


def mark_posted(post_id: int, url: str, engine: Optional[Engine] = None, alert: bool = True,
                notifier=None) -> tuple[bool, str]:
    """Record a manually published post from its public link (then send the submit alert)."""
    with get_session(engine) as s:
        post = s.get(Post, post_id)
        if post is None:
            return False, f"post #{post_id} not found"
        if post.status in (PostStatus.posted, PostStatus.submitted):
            return False, f"post #{post_id} is already {post.status.value}"
        if post.status not in MARKABLE:
            return False, f"post #{post_id} is {post.status.value}"
        norm, pid, err = normalize_post_url(post.platform.value, url)
        if err:
            return False, f"post #{post_id}: {err}"
        dup = s.scalar(select(Post).where(Post.post_url == norm, Post.id != post_id))
        if dup is not None:
            return False, f"that link is already recorded on post #{dup.id}"
        post.status, post.post_url, post.platform_post_id = PostStatus.posted, norm, pid
        post.posted_at, post.visibility, post.error = datetime.now(timezone.utc), "public", None
        clip = s.get(Clip, post.clip_id)
        if clip is not None:
            clip.status = ClipStatus.posted
    if alert:
        try:
            from app.posting.submit import send_submit_alert
            send_submit_alert(post_id, engine, notifier)
        except Exception as exc:  # noqa: BLE001
            log.warning("submit alert failed for post %d: %s", post_id, type(exc).__name__)
    return True, f"Post #{post_id} marked posted: {norm}"


def caption_text(clip: Clip, platform: str, campaign: Optional[Campaign] = None) -> str:
    """Copy-ready caption for a platform (youtube: title + blank line + description)."""
    data = (clip.platform_captions or {}).get(platform)
    if not data and campaign is not None:
        try:
            data = render_for_platform(get_rules(campaign), clip.hook_title or "", platform)
        except ValueError:
            data = {}
    data = data or {}
    if platform == "youtube":
        return f"{data.get('title', '')}\n\n{data.get('description', '')}".strip()
    return (data.get("caption") or data.get("text") or clip.caption or "").strip()


def send_manual_handoff(post_id: int, engine: Optional[Engine] = None, notifier=None) -> bool:
    """Send the video + caption to Telegram; the post becomes manual_pending either way. Returns True if sent."""
    notifier = notifier or get_notifier()
    with get_session(engine) as s:
        post = s.get(Post, post_id)
        clip, account = s.get(Clip, post.clip_id), s.get(Account, post.account_id)
        campaign = s.get(Campaign, clip.campaign_id)
        platform, handle = post.platform.value, account.handle if account else "?"
        text = caption_text(clip, platform, campaign)
        file_path, campaign_name = clip.file_path, (campaign.name if campaign else "")
    sent = False
    if notifier.enabled:
        short = f"#{post_id} {platform} @{handle.lstrip('@')} · {esc(campaign_name[:60])}"
        video_ok = bool(file_path) and notifier.send_video(file_path, short)
        body = (f"<b>#{post_id} {esc(platform)}</b> @{esc(handle.lstrip('@'))}\n<pre>{esc(text)}</pre>\n"
                f"After posting reply: /posted {post_id} &lt;link&gt;")
        if settings.DASHBOARD_PUBLIC_URL:
            body += f"\n{esc(settings.DASHBOARD_PUBLIC_URL.rstrip('/'))}/posts?tab=manual"
        sent = notifier.send_message(body) and video_ok
    with get_session(engine) as s:
        post = s.get(Post, post_id)
        post.status, post.notified_at, post.mode = PostStatus.manual_pending, datetime.now(timezone.utc), "manual"
    log.info("post %d handed off for manual posting (telegram %s)", post_id, "sent" if sent else "not sent")
    return sent
