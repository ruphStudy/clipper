"""Per-kind fetchers. Each returns the downloaded file Path or raises DownloadError(short message)."""
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import gdown
import httpx
import yt_dlp

from app.config import settings
from app.logger import get_logger

log = get_logger("clipflow.downloader.fetch")

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
GB = 1024 ** 3
MAX_FOLDER_FILES = 100
USER_AGENT = "ClipFlow/0.1 personal clipping tool"


class DownloadError(Exception):
    """Download failed; message is short and user-facing."""


def _short(msg: str, limit: int = 200) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(msg)).replace("ERROR: ", "").strip()
    msg = msg.splitlines()[0] if msg else "unknown error"
    return msg if len(msg) <= limit else msg[: limit - 1] + "…"


def map_ytdlp_error(msg: str) -> str:
    """Map yt-dlp errors to short, actionable messages."""
    low = (msg or "").lower()
    if "private video" in low:
        return "private/unavailable"
    if "sign in to confirm" in low or "not a bot" in low or "use --cookies" in low or "login required" in low:
        return "sign-in/bot check (set YTDLP_COOKIES_FILE)"
    if "geo" in low or "in your country" in low or "from your location" in low:
        return "geo-blocked"
    if "larger than max-filesize" in low or "max_filesize" in low:
        return "too large"
    if "too long" in low:
        return "too long"
    if any(k in low for k in ("private video", "video unavailable", "unavailable", "removed", "does not exist",
                              "http error 404", "members-only", "this video is private")):
        return "private/unavailable"
    return _short(msg)


def _duration_filter(info: dict, *, incomplete: bool = False) -> Optional[str]:
    duration = info.get("duration")
    if duration and duration > settings.MAX_SOURCE_MINUTES * 60:
        return f"too long ({int(duration // 60)} min > {settings.MAX_SOURCE_MINUTES} min)"
    return None


def _found(out_base: Path, marker: str) -> Optional[Path]:
    files = [p for p in out_base.parent.glob(f"{out_base.name}{marker}*")
             if p.is_file() and not p.name.endswith((".part", ".ytdl"))
             and not any(re.fullmatch(r"\.f\d+(-\d+)?", sfx) for sfx in p.suffixes[:-1])]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def ytdlp_fetch(url: str, out_base: Path, ydl_cls: Optional[Callable] = None) -> Path:
    """Download with yt-dlp (youtube/twitch/kick/other)."""
    ydl_cls = ydl_cls or yt_dlp.YoutubeDL
    h = settings.DOWNLOAD_MAX_HEIGHT
    opts = {
        "format": f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}]/b",
        "merge_output_format": "mp4",
        "outtmpl": f"{out_base}.dl.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
        "match_filter": _duration_filter,
        "max_filesize": int(settings.MAX_SOURCE_GB * GB),
        "overwrites": True,
    }
    if settings.YTDLP_COOKIES_FILE:
        opts["cookiefile"] = settings.YTDLP_COOKIES_FILE
    try:
        with ydl_cls(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
    except Exception as exc:  # yt_dlp.utils.DownloadError and friends
        raise DownloadError(map_ytdlp_error(str(exc))) from exc
    path = _found(out_base, ".dl.")
    if path is None:
        if _duration_filter(info):
            raise DownloadError("too long")
        size = info.get("filesize") or info.get("filesize_approx")
        if size and size > settings.MAX_SOURCE_GB * GB:
            raise DownloadError("too large")
        raise DownloadError("no file downloaded")
    return path


def _looks_html(path: Path) -> bool:
    with path.open("rb") as fh:
        head = fh.read(512).lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head[:200]


def drive_fetch(url: str, out_base: Path, downloader: Optional[Callable] = None) -> Path:
    """Download a public Google Drive file."""
    downloader = downloader or gdown.download
    target = Path(f"{out_base}.dl.part")
    try:
        result = downloader(url, output=str(target), quiet=True, fuzzy=True)
    except Exception as exc:  # gdown raises FileURLRetrievalError etc.
        target.unlink(missing_ok=True)
        low = str(exc).lower()
        if "too many users" in low or "quota" in low:
            raise DownloadError("drive: quota exceeded") from exc
        if "permission" in low or "cannot retrieve the public link" in low or "not found" in low:
            raise DownloadError("drive: not public") from exc
        raise DownloadError("drive: not public or quota exceeded") from exc
    path = Path(result) if result else target
    if not path.exists() or path.stat().st_size == 0 or _looks_html(path):
        path.unlink(missing_ok=True)
        raise DownloadError("drive: not public or quota exceeded")
    return path


def direct_fetch(url: str, out_base: Path, client: Optional[httpx.Client] = None) -> Path:
    """Stream a direct video URL to disk."""
    limit = settings.MAX_SOURCE_GB * GB
    ext = Path(urlsplit(url).path).suffix.lower()
    target = Path(f"{out_base}.dl{ext if ext in VIDEO_EXTS else '.mp4'}.part")
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=60, headers={"User-Agent": USER_AGENT})
    try:
        with client.stream("GET", url, follow_redirects=True, timeout=60) as resp:
            if resp.status_code != 200:
                raise DownloadError(f"HTTP {resp.status_code}")
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if not (ctype.startswith("video/") or ctype == "application/octet-stream"):
                raise DownloadError(f"not a video ({ctype or 'no content-type'})")
            declared = int(resp.headers.get("content-length") or 0)
            if declared > limit:
                raise DownloadError("too large")
            written = 0
            with target.open("wb") as fh:
                for chunk in resp.iter_bytes(1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise DownloadError("too large")
                    fh.write(chunk)
    except DownloadError:
        target.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as exc:
        target.unlink(missing_ok=True)
        raise DownloadError(f"network error: {type(exc).__name__}") from exc
    finally:
        if own:
            client.close()
    return target


def expand_drive_folder(url: str, lister: Optional[Callable] = None) -> list[tuple[str, str]]:
    """List video files in a public Drive folder (no download): [(file_url, name)], max 100."""
    lister = lister or gdown.download_folder
    try:
        items = lister(url=url, skip_download=True, quiet=True, remaining_ok=True) or []
    except Exception as exc:
        raise DownloadError("drive folder: not public or listing failed") from exc
    out: list[tuple[str, str]] = []
    for item in items:
        file_id = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None)
        name = getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else "") or ""
        if file_id and Path(name).suffix.lower() in VIDEO_EXTS:
            out.append((f"https://drive.google.com/file/d/{file_id}/view", Path(name).name))
        if len(out) >= MAX_FOLDER_FILES:
            break
    return out
