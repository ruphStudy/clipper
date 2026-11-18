"""Transcription orchestration: claim → audio → backend → JSON; never raises to callers."""
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, or_, select, update

from app.config import settings
from app.db import get_session
from app.db.enums import CampaignStatus, SourceStatus
from app.db.models import Campaign, Source
from app.logger import get_logger
from app.notify import notify_error
from app.rules import get_rules
from app.clipper.audio import extract_audio, split_audio
from app.clipper.transcribe import TranscribeError, get_backend, stats

log = get_logger("clipflow.transcribe")

MAX_ATTEMPTS = 2
CHUNK_SEC = 600
ERROR_MAX = 200


def _attempts_ok():
    return or_(Source.transcribe_attempts.is_(None), Source.transcribe_attempts < MAX_ATTEMPTS)


def _claim(source_id: int, engine: Optional[Engine]) -> Optional[tuple[str, int]]:
    """downloaded + no transcript + file exists + attempts<2 → processing. Returns (local_path, campaign_id)."""
    with get_session(engine) as s:
        src = s.get(Source, source_id)
        if src is None or not src.local_path or not Path(src.local_path).is_file():
            return None
        res = s.execute(update(Source).where(
            Source.id == source_id, Source.status == SourceStatus.downloaded, Source.transcript_path.is_(None),
            _attempts_ok()).values(status=SourceStatus.processing, updated_at=datetime.now(timezone.utc)))
        if res.rowcount != 1:
            return None
        return src.local_path, src.campaign_id


def _language(campaign_id: int, engine: Optional[Engine]) -> Optional[str]:
    with get_session(engine) as s:
        lang = (get_rules(s.get(Campaign, campaign_id)).language or "").strip().lower()
    return None if lang in ("", "auto") else lang


def transcribe_source(source_id: int, engine: Optional[Engine] = None, backend=None) -> bool:
    """Transcribe one downloaded source. Returns True on success. Never raises."""
    try:
        claimed = _claim(source_id, engine)
    except Exception as exc:  # noqa: BLE001
        log.error("source %d: transcribe claim failed: %s", source_id, exc)
        return False
    if claimed is None:
        return False
    local_path, campaign_id = claimed
    tmp_dir = Path(settings.TRANSCRIPTS_DIR) / f".tmp_{source_id}"
    started = time.monotonic()
    try:
        backend = backend or get_backend()
        language = _language(campaign_id, engine)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        audio = extract_audio(local_path, tmp_dir / f"{source_id}.{backend.audio_format}", backend.audio_format)
        if backend.name == "openai":
            from app.downloader.media import probe
            duration = probe(audio)["duration_sec"]
            transcript = backend.transcribe_chunks(split_audio(audio, CHUNK_SEC), source_id, language, duration)
        else:
            transcript = backend.transcribe(audio, source_id, language)
        out = Path(settings.TRANSCRIPTS_DIR) / f"{source_id}.json"
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(transcript, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(out)
        st = stats(transcript)
        with get_session(engine) as s:
            src = s.get(Source, source_id)
            src.transcript_path, src.transcribed_at = str(out), datetime.now(timezone.utc)
            src.language, src.speech_ratio, src.word_count = transcript.get("language"), st["speech_ratio"], st["word_count"]
            src.status, src.error = SourceStatus.downloaded, None
        log.info("source %d transcribed: %d words, speech %.0f%%, lang %s, %.1fs", source_id, st["word_count"],
                 st["speech_ratio"] * 100, transcript.get("language"), time.monotonic() - started)
        return True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) if isinstance(exc, TranscribeError) else f"{type(exc).__name__}: {exc}"
        msg = f"transcribe: {msg}".replace("\n", " ")
        msg = msg if len(msg) <= ERROR_MAX else msg[: ERROR_MAX - 1] + "…"
        attempts = 0
        try:
            with get_session(engine) as s:
                src = s.get(Source, source_id)
                src.transcribe_attempts = (src.transcribe_attempts or 0) + 1
                attempts = src.transcribe_attempts
                src.status, src.error = SourceStatus.downloaded, msg
        except Exception as db_exc:  # noqa: BLE001
            log.error("source %d: could not record transcribe failure: %s", source_id, db_exc)
        log.warning("source %d %s (attempt %d)", source_id, msg, attempts)
        if attempts >= MAX_ATTEMPTS:
            try:
                notify_error(f"transcribe source #{source_id}", exc)
            except Exception:  # noqa: BLE001
                pass
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def transcribe_pending(limit: int = 1, engine: Optional[Engine] = None, backend=None) -> dict:
    """Transcribe the oldest downloaded, untranscribed sources of joined campaigns."""
    with get_session(engine) as s:
        ids = list(s.scalars(
            select(Source.id).join(Campaign, Source.campaign_id == Campaign.id)
            .where(Source.status == SourceStatus.downloaded, Source.transcript_path.is_(None), _attempts_ok(),
                   Source.local_path.is_not(None), Campaign.status == CampaignStatus.joined)
            .order_by(Source.downloaded_at, Source.id).limit(limit)))
    summary = {"done": 0, "failed": 0}
    for sid in ids:
        summary["done" if transcribe_source(sid, engine, backend) else "failed"] += 1
    return summary


def load_transcript(source: Source) -> dict:
    """Read a source's transcript JSON."""
    if not source.transcript_path:
        raise FileNotFoundError(f"source {source.id} has no transcript")
    return json.loads(Path(source.transcript_path).read_text(encoding="utf-8"))


def words_in_range(transcript: dict, start: float, end: float) -> list[dict]:
    """Words fully inside [start, end] (absolute seconds), in order."""
    return [w for seg in transcript.get("segments") or [] if seg["end"] >= start and seg["start"] <= end
            for w in seg.get("words") or [] if w["s"] >= start and w["e"] <= end]
