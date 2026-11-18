"""Render candidate clips to 9:16 MP4 with burned captions and hook; never raises to callers."""
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import Engine, and_, func, or_, select, update

from app.clipper.compliance import check_clip
from app.clipper.captions import build_ass, ffmpeg_has_ass, font_path, render_overlay_sequence
from app.clipper.framing import choose_layout, crop_width, crop_x_expr, detect_faces_track, sample_times
from app.clipper.transcribe_service import load_transcript, words_in_range
from app.config import settings
from app.db import get_session
from app.db.enums import CampaignStatus, ClipStatus, SourceStatus
from app.db.models import Campaign, Clip, Source
from app.downloader.media import probe
from app.logger import get_logger
from app.rules import get_rules
from app.rules.caption import render_caption

log = get_logger("clipflow.render")

MAX_ATTEMPTS = 2
DURATION_TOLERANCE = 0.3
THUMB_AT = 1.0
THUMB_WIDTH = 360
ERROR_MAX = 200


class RenderError(Exception):
    """Rendering failed (short message)."""


def _run(args: list[str], cwd: Path, timeout: float) -> None:
    try:
        out = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RenderError("ffmpeg timed out") from exc
    except OSError as exc:
        raise RenderError(f"ffmpeg not runnable: {exc}") from exc
    if out.returncode != 0:
        lines = [ln for ln in (out.stderr or "").strip().splitlines() if ln.strip()]
        raise RenderError(f"ffmpeg: {lines[-1][:160] if lines else 'exit ' + str(out.returncode)}")


def _video_chain(layout: str, src_w: int, src_h: int, track: list, clip_len: float) -> str:
    W, H = settings.RENDER_WIDTH, settings.RENDER_HEIGHT
    if layout == "crop":
        cw, ch = crop_width(src_w, src_h), src_h // 2 * 2
        x = crop_x_expr(track, src_w, src_h, clip_len)
        # single quotes protect the commas of the if() expression inside the filtergraph
        return f"[0:v]crop=w={cw}:h={ch}:x='{x}':y=0,scale={W}:{H},setsar=1[base]"
    return (f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2,"
            f"eq=brightness=-0.08[bg];"
            f"[fgsrc]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")


def _claim(clip_id: int, engine: Optional[Engine]) -> bool:
    with get_session(engine) as s:
        res = s.execute(update(Clip).where(
            Clip.id == clip_id, Clip.status == ClipStatus.candidate,
            or_(Clip.render_attempts.is_(None), Clip.render_attempts < MAX_ATTEMPTS))
            .values(status=ClipStatus.rendering, updated_at=datetime.now(timezone.utc)))
        return res.rowcount == 1


def _mark_source_processed(source_id: int, engine: Optional[Engine]) -> None:
    with get_session(engine) as s:
        open_clips = s.scalar(select(func.count()).select_from(Clip).where(
            Clip.source_id == source_id, Clip.status.in_([ClipStatus.candidate, ClipStatus.rendering])))
        src = s.get(Source, source_id)
        if not open_clips and src is not None and src.status == SourceStatus.downloaded and src.moments_at:
            src.status = SourceStatus.processed
            log.info("source %d processed (all clips rendered)", source_id)


