"""Vertical framing: face tracking (OpenCV Haar), layout choice, and a time-varying crop expression."""
import statistics
from typing import Optional

from app.config import settings
from app.logger import get_logger

log = get_logger("clipflow.framing")

SAMPLE_FPS = 2.0
DETECT_WIDTH = 480
FACE_SHARE_FOR_CROP = 0.4
MOVE_THRESHOLD = 0.12
MIN_SEGMENT_SEC = 1.5
MAX_SEGMENTS = 12


def sample_times(clip_len: float) -> list[float]:
    """Relative sample times at 2 fps."""
    n = max(1, int(clip_len * SAMPLE_FPS))
    return [round(i / SAMPLE_FPS, 3) for i in range(n)]


def detect_faces_track(video: str, start: float, end: float) -> list[tuple[float, float]]:
    """[(t relative to clip start, face centre x / width)] for frames where a face was found (biggest face)."""
    import cv2

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(video))
    track: list[tuple[float, float]] = []
    try:
        if not cap.isOpened() or cascade.empty():
            return track
        for t in sample_times(end - start):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            if w > DETECT_WIDTH:
                frame = cv2.resize(frame, (DETECT_WIDTH, int(h * DETECT_WIDTH / w)))
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
            if len(faces):
                x, _y, fw, _fh = max(faces, key=lambda f: f[2] * f[3])
                track.append((t, (x + fw / 2) / frame.shape[1]))
    finally:
        cap.release()
    return track


def choose_layout(track: list, n_samples: int, source_w: int, source_h: int,
                  forced: Optional[str] = None) -> str:
    """'crop' (follow faces) or 'pad' (blurred background; vertical sources just scale)."""
    forced = (forced if forced is not None else settings.RENDER_LAYOUT or "auto").lower()
    if forced in ("crop", "pad"):
        return forced
    if source_h > source_w:
        return "pad"
    if n_samples and len(track) / n_samples >= FACE_SHARE_FOR_CROP:
        return "crop"
    return "pad"


def crop_width(src_w: int, src_h: int, out_ratio: float = 9 / 16) -> int:
    cw = int(src_h * out_ratio) // 2 * 2
    return min(cw, src_w // 2 * 2)


def _segments(track: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    segs: list[list[tuple[float, float]]] = []
    for t, cx in sorted(track):
        if not segs:
            segs.append([(t, cx)])
            continue
        cur = segs[-1]
        anchor = statistics.median(c for _, c in cur)
        if abs(cx - anchor) > MOVE_THRESHOLD and t - cur[0][0] >= MIN_SEGMENT_SEC:
            segs.append([(t, cx)])
        else:
            cur.append((t, cx))
    while len(segs) > MAX_SEGMENTS:  # merge the shortest into its neighbour
        lens = [(seg[-1][0] - seg[0][0], i) for i, seg in enumerate(segs)]
        _, i = min(lens)
        j = i - 1 if i > 0 else i + 1
        a, b = sorted((i, j))
        segs[a:b + 1] = [segs[a] + segs[b]]
    return segs


def crop_x_expr(track: list[tuple[float, float]], src_w: int, src_h: int, clip_len: float,
                out_ratio: float = 9 / 16) -> str:
    """ffmpeg expression for crop x (t relative to the clip start): constant or nested if(lt(t,T),X,...)."""
    cw = crop_width(src_w, src_h, out_ratio)
    max_x = max(0, src_w - cw)

    def x_for(cx: float) -> int:
        return int(min(max_x, max(0, round(cx * src_w - cw / 2))))

    if not track:
        return str(max_x // 2)
    segs = _segments(track)
    xs = [x_for(statistics.median(c for _, c in seg)) for seg in segs]
    bounds = [round(seg[0][0], 2) for seg in segs[1:]]
    # collapse consecutive segments that ended up at the same x
    merged_x, merged_b = [xs[0]], []
    for b, x in zip(bounds, xs[1:]):
        if x == merged_x[-1]:
            continue
        merged_b.append(b)
        merged_x.append(x)
    expr = str(merged_x[-1])
    for b, x in zip(reversed(merged_b), reversed(merged_x[:-1])):
        expr = f"if(lt(t,{b:g}),{x},{expr})"
    return expr
