"""Pick clip moments for transcribed sources and store them as candidate clips. Never raises."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import Engine, and_, or_, select, update

from app.ai.gemini import generate_json
from app.ai.usage import budget_ok, warn_budget_once
from app.clipper.moments import Moment, pick_ai, pick_heuristic, refine
from app.clipper.transcribe_service import load_transcript
from app.config import settings
from app.db import get_session
from app.db.enums import CampaignStatus, ClipStatus, SourceStatus
from app.db.models import Campaign, Clip, Source
from app.logger import get_logger
from app.rules import get_rules

log = get_logger("clipflow.moments")

MAX_ATTEMPTS = 2
MIN_SPEECH_RATIO = 0.15
MIN_WORDS = 40
DUP_TOLERANCE = 1.0
ERROR_MAX = 200


def _eligible():
    """Downloaded, not yet picked, attempts left, and transcribed (or transcription gave up)."""
    return and_(
        Source.status == SourceStatus.downloaded, Source.moments_at.is_(None),
        or_(Source.moments_attempts.is_(None), Source.moments_attempts < MAX_ATTEMPTS),
        Source.local_path.is_not(None),
        or_(Source.transcript_path.is_not(None), Source.transcribe_attempts >= 2),
    )


def _claim(source_id: int, engine: Optional[Engine]) -> bool:
    with get_session(engine) as s:
        joined = select(Campaign.id).where(Campaign.id == Source.campaign_id,
                                           Campaign.status == CampaignStatus.joined).exists()
        res = s.execute(update(Source).where(Source.id == source_id, _eligible(), joined)
                        .values(status=SourceStatus.processing, updated_at=datetime.now(timezone.utc)))
        return res.rowcount == 1


def _save(source_id: int, moments: list[Moment], method: str, engine: Optional[Engine]) -> int:
    with get_session(engine) as s:
        src = s.get(Source, source_id)
        existing = [(c.start_sec, c.end_sec) for c in s.scalars(select(Clip).where(Clip.source_id == source_id))]
        created = 0
        for m in moments:
            if any(abs(a - m.start) <= DUP_TOLERANCE and abs(b - m.end) <= DUP_TOLERANCE for a, b in existing):
                continue
            s.add(Clip(campaign_id=src.campaign_id, source_id=src.id, start_sec=m.start, end_sec=m.end,
                       hook_title=m.hook or None, moment_score=m.score, hashtags=[], status=ClipStatus.candidate))
            existing.append((m.start, m.end))
            created += 1
        src.moments_at, src.moments_method = datetime.now(timezone.utc), method
        src.status, src.error = SourceStatus.downloaded, None
    return created


def pick_moments(source_id: int, engine: Optional[Engine] = None, generate: Optional[Callable] = None,
                 heuristic: Optional[Callable] = None) -> int:
    """Create candidate clips for one source; returns how many were created. Never raises."""
    generate = generate or (lambda *a, **k: generate_json(*a, engine=engine, **k))
    heuristic = heuristic or pick_heuristic
    try:
        if not _claim(source_id, engine):
            return 0
    except Exception as exc:  # noqa: BLE001
        log.error("source %d: moments claim failed: %s", source_id, exc)
        return 0
    try:
        with get_session(engine) as s:
            src = s.get(Source, source_id)
            campaign = s.get(Campaign, src.campaign_id)
            rules = get_rules(campaign)
            s.expunge_all()
        if not Path(src.local_path).is_file():
            raise FileNotFoundError("source file missing")
        transcript = load_transcript(src) if src.transcript_path else None
        duration = src.duration_sec or (transcript or {}).get("duration") or 0
        speechy = bool(transcript) and (src.speech_ratio or 0) >= MIN_SPEECH_RATIO and (src.word_count or 0) >= MIN_WORDS
        moments: list[Moment] = []
        method = "heuristic"
        if speechy and settings.GEMINI_API_KEY:
            if budget_ok(engine):
                moments = refine(pick_ai(src, campaign, rules, transcript, generate), transcript, rules, duration)
                method = "ai" if moments else "heuristic"
                if not moments:
                    log.warning("source %d: AI gave no usable moments; using heuristic", source_id)
            else:
                warn_budget_once(engine)
                log.info("source %d: AI budget reached; using heuristic", source_id)
        if not moments:
            moments = heuristic(src, rules, duration)
        created = _save(source_id, moments, method, engine)
        log.info("source %d: %d candidate moments (%s)", source_id, created, method)
        return created
    except Exception as exc:  # noqa: BLE001
        msg = f"moments: {type(exc).__name__}: {exc}".replace("\n", " ")
        msg = msg if len(msg) <= ERROR_MAX else msg[: ERROR_MAX - 1] + "…"
        try:
            with get_session(engine) as s:
                src = s.get(Source, source_id)
                src.moments_attempts = (src.moments_attempts or 0) + 1
                src.status, src.error = SourceStatus.downloaded, msg
        except Exception as db_exc:  # noqa: BLE001
            log.error("source %d: could not record moments failure: %s", source_id, db_exc)
        log.warning("source %d %s", source_id, msg)
        return 0


def pick_pending(limit: int = 3, engine: Optional[Engine] = None, generate: Optional[Callable] = None,
                 heuristic: Optional[Callable] = None) -> dict:
    """Pick moments for the oldest eligible sources of joined campaigns."""
    with get_session(engine) as s:
        ids = list(s.scalars(select(Source.id).join(Campaign, Source.campaign_id == Campaign.id)
                             .where(_eligible(), Campaign.status == CampaignStatus.joined)
                             .order_by(Source.transcribed_at, Source.downloaded_at, Source.id).limit(limit)))
    summary = {"sources": 0, "candidates": 0}
    for sid in ids:
        summary["sources"] += 1
        summary["candidates"] += pick_moments(sid, engine, generate, heuristic)
    return summary
