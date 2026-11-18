"""Moment picking: compact transcript → Gemini (cheap) → word-level refinement; ffmpeg heuristic fallback."""
import math
import re
import subprocess
from dataclasses import dataclass, replace
from typing import Callable, Optional

from app.config import settings
from app.logger import get_logger
from app.rules import Rules
from app.rules.caption import check_text

log = get_logger("clipflow.moments")

WINDOW_OVERLAP_SEC = 30.0
TINY_SEGMENT_SEC = 2.0
SNAP_SEC = 0.3
SENTENCE_EXTEND_SEC = 3.0
DEDUPE_OVERLAP = 0.4
FILLERS = {"um", "uh", "erm", "uhm", "hmm", "mm", "ah"}
SCHEMA = {
    "type": "OBJECT",
    "properties": {"moments": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "hook": {"type": "STRING"},
                       "score": {"type": "INTEGER"}},
        "required": ["start", "end", "hook", "score"]}}},
    "required": ["moments"],
}


@dataclass
class Moment:
    start: float
    end: float
    hook: str
    score: float


def fmt_ts(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clean(text: str) -> str:
    out: list[str] = []
    for tok in text.split():
        bare = re.sub(r"[^\w']", "", tok).lower()
        if bare in FILLERS:
            continue
        if out and bare and re.sub(r"[^\w']", "", out[-1]).lower() == bare:
            continue  # "the the the" → "the"
        out.append(tok)
    return " ".join(out)


def compact_transcript(segments: list[dict], start: float, end: float) -> str:
    """'[m:ss] text' per segment inside [start, end); tiny (<2s) segments merged into the previous one."""
    lines: list[list] = []  # [start, end, text]
    for seg in segments:
        if seg["end"] <= start or seg["start"] >= end:
            continue
        text = _clean(seg.get("text") or "")
        if not text:
            continue
        if lines and (seg["end"] - seg["start"] < TINY_SEGMENT_SEC or lines[-1][1] - lines[-1][0] < TINY_SEGMENT_SEC):
            lines[-1][1] = seg["end"]
            lines[-1][2] += " " + text
        else:
            lines.append([seg["start"], seg["end"], text])
    return "\n".join(f"[{fmt_ts(s)}] {t}" for s, _, t in lines)


def build_prompt(campaign, rules: Rules, window_text: str, n: int) -> str:
    desc = re.sub(r"\s+", " ", (getattr(campaign, "description", None) or "")).strip()[:300]
    brand = getattr(campaign, "brand", None) or ""
    banned = ", ".join(rules.banned_words[:20]) or "none"
    return (
        f"Campaign: {campaign.name}{' by ' + brand if brand else ''}. {desc}\n"
        f"Pick the {n} best self-contained moments from this transcript for vertical short-form video. "
        f"Each needs a strong hook in the first 3 seconds, a clear payoff, and emotion, humor, surprise or insight, "
        f"and must make sense without context. Length {rules.clip_min_sec}-{rules.clip_max_sec} seconds. "
        f"Use start/end in seconds from the [m:ss] timestamps. Avoid moments containing: {banned}. "
        f"hook: a curiosity-style title, max 60 characters, in the transcript's language, no false claims, "
        f"at most one emoji. score: 1-10. Output only the JSON.\n\nTranscript:\n{window_text}"
    )


def windows(total: float, window_min: float) -> list[tuple[float, float]]:
    """[(start, end)] windows of window_min minutes overlapping by 30 s."""
    size = window_min * 60
    if total <= size:
        return [(0.0, float(total))]
    out, start = [], 0.0
    while True:
        end = min(start + size, total)
        out.append((start, end))
        if end >= total:
            return out
        start = end - WINDOW_OVERLAP_SEC


def per_window_n(window_len: float, total: float) -> int:
    return max(2, math.ceil(settings.MOMENTS_PER_SOURCE * window_len / total)) if total else 2


def pick_ai(source, campaign, rules: Rules, transcript: dict, generate: Callable) -> list[Moment]:
    """Ask the LLM per window; returns all valid moments (score desc). Empty if every call failed."""
    total = transcript.get("duration") or source.duration_sec or (transcript["segments"][-1]["end"]
                                                                  if transcript.get("segments") else 0)
    found: list[Moment] = []
    for w_start, w_end in windows(total, settings.MOMENTS_WINDOW_MIN):
        text = compact_transcript(transcript.get("segments") or [], w_start, w_end)
        if not text:
            continue
        n = per_window_n(w_end - w_start, total)
        result = generate(build_prompt(campaign, rules, text, n), SCHEMA, "moments",
                          max_output_tokens=60 * n + 200)
        for m in (result or {}).get("moments") or []:
            try:
                start, end = float(m["start"]), float(m["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                continue
            hook = re.sub(r"\s+", " ", str(m.get("hook") or "")).strip()[:60]
            score = min(10.0, max(1.0, float(m.get("score") or 1)))
            found.append(Moment(start, end, hook, score))
    return sorted(found, key=lambda m: m.score, reverse=True)


def _flat_words(transcript: dict) -> list[dict]:
    return sorted((w for seg in transcript.get("segments") or [] for w in seg.get("words") or []
                   if w.get("s") is not None and w.get("e") is not None), key=lambda w: w["s"])


def _overlap(a: Moment, b: Moment) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def _snap(m: Moment, words: list[dict], rules: Rules, duration: float) -> Optional[Moment]:
    starts = [i for i, w in enumerate(words) if w["s"] >= m.start - SNAP_SEC]
    if not starts:
        return None
    i = starts[0]
    ends = [j for j, w in enumerate(words) if w["e"] <= m.end + SNAP_SEC and j >= i]
    if not ends:
        return None
    j = ends[-1]
    if not re.search(r"[.!?][\"')\]]*$", words[j]["w"]):  # extend to a sentence end within +3 s
        for k in range(j + 1, len(words)):
            if words[k]["e"] > words[j]["e"] + SENTENCE_EXTEND_SEC:
                break
            if re.search(r"[.!?][\"')\]]*$", words[k]["w"]):
                j = k
                break
    lo, hi = rules.clip_min_sec, rules.clip_max_sec
    while words[j]["e"] - words[i]["s"] < lo and j + 1 < len(words) and words[j + 1]["e"] - words[i]["s"] <= hi:
        j += 1
    while words[j]["e"] - words[i]["s"] < lo and i > 0 and words[j]["e"] - words[i - 1]["s"] <= hi:
        i -= 1
    while words[j]["e"] - words[i]["s"] > hi and j > i:
        j -= 1
    start, end = max(0.0, words[i]["s"]), min(duration, words[j]["e"])
    if not (lo <= end - start <= hi):
        return None
    return replace(m, start=round(start, 2), end=round(end, 2))


def refine(moments: list[Moment], transcript: dict, rules: Rules, duration: float) -> list[Moment]:
    """Snap to words/sentences, enforce length, drop banned, dedupe overlaps, top N by score."""
    words = _flat_words(transcript)
    banned_only = Rules(banned_words=list(rules.banned_words), caption_template="{hook}")
    kept: list[Moment] = []
    for m in sorted(moments, key=lambda m: m.score, reverse=True):
        snapped = _snap(m, words, rules, duration) if words else None
        if snapped is None:
            continue
        text = " ".join(w["w"] for w in words if w["s"] >= snapped.start and w["e"] <= snapped.end)
        if banned_only.banned_words and check_text(banned_only, text):
            continue
        if any(_overlap(snapped, k) > DEDUPE_OVERLAP * min(snapped.end - snapped.start, k.end - k.start)
               for k in kept):
            continue
        kept.append(snapped)
        if len(kept) >= settings.MOMENTS_PER_SOURCE:
            break
    return kept


# --- heuristic (no AI) ------------------------------------------------------------------------

def _ffmpeg_lines(args: list[str], timeout: int = 1800) -> list[str]:
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-nostats", *args], capture_output=True, text=True,
                             timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ffmpeg analysis failed: {type(exc).__name__}") from exc
    return out.stdout.splitlines()


def loudness_per_second(path: str) -> list[float]:
    """RMS level (dB) per 1 s of audio, streamed through ffmpeg (no temp files)."""
    lines = _ffmpeg_lines(["-i", str(path), "-vn", "-af",
                           "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,"
                           "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-",
                           "-f", "null", "-"])
    levels = []
    for line in lines:
        if "RMS_level=" in line:
            val = line.split("=", 1)[1].strip()
            try:
                levels.append(max(-120.0, float(val)))
            except ValueError:
                levels.append(-120.0)
    return levels


def scene_changes(path: str) -> list[float]:
    """Timestamps (s) of scene cuts (score > 0.35) on a downscaled, low-fps stream."""
    lines = _ffmpeg_lines(["-i", str(path), "-an", "-vf",
                           "fps=5,scale=160:-2,select='gt(scene,0.35)',metadata=mode=print:file=-",
                           "-f", "null", "-"])
    out = []
    for line in lines:
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            out.append(float(m.group(1)))
    return out


def pick_heuristic(source, rules: Rules, duration: Optional[float] = None) -> list[Moment]:
    """Loud + visually busy windows, no AI. Hooks left empty."""
    duration = duration or source.duration_sec or 0
    lo, hi = rules.clip_min_sec, rules.clip_max_sec
    length = min((lo + hi) / 2, duration)
    if length < lo or duration <= 0:
        return []
    levels = loudness_per_second(source.local_path)
    scenes = scene_changes(source.local_path)
    if not levels:
        return []
    mean = sum(levels) / len(levels)
    std = math.sqrt(sum((x - mean) ** 2 for x in levels) / len(levels)) or 1.0
    z = [(x - mean) / std for x in levels]
    win = max(1, int(round(length)))
    candidates = []
    for t in range(0, max(1, int(duration - length) + 1)):
        seg = z[t:t + win]
        if not seg:
            break
        loud = sum(seg) / len(seg)
        density = sum(1 for s in scenes if t <= s < t + length) / (length / 10)
        candidates.append((loud + 0.5 * density, float(t)))
    candidates.sort(reverse=True)
    picked: list[Moment] = []
    for score, start in candidates:
        m = Moment(start, round(min(duration, start + length), 2), "", score)
        if any(_overlap(m, k) > 0 for k in picked):
            continue
        picked.append(m)
        if len(picked) >= settings.MOMENTS_PER_SOURCE:
            break
    if picked:
        top, bottom = picked[0].score, picked[-1].score
        span = (top - bottom) or 1.0
        picked = [replace(m, score=round(1 + 9 * (m.score - bottom) / span, 1) if top != bottom else 5.0)
                  for m in picked]
    return picked