def render_clip(clip_id: int, engine: Optional[Engine] = None, track_fn: Optional[Callable] = None) -> bool:
    """Render one candidate clip. Returns True on success. Never raises."""
    try:
        if not _claim(clip_id, engine):
            return False
    except Exception as exc:  # noqa: BLE001
        log.error("clip %d: render claim failed: %s", clip_id, exc)
        return False
    started = time.monotonic()
    tmp_dir = Path(settings.CLIPS_DIR) / f".tmp_{clip_id}"
    part = thumb = None
    source_id = None
    try:
        with get_session(engine) as s:
            clip = s.get(Clip, clip_id)
            src = s.get(Source, clip.source_id)
            campaign = s.get(Campaign, clip.campaign_id)
            rules = get_rules(campaign)
            s.expunge_all()
        source_id = src.id
        if not src.local_path or not Path(src.local_path).is_file():
            raise RenderError("source file missing")
        start, clip_len = float(clip.start_sec), float(clip.end_sec - clip.start_sec)
        if clip_len <= 0.5:
            raise RenderError("clip too short")
        hook = clip.hook_title or (campaign.name or "")[:40].strip()
        info = probe(src.local_path)
        src_w, src_h = info["width"], info["height"]
        if not (info["has_video"] and src_w and src_h):
            raise RenderError("source has no video stream")
        W, H = settings.RENDER_WIDTH, settings.RENDER_HEIGHT

        track: list = []
        forced = (settings.RENDER_LAYOUT or "auto").lower()
        if forced != "pad" and src_w > src_h:
            track = (track_fn or detect_faces_track)(src.local_path, start, start + clip_len)
        layout = choose_layout(track, len(sample_times(clip_len)), src_w, src_h)

        words: list = []
        if rules.burn_captions and src.transcript_path and Path(src.transcript_path).is_file():
            words = words_in_range(load_transcript(src), start, start + clip_len)

        tmp_dir.mkdir(parents=True, exist_ok=True)
        inputs = ["-ss", f"{start:.3f}", "-t", f"{clip_len:.3f}", "-i", str(Path(src.local_path).resolve())]
        graph = [_video_chain(layout, src_w, src_h, track, clip_len)]
        if ffmpeg_has_ass():
            # run inside tmp_dir with relative names: no filtergraph path escaping needed (spaces/colons safe)
            (tmp_dir / "c.ass").write_text(build_ass(words, start, hook, rules, None, W, H), encoding="utf-8")
            os.symlink(font_path().parent, tmp_dir / "fonts")
            graph.append(f"[base]ass=c.ass:fontsdir=fonts,fps={settings.RENDER_FPS},format=yuv420p[v]")
        else:
            render_overlay_sequence(words, start, hook, rules, clip_len, W, H, tmp_dir)
            inputs += ["-f", "concat", "-safe", "0", "-i", "overlay.ffconcat"]
            graph.append(f"[1:v]format=rgba[cap];[base][cap]overlay=0:0:eof_action=pass:format=auto,"
                         f"fps={settings.RENDER_FPS},format=yuv420p[v]")
        audio_idx = 0
        if not info["has_audio"]:
            audio_idx = 2 if not ffmpeg_has_ass() else 1
            inputs += ["-f", "lavfi", "-t", f"{clip_len:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph.append(f"[{audio_idx}:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]")

        out_dir = Path(settings.CLIPS_DIR) / str(clip.campaign_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        final = (out_dir / f"{clip_id}.mp4").resolve()
        part = final.with_name(f"{clip_id}.part.mp4")
        thumb = final.with_name(f"{clip_id}.jpg")
        _run([*inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", str(settings.RENDER_CRF),
              "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart",
              "-t", f"{clip_len:.3f}", str(part)], cwd=tmp_dir, timeout=max(300.0, clip_len * 30))

        out = probe(part)
        if not (out["has_video"] and out["has_audio"]):
            raise RenderError("output missing video or audio")
        if (out["width"], out["height"]) != (W, H):
            raise RenderError(f"output size {out['width']}x{out['height']}")
        if abs((out["duration_sec"] or 0) - clip_len) > DURATION_TOLERANCE:
            raise RenderError(f"output duration {out['duration_sec']}s vs {clip_len:.2f}s")
        _run(["-ss", f"{min(THUMB_AT, clip_len / 2):.2f}", "-i", str(part), "-frames:v", "1",
              "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", str(thumb)], cwd=tmp_dir, timeout=60)
        part.replace(final)
        with get_session(engine) as s:
            c = s.get(Clip, clip_id)
            c.status, c.file_path, c.thumb_path = ClipStatus.rendered, str(final), str(thumb)
            c.duration_sec, c.layout, c.rendered_at = out["duration_sec"], layout, datetime.now(timezone.utc)
            c.caption, c.hashtags, c.hook_title = render_caption(rules, hook), list(rules.hashtags), hook
            c.reject_reason, c.file_deleted_at = None, None
        log.info("clip %d rendered (%s) in %.1fs → %s", clip_id, layout, time.monotonic() - started, final)
        check_clip(clip_id, autofix=True, engine=engine)  # never raises; result saved on the clip
        return True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) if isinstance(exc, RenderError) else f"{type(exc).__name__}: {exc}"
        msg = f"render: {msg}".replace("\n", " ")
        msg = msg if len(msg) <= ERROR_MAX else msg[: ERROR_MAX - 1] + "…"
        for f in (part, thumb):
            if f is not None:
                Path(f).unlink(missing_ok=True)
        try:
            with get_session(engine) as s:
                c = s.get(Clip, clip_id)
                c.render_attempts = (c.render_attempts or 0) + 1
                if c.render_attempts >= MAX_ATTEMPTS:
                    c.status, c.reject_reason = ClipStatus.failed, msg
                else:
                    c.status = ClipStatus.candidate
        except Exception as db_exc:  # noqa: BLE001
            log.error("clip %d: could not record render failure: %s", clip_id, db_exc)
        log.warning("clip %d %s", clip_id, msg)
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if source_id is not None:
            try:
                _mark_source_processed(source_id, engine)
            except Exception as exc:  # noqa: BLE001
                log.warning("source %d: processed check failed: %s", source_id, exc)


def render_pending(limit: Optional[int] = None, engine: Optional[Engine] = None,
                   track_fn: Optional[Callable] = None) -> dict:
    """Render the best-scoring candidates of joined campaigns."""
    limit = limit or settings.RENDER_PER_RUN
    with get_session(engine) as s:
        ids = list(s.scalars(
            select(Clip.id).join(Campaign, Clip.campaign_id == Campaign.id)
            .where(and_(Clip.status == ClipStatus.candidate,
                        or_(Clip.render_attempts.is_(None), Clip.render_attempts < MAX_ATTEMPTS)),
                   Campaign.status == CampaignStatus.joined)
            .order_by(Clip.moment_score.desc(), Clip.id).limit(limit)))
    summary = {"rendered": 0, "failed": 0}
    for cid in ids:
        summary["rendered" if render_clip(cid, engine, track_fn) else "failed"] += 1
    return summary
