"""Regex-only rule suggestions from campaign text (no AI, no network)."""
import re

from app.rules.model import normalize_hashtags, normalize_mentions

_HASHTAG = re.compile(r"(?<![\w&#/])#([A-Za-z0-9_]+)")
_MENTION = re.compile(r"(?<![\w.@/])@([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,29}[A-Za-z0-9_])?)")
_UNIT = r"(s|secs?|seconds?|m|mins?|minutes?)\b"
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT_OPT = rf"(?:{_UNIT})?"
_RANGE = [
    re.compile(rf"\bbetween\s+{_NUM}\s*{_UNIT_OPT}\s*and\s+{_NUM}\s*{_UNIT}", re.I),
    re.compile(rf"\b{_NUM}\s*{_UNIT_OPT}\s*(?:-|–|—|to)\s*{_NUM}\s*{_UNIT}", re.I),
]
_MIN = re.compile(rf"\b(?:at\s+least|min(?:imum)?\.?|no\s+shorter\s+than|longer\s+than)\s*:?\s*{_NUM}\s*{_UNIT}",
                  re.I)
_MAX = re.compile(rf"\b(?:under|max(?:imum)?\.?|up\s+to|no\s+longer\s+than|less\s+than|at\s+most|shorter\s+than)"
                  rf"\s*:?\s*{_NUM}\s*{_UNIT}", re.I)
_DISCLOSURE = re.compile(r"#ad\b|#sponsored\b|paid\s+partnership|disclos", re.I)
_BANNED = re.compile(r"\bdo\s+not\b|\bdon[’']t\b|\bno\s|\bnot\s+allowed\b|\bprohibited\b", re.I)


def _seconds(num: str, unit: str | None) -> int:
    value = float(num)
    if unit and unit.lower().startswith("m"):
        value *= 60
    return int(round(value))


def _length(text: str) -> dict:
    for pat in _RANGE:
        if m := pat.search(text):
            a, ua, b, ub = m.groups()
            return {"clip_min_sec": _seconds(a, ua or ub), "clip_max_sec": _seconds(b, ub)}
    out = {}
    if m := _MIN.search(text):
        out["clip_min_sec"] = _seconds(*m.groups())
    if m := _MAX.search(text):
        out["clip_max_sec"] = _seconds(*m.groups())
    return out


def suggest_from_text(text: str) -> dict:
    """Suggest rules found in text; returns only keys that were found."""
    text = text or ""
    out: dict = {}
    if _DISCLOSURE.search(text):
        lowered = text.lower()
        out["disclosure_tag"] = "#sponsored" if "#sponsored" in lowered and "#ad" not in lowered else "#ad"
    tags = [t for t in normalize_hashtags(_HASHTAG.findall(text)) if t != out.get("disclosure_tag")]
    if tags:
        out["hashtags"] = tags
    mentions = normalize_mentions(_MENTION.findall(text))
    if mentions:
        out["mentions"] = mentions
    out.update(_length(text))
    hints = []
    for line in re.split(r"[\n\r]+|(?<=[.!?])\s+", text):
        line = line.strip(" -•*\t")
        if line and _BANNED.search(line):
            short = line if len(line) <= 100 else line[:99] + "…"
            if short not in hints:
                hints.append(short)
        if len(hints) >= 10:
            break
    if hints:
        out["banned_hints"] = hints
    return out
