"""ffprobe and disk-space helpers."""
import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app.config import settings

GB = 1024 ** 3
MIN_FREE_GB = 2.0


class ProbeError(Exception):
    """ffprobe could not read the file."""


def probe(path: str | Path) -> dict:
    """Return {duration_sec, width, height, has_audio, has_video, format_name}."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"ffprobe failed: {type(exc).__name__}") from exc
    if out.returncode != 0:
        raise ProbeError((out.stderr or "ffprobe error").strip().splitlines()[-1][:200])
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    duration = (data.get("format") or {}).get("duration") or (video or {}).get("duration")
    return {
        "duration_sec": round(float(duration), 3) if duration not in (None, "N/A") else None,
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "has_video": video is not None,
        "format_name": (data.get("format") or {}).get("format_name", ""),
    }


def disk_usage_gb(directory: Optional[str | Path] = None) -> float:
    """Total size of files under the download directory, in GB."""
    root = Path(directory or settings.DOWNLOAD_DIR)
    if not root.exists():
        return 0.0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) / GB


def volume_used_pct(directory: Optional[str | Path] = None) -> float:
    """Percent used of the volume holding the download directory."""
    root = Path(directory or settings.DOWNLOAD_DIR)
    root.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(root)
    return du.used / du.total * 100 if du.total else 0.0


def has_space(extra_gb: float, directory: Optional[str | Path] = None) -> bool:
    """True if extra_gb fits under MAX_DISK_GB and leaves ≥2 GB free on the volume."""
    root = Path(directory or settings.DOWNLOAD_DIR)
    root.mkdir(parents=True, exist_ok=True)
    if disk_usage_gb(root) + extra_gb > settings.MAX_DISK_GB:
        return False
    return shutil.disk_usage(root).free / GB - extra_gb >= MIN_FREE_GB
