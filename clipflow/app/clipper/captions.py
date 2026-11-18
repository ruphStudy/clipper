"""Word-highlight captions + hook title: ASS output (for ffmpeg+libass) and a Pillow PNG fallback.

Both renderers share the same timed events, so the look and timing match whichever path ffmpeg supports.
"""
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.config import settings

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
SYSTEM_FONTS = ("/System/Library/Fonts/Supplemental/Impact.ttf", "/System/Library/Fonts/Supplemental/Arial Black.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
PUNCT_END = re.compile(r"[.,!?;:…]$")
MAX_LINE_GAP = 1.2
HOOK_SECONDS = 3.0
HOOK_FADE = 0.2
HOOK_WRAP = 22
YELLOW_ASS = "&H00FFFF&"
WHITE_ASS = "&HFFFFFF&"


@dataclass
class CaptionEvent:
    start: float
    end: float
    words: list[str] = field(default_factory=list)  # whole line, uppercase
    current: int = -1  # index of the highlighted word


@dataclass
class Layout:
    width: int
    height: int
    cap_size: int
    cap_margin_v: int
    hook_size: int
    hook_margin_v: int
    side_margin: int


def layout_for(w: int, h: int) -> Layout:
    return Layout(w, h, cap_size=round(w * 0.075), cap_margin_v=round(h * 0.28), hook_size=round(w * 0.06),
                  hook_margin_v=round(h * 0.12), side_margin=round(w * 0.055))


def font_path(name: Optional[str] = None) -> Path:
    """Bundled font file (e.g. Anton-Regular.ttf), else a bold system font."""
    name = name or settings.CAPTION_FONT
    for candidate in sorted(FONTS_DIR.glob(f"{name}*.ttf")) + sorted(FONTS_DIR.glob(f"{name}*.otf")):
        return candidate
    for sys_font in SYSTEM_FONTS:
        if Path(sys_font).exists():
            return Path(sys_font)
    raise FileNotFoundError(f"no caption font found for {name}")


def _clean_word(w: str) -> str:
    # Braces start ASS override blocks and backslashes start tags: swap them for safe look-alikes.
    return w.strip().upper().replace("\\", "∖").replace("{", "(").replace("}", ")")


def caption_events(words: list[dict], clip_start: float, per_line: Optional[int] = None) -> list[CaptionEvent]:
    """One event per word: the whole line, with that word highlighted until the next word starts."""
    per_line = per_line or settings.CAPTION_WORDS_PER_LINE
    lines: list[list[dict]] = []
    for w in words:
        text = _clean_word(w.get("w", ""))
        if not text:
            continue
        item = {"t": text, "s": max(0.0, w["s"] - clip_start), "e": max(0.0, w["e"] - clip_start)}
        if lines:
            cur = lines[-1]
            prev = cur[-1]
            if len(cur) < per_line and not PUNCT_END.search(prev["t"]) and item["s"] - prev["e"] <= MAX_LINE_GAP:
                cur.append(item)
                continue
        lines.append([item])
    events = []
    for line in lines:
        texts = [it["t"] for it in line]
        for i, it in enumerate(line):
            end = line[i + 1]["s"] if i + 1 < len(line) else line[-1]["e"]
            end = max(end, it["s"] + 0.05)
            events.append(CaptionEvent(round(it["s"], 2), round(end, 2), texts, i))
    return events


def hook_lines(hook: str) -> list[str]:
    hook = re.sub(r"\s+", " ", hook or "").strip()
    if not hook:
        return []
    lines = textwrap.wrap(hook, HOOK_WRAP) or [hook]
    if len(lines) > 2:
        lines = [lines[0], textwrap.shorten(" ".join(lines[1:]), HOOK_WRAP, placeholder="…")]
    return lines


def _ts(sec: float) -> str:
    cs = int(round(max(0.0, sec) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "∖").replace("{", "(").replace("}", ")")


def build_ass(words: list[dict], clip_start: float, hook: str, rules, font: Optional[str], w: int, h: int) -> str:
    """ASS subtitle document: hook at the top for 3 s, word-by-word highlighted captions lower-middle."""
    lay = layout_for(w, h)
    family = font or settings.CAPTION_FONT
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cap,{family},{lay.cap_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,6,0,2,"
        f"{lay.side_margin},{lay.side_margin},{lay.cap_margin_v},1",
        f"Style: Hook,{family},{lay.hook_size},&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,-1,0,0,0,100,100,0,0,3,14,0,8,"
        f"{lay.side_margin},{lay.side_margin},{lay.hook_margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    lines = hook_lines(hook)
    if lines:
        text = "\\N".join(_ass_escape(x) for x in lines)
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(HOOK_SECONDS)},Hook,,0,0,0,,{{\\fad({int(HOOK_FADE * 1000)},0)}}{text}")
    if getattr(rules, "burn_captions", True):
        for ev in caption_events(words, clip_start):
            parts = [f"{{\\c{YELLOW_ASS}}}{t}{{\\c{WHITE_ASS}}}" if i == ev.current else t
                     for i, t in enumerate(ev.words)]
            events.append(f"Dialogue: 0,{_ts(ev.start)},{_ts(ev.end)},Cap,,0,0,0,,{' '.join(parts)}")
    return "\n".join(head + events) + "\n"


@lru_cache(maxsize=1)
def ffmpeg_has_ass() -> bool:
    """True if this ffmpeg build has libass (the 'ass' filter)."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[1:2] == ["ass"] for line in out.stdout.splitlines() if line.strip())


# --- Pillow fallback: PNG overlay sequence -----------------------------------------------------------------

def _font(path: Path, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(path), size)


def _draw_caption(img, ev: CaptionEvent, lay: Layout, fpath: Path) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)
    size = lay.cap_size
    while True:
        font = _font(fpath, size)
        space = draw.textlength(" ", font=font)
        widths = [draw.textlength(t, font=font) for t in ev.words]
        total = sum(widths) + space * (len(widths) - 1)
        if total <= lay.width - 2 * lay.side_margin or size <= 24:
            break
        size = int(size * 0.9)
    ascent, descent = font.getmetrics()
    y = lay.height - lay.cap_margin_v - (ascent + descent)
    x = (lay.width - total) / 2
    for i, (t, wdt) in enumerate(zip(ev.words, widths)):
        fill = (255, 255, 0, 255) if i == ev.current else (255, 255, 255, 255)
        draw.text((x, y), t, font=font, fill=fill, stroke_width=6, stroke_fill=(0, 0, 0, 255))
        x += wdt + space


def _draw_hook(img, lines: list[str], lay: Layout, fpath: Path, alpha: float) -> None:
    from PIL import Image, ImageDraw

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = _font(fpath, lay.hook_size)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    pad = round(lay.hook_size * 0.3)
    y = lay.hook_margin_v
    for line in lines:
        wdt = draw.textlength(line, font=font)
        x = (lay.width - wdt) / 2
        draw.rectangle([x - pad, y - pad // 2, x + wdt + pad, y + line_h + pad // 2], fill=(0, 0, 0, 128))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h + pad
    if alpha < 1:
        a = layer.getchannel("A").point(lambda v: int(v * alpha))
        layer.putalpha(a)
    img.alpha_composite(layer)


def render_overlay_sequence(words: list[dict], clip_start: float, hook: str, rules, clip_len: float,
                            w: int, h: int, out_dir: Path, font: Optional[str] = None) -> Path:
    """Write transparent PNG frames + an ffconcat list timing them; returns the list path."""
    from PIL import Image

    lay = layout_for(w, h)
    fpath = font_path(font)
    hook_l = hook_lines(hook)
    cap_events = caption_events(words, clip_start) if getattr(rules, "burn_captions", True) else []
    fades = [round(HOOK_FADE * i / 4, 3) for i in range(4)] if hook_l else []
    cuts = {0.0, round(clip_len, 3)}
    if hook_l:
        cuts.update(fades + [HOOK_SECONDS])
    for ev in cap_events:
        cuts.update((ev.start, ev.end))
    times = sorted(t for t in cuts if 0 <= t <= clip_len)
    cache: dict = {}
    entries: list[tuple[str, float]] = []
    for a, b in zip(times, times[1:]):
        if b - a < 0.005:
            continue
        mid = (a + b) / 2
        active = [ev for ev in cap_events if ev.start <= mid < ev.end]
        hook_alpha = 0.0
        if hook_l and mid < HOOK_SECONDS:
            hook_alpha = min(1.0, (sum(1 for f in fades if f <= mid)) / 4) if mid < HOOK_FADE else 1.0
        key = (tuple((tuple(ev.words), ev.current) for ev in active), round(hook_alpha, 2))
        if key not in cache:
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            if hook_alpha:
                _draw_hook(img, hook_l, lay, fpath, hook_alpha)
            for ev in active:
                _draw_caption(img, ev, lay, fpath)
            name = f"o{len(cache):04d}.png"
            img.save(out_dir / name, optimize=False, compress_level=3)
            cache[key] = name
        entries.append((cache[key], round(b - a, 3)))
    if not entries:
        Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(out_dir / "o0000.png")
        entries = [("o0000.png", round(clip_len, 3))]
    lines = ["ffconcat version 1.0"]
    for name, dur in entries:
        lines += [f"file '{name}'", f"duration {dur}"]
    lines.append(f"file '{entries[-1][0]}'")  # the concat demuxer needs the last file repeated
    out = out_dir / "overlay.ffconcat"
    out.write_text("\n".join(lines) + "\n")
    return out
