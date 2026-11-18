"""ffmpeg audio helpers (mono 16 kHz, small temp files)."""
import math
import subprocess
from pathlib import Path

from app.downloader.media import probe

CODECS = {"flac": ["-c:a", "flac"], "mp3": ["-c:a", "libmp3lame", "-b:a", "32k"]}
TIMEOUT_SEC = 3600


class AudioError(Exception):
    """ffmpeg could not produce the audio file."""


def _run(args: list[str]) -> None:
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True,
                             timeout=TIMEOUT_SEC, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"ffmpeg failed: {type(exc).__name__}") from exc
    if out.returncode != 0:
        raise AudioError((out.stderr or "ffmpeg error").strip().splitlines()[-1][:200])


def extract_audio(video_path: str | Path, out_path: str | Path, fmt: str = "flac") -> Path:
    """Write mono 16 kHz audio (flac for local, 32 kbps mp3 for the API)."""
    if fmt not in CODECS:
        raise ValueError(f"unsupported audio format: {fmt}")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["-i", str(video_path), "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000", *CODECS[fmt], str(out)])
    if not out.exists() or out.stat().st_size == 0:
        raise AudioError("no audio track")
    return out


def decode_pcm(path: str | Path):
    """Decode audio to a float32 mono 16 kHz numpy array in memory (no WAV on disk)."""
    import numpy as np

    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "16000",
                              "pipe:1"], capture_output=True, timeout=TIMEOUT_SEC, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"ffmpeg decode failed: {type(exc).__name__}") from exc
    if out.returncode != 0 or not out.stdout:
        raise AudioError((out.stderr.decode(errors="ignore") or "decode error").strip().splitlines()[-1][:200])
    return np.frombuffer(out.stdout, dtype=np.float32)


def split_audio(path: str | Path, chunk_sec: int = 600) -> list[tuple[Path, float]]:
    """Cut audio into chunk_sec pieces (re-encoded, exact offsets): [(chunk_path, offset_sec)]."""
    path = Path(path)
    duration = probe(path)["duration_sec"] or 0
    if duration <= chunk_sec:
        return [(path, 0.0)]
    fmt = path.suffix.lstrip(".").lower()
    codec = CODECS.get(fmt, [])
    chunks = []
    for i in range(math.ceil(duration / chunk_sec)):
        offset = float(i * chunk_sec)
        if duration - offset < 0.05:
            break
        out = path.with_name(f"{path.stem}.part{i:03d}{path.suffix}")
        _run(["-ss", str(offset), "-t", str(chunk_sec), "-i", str(path), "-ac", "1", "-ar", "16000", *codec, str(out)])
        chunks.append((out, offset))
    return chunks
