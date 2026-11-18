"""Pre-posting compliance: hard errors block posting, warnings are shown to the user. Never raises."""
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, select

from app.clipper.transcribe_service import load_transcript, words_in_range
from app.config import platform_max_sec, settings
from app.db import get_session
from app.db.enums import ClipStatus
from app.db.models import Campaign, Clip, Source
from app.downloader.media import probe
from app.logger import get_logger
from app.rules import Rules, get_rules
from app.rules.caption import check_text, render_caption, render_for_platform
from app.rules.model import KNOWN_PLATFORMS

log = get_logger("clipflow.compliance")

DURATION_SLACK = 0.5
ASPECT = 9 / 16
ASPECT_TOLERANCE = 0.01
BLACK_SHARE_WARN = 0.30
SILENCE_SHARE_WARN = 0.50
HOOK_MAX = 60
DUP_OVERLAP = 0.40
DUP_STATUSES = (ClipStatus.approved, ClipStatus.scheduled, ClipStatus.posted)
RECHECK_STATUSES = (ClipStatus.rendered, ClipStatus.approved)
TEXT_FIELDS = ("title", "description", "caption", "text")


def analyze_av(path: str, duration: float) -> dict:
    """Seconds of black video and of silence, from one ffmpeg pass (blackdetect + silencedetect)."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
                              "-vf", "blackdetect=d=0.5:pix_th=0.10", "-af", "silencedetect=noise=-35dB:d=1",
                              "-f", "null", "-"], capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"black": 0.0, "silence": 0.0}
    log_text = out.stderr or ""
    black = sum(float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", log_text))
    silence = sum(float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", log_text))
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", log_text)]
    ends = re.findall(r"silence_end:", log_text)
    if len(starts) > len(ends):  # silence running to the end of the file has no silence_end line
        silence += max(0.0, duration - starts[-1])
    return {"black": black, "silence": silence}


def _texts(captions: dict) -> list[tuple[str, str, str]]:
    """[(platform, field, text)] for every text field of every platform caption."""
    return [(p, f, out[f]) for p, out in (captions or {}).items() for f in TEXT_FIELDS if out.get(f)]


def _body(out: dict) -> str:
    return out.get("description") or out.get("caption") or out.get("text") or ""


def _evaluate(clip: Clip, campaign: Campaign, source: Optional[Source], rules: Rules,
              others: list[Clip]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    hook = clip.hook_title or ""
    banned_only = Rules(banned_words=list(rules.banned_words), caption_template="{hook}")

    if not rules.post_platforms:
        errors.append("no platforms to post to (rules: post_platforms empty)")
    if not rules.confirmed:
        warnings.append("campaign rules not confirmed (rules ?)")

    info = None
    path = clip.file_path
    if not path or not Path(path).is_file():
        errors.append("clip file missing")
    else:
        try:
            info = probe(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"probe failed: {str(exc)[:120]}")
    duration = 0.0
    if info:
        if not info["has_video"]:
            errors.append("no video stream")
        if not info["has_audio"]:
            errors.append("no audio stream")
        w, h = info["width"] or 0, info["height"] or 0
        if not h or abs(w / h - ASPECT) / ASPECT > ASPECT_TOLERANCE:
            errors.append(f"not 9:16 ({w}x{h})")
        elif (w, h) != (settings.RENDER_WIDTH, settings.RENDER_HEIGHT):
            errors.append(f"resolution {w}x{h}, expected {settings.RENDER_WIDTH}x{settings.RENDER_HEIGHT}")
        duration = info["duration_sec"] or 0.0
        lo, hi = rules.clip_min_sec - DURATION_SLACK, rules.clip_max_sec + DURATION_SLACK
        if not lo <= duration <= hi:
            errors.append(f"duration {duration:.1f}s outside {rules.clip_min_sec}-{rules.clip_max_sec}s")
        limits = platform_max_sec()
        for p in rules.post_platforms:
            if p in limits and duration > limits[p]:
                errors.append(f"too long for {p} ({duration:.0f}s > {limits[p]:g}s)")
        size_mb = Path(path).stat().st_size / 1024 ** 2
        if size_mb > settings.MAX_CLIP_MB:
            errors.append(f"file {size_mb:.0f} MB > {settings.MAX_CLIP_MB:g} MB")
        if info["has_video"] and info["has_audio"] and duration:
            av = analyze_av(path, duration)
            if av["black"] > BLACK_SHARE_WARN * duration:
                warnings.append(f"black frames {av['black'] / duration:.0%} of the clip")
            if av["silence"] > SILENCE_SHARE_WARN * duration:
                warnings.append(f"silence {min(1.0, av['silence'] / duration):.0%} of the clip")

    words: list = []
    if source is not None and source.transcript_path and Path(source.transcript_path).is_file():
        words = words_in_range(load_transcript(source), clip.start_sec, clip.end_sec)
    if not words:
        warnings.append("no speech captions (no transcript words in range)")

    if banned_only.banned_words:
        if check_text(banned_only, hook):
            errors.append("banned word in hook: " + ", ".join(v.split(": ", 1)[1] for v in check_text(banned_only, hook)))
        spoken = " ".join(w["w"] for w in words)
        if spoken and check_text(banned_only, spoken):
            errors.append("banned word spoken in clip: "
                          + ", ".join(v.split(": ", 1)[1] for v in check_text(banned_only, spoken)))
        for p, field, text in _texts(clip.platform_captions):
            found = check_text(banned_only, text)
            if found:
                errors.append(f"banned word in {p} {field}: " + ", ".join(v.split(": ", 1)[1] for v in found))
    for p, out in (clip.platform_captions or {}).items():
        missing = [v for v in check_text(rules, _body(out)) if v.startswith("missing")]
        errors += [f"{p}: {m}" for m in missing]

    if not hook.strip():
        warnings.append("hook title empty")
    elif len(hook) > HOOK_MAX:
        warnings.append(f"hook title {len(hook)} chars (> {HOOK_MAX})")
    for other in others:
        overlap = max(0.0, min(clip.end_sec, other.end_sec) - max(clip.start_sec, other.start_sec))
        shorter = min(clip.end_sec - clip.start_sec, other.end_sec - other.start_sec) or 1
        if overlap > DUP_OVERLAP * shorter:
            warnings.append(f"near-duplicate of #{other.id} ({other.status.value})")
    return errors, warnings


def check_clip(clip_id: int, autofix: bool = True, engine: Optional[Engine] = None) -> dict:
    """Run all checks, save compliance (+ autofixed captions), return {"ok","errors","warnings"}."""
    result: dict = {"ok": False, "errors": [], "warnings": []}
    try:
        with get_session(engine) as s:
            clip = s.get(Clip, clip_id)
            if clip is None:
                return {"ok": False, "errors": ["clip not found"], "warnings": []}
            campaign = s.get(Campaign, clip.campaign_id)
            source = s.get(Source, clip.source_id)
            rules = get_rules(campaign)
            if autofix:
                hook = clip.hook_title or ""
                clip.platform_captions = {p: render_for_platform(rules, hook, p)
                                          for p in rules.post_platforms if p in KNOWN_PLATFORMS}
                clip.caption = render_caption(rules, hook)
                clip.hashtags = list(rules.hashtags)
            others = s.scalars(select(Clip).where(Clip.source_id == clip.source_id, Clip.id != clip.id,
                                                  Clip.status.in_(DUP_STATUSES))).all()
            errors, warnings = _evaluate(clip, campaign, source, rules, list(others))
            result = {"ok": not errors, "errors": errors, "warnings": warnings}
            clip.compliance, clip.compliance_at = result, datetime.now(timezone.utc)
        return result
    except Exception as exc:  # noqa: BLE001
        msg = f"compliance: {type(exc).__name__}: {exc}".replace("\n", " ")[:200]
        log.warning("clip %d %s", clip_id, msg)
        result = {"ok": False, "errors": [msg], "warnings": []}
        try:
            with get_session(engine) as s:
                clip = s.get(Clip, clip_id)
                if clip is not None:
                    clip.compliance, clip.compliance_at = result, datetime.now(timezone.utc)
        except Exception as db_exc:  # noqa: BLE001
            log.error("clip %d: could not save compliance: %s", clip_id, db_exc)
        return result


def recheck_rules_changed(campaign_id: int, engine: Optional[Engine] = None) -> int:
    """Re-run compliance (with autofix) for a campaign's rendered/approved clips; returns how many."""
    with get_session(engine) as s:
        ids = list(s.scalars(select(Clip.id).where(Clip.campaign_id == campaign_id,
                                                   Clip.status.in_(RECHECK_STATUSES))))
    for cid in ids:
        check_clip(cid, autofix=True, engine=engine)
    if ids:
        log.info("campaign %d: rechecked %d clips after a rules change", campaign_id, len(ids))
    return len(ids)


def compliance_state(compliance: Optional[dict]) -> str:
    """'ok' | 'warn' | 'error' | 'none' for badges and filters."""
    if not compliance:
        return "none"
    if compliance.get("errors"):
        return "error"
    return "warn" if compliance.get("warnings") else "ok"
