"""Pure URL helpers for footage links (no network)."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.db.enums import SourceKind

MAX_LINKS = 50
TRACKING_PARAMS = {"si", "feature", "fbclid", "igshid"}
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{6,}$")
_DRIVE_FILE = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
_DRIVE_FOLDER = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_SPLIT = re.compile(r"[\s,]+")


def _host(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower()


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def detect_kind(url: str) -> SourceKind:
    """Classify a footage URL."""
    host = _host(url)
    path = urlsplit(url.strip()).path.lower()
    if _host_is(host, "drive.google.com") or _host_is(host, "docs.google.com"):
        return SourceKind.drive
    if host in YOUTUBE_HOSTS or host == "youtu.be":
        return SourceKind.youtube
    if _host_is(host, "twitch.tv"):
        return SourceKind.twitch
    if _host_is(host, "kick.com"):
        return SourceKind.kick
    if path.endswith(VIDEO_EXTS):
        return SourceKind.direct
    return SourceKind.other


def is_drive_folder(url: str) -> bool:
    """True for Google Drive folder links."""
    return _host_is(_host(url), "drive.google.com") and bool(_DRIVE_FOLDER.search(urlsplit(url.strip()).path))


def _clean_query(query: str) -> str:
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    return urlencode(kept)


def _youtube_id(host: str, path: str, query: str) -> str | None:
    if host == "youtu.be":
        vid = path.strip("/").split("/")[0]
        return vid if _YT_ID.match(vid or "") else None
    if path.rstrip("/") == "/watch":
        vid = dict(parse_qsl(query)).get("v", "")
        return vid if _YT_ID.match(vid) else None
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed") and _YT_ID.match(parts[1]):
        return parts[1]
    return None


def normalize_url(url: str) -> str:
    """Canonical form: https, lowercase host, no tracking; YouTube/Drive id forms."""
    raw = url.strip()
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    path, query = parts.path or "", parts.query
    bare = host.split(":")[0]
    if bare in YOUTUBE_HOSTS or bare == "youtu.be":
        vid = _youtube_id(bare, path, query)
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
        return urlunsplit(("https", "www.youtube.com", path, _clean_query(query), ""))
    if _host_is(bare, "drive.google.com"):
        if m := _DRIVE_FOLDER.search(path):
            return f"https://drive.google.com/drive/folders/{m.group(1)}"
        if m := _DRIVE_FILE.search(path):
            return f"https://drive.google.com/file/d/{m.group(1)}/view"
        file_id = dict(parse_qsl(query)).get("id")
        if path.rstrip("/") in ("/open", "/uc") and file_id:
            return f"https://drive.google.com/file/d/{file_id}/view"
    return urlunsplit(("https", host, path, _clean_query(query), ""))


def _valid(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme in ("http", "https") and "." in host and not any(c.isspace() for c in url)


def parse_links(text: str) -> tuple[list[str], list[str]]:
    """Split text into (valid normalized unique URLs, invalid tokens); max 50 valid per call."""
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for token in _SPLIT.split(text or ""):
        token = token.strip().strip("<>\"'")
        if not token:
            continue
        if not _valid(token):
            invalid.append(token)
            continue
        norm = normalize_url(token)
        if norm in seen:
            continue
        seen.add(norm)
        if len(valid) >= MAX_LINKS:
            invalid.append(token)
            continue
        valid.append(norm)
    return valid, invalid
