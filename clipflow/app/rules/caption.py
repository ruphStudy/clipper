"""Caption rendering and checking against campaign rules."""
import re
from typing import Optional

from app.rules.model import Rules, normalize_hashtags

LIMITS = {"youtube": 5000, "instagram": 2200, "tiktok": 2200, "x": 280}
YT_TITLE_MAX = 100
YT_HOOK_MAX = 95
SHORTS_TAG = " #shorts"


class _Blank(dict):
    def __missing__(self, key):
        return ""


def _collapse(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def render_caption(rules: Rules, hook: str, extra_hashtags: Optional[list[str]] = None) -> str:
    """Fill the caption template and tidy whitespace."""
    tags = normalize_hashtags(list(rules.hashtags) + list(extra_hashtags or []))
    values = _Blank(hook=(hook or "").strip(), credit=rules.credit_text or "",
                    mentions=" ".join(rules.mentions), hashtags=" ".join(tags),
                    disclosure=rules.disclosure_tag or "")
    try:
        filled = rules.caption_template.format_map(values)
    except (ValueError, IndexError):
        filled = rules.caption_template.replace("{hook}", values["hook"])
    return _collapse(filled)


def _trim(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def _fit(rules: Rules, hook: str, limit: int) -> str:
    """Render within limit by shortening the hook only (required tags are never dropped)."""
    full = render_caption(rules, hook)
    if len(full) <= limit:
        return full
    base = len(render_caption(rules, ""))
    room = limit - base - 1  # newline/space joining the hook
    if room <= 1:
        return render_caption(rules, "")
    lo, hi, best = 1, room, render_caption(rules, "")
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = render_caption(rules, _trim(hook, mid))
        if len(cand) <= limit:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best


def render_for_platform(rules: Rules, hook: str, platform: str) -> dict:
    """Platform-ready text fields."""
    platform = platform.lower()
    if platform == "youtube":
        title = _trim(hook or "", YT_HOOK_MAX)
        if len(title) + len(SHORTS_TAG) <= YT_TITLE_MAX:
            title += SHORTS_TAG
        return {"platform": platform, "title": title, "description": _fit(rules, hook, LIMITS["youtube"])}
    if platform in ("instagram", "tiktok"):
        return {"platform": platform, "caption": _fit(rules, hook, LIMITS[platform])}
    if platform == "x":
        return {"platform": platform, "text": _fit(rules, hook, LIMITS["x"])}
    raise ValueError(f"unknown platform: {platform}")


def _has_token(text: str, token: str) -> bool:
    return re.search(r"(?<![\w#@])" + re.escape(token) + r"(?!\w)", text, re.I) is not None


def check_text(rules: Rules, text: str) -> list[str]:
    """Violations: missing required hashtags/mentions/disclosure, banned words present."""
    text = text or ""
    problems = [f"missing hashtag {t}" for t in rules.hashtags if not _has_token(text, t)]
    problems += [f"missing mention {m}" for m in rules.mentions if not _has_token(text, m)]
    if rules.disclosure_tag and not _has_token(text, rules.disclosure_tag):
        problems.append(f"missing disclosure {rules.disclosure_tag}")
    for word in rules.banned_words:
        if re.search(r"\b" + re.escape(word) + r"\b", text, re.I):
            problems.append(f"banned word: {word}")
    return problems
