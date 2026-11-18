"""Clip renderer tests: real ffmpeg on tiny generated media, face detection mocked."""
import json
import re
from datetime import datetime, timezone
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app import config
from app.clipper import captions
from app.clipper.captions import build_ass, caption_events, render_overlay_sequence
from app.clipper.framing import choose_layout, crop_x_expr
from app.clipper.render import render_clip, render_pending
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, SourceKind, SourceStatus
from app.db.models import Campaign, Clip, Source
from app.downloader.media import probe
from app.rules import Rules
from app.scheduler.jobs import build_scheduler

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

WORDS = [{"w": "Hello", "s": 2.5, "e": 2.9, "p": 0.9}, {"w": "{weird}\\word", "s": 3.0, "e": 3.4, "p": 0.9},
         {"w": "there!", "s": 3.5, "e": 3.9, "p": 0.9}, {"w": "next", "s": 4.2, "e": 4.6, "p": 0.9},
         {"w": "line", "s": 4.7, "e": 5.1, "p": 0.9}]


# --- framing -----------------------------------------------------------------------------

def test_crop_x_expr_constant_moving_clamped_capped():
    steady = [(t / 2, 0.5) for t in range(10)]
    assert crop_x_expr(steady, 1280, 720, 5) == "438"  # cw=404 → 640-202
    moving = [(t / 2, 0.25) for t in range(8)] + [(4 + t / 2, 0.75) for t in range(8)]
    expr = crop_x_expr(moving, 1280, 720, 8)
    assert expr == "if(lt(t,4),118,758)"
    assert crop_x_expr([(0, 0.0), (0.5, 0.0)], 1280, 720, 1) == "0"
    assert crop_x_expr([(0, 1.0), (0.5, 1.0)], 1280, 720, 1) == str(1280 - 404)
    assert crop_x_expr([], 1280, 720, 5) == str((1280 - 404) // 2)
    zigzag = [(i * 0.5, 0.1 if (i // 4) % 2 == 0 else 0.9) for i in range(120)]
    many = crop_x_expr(zigzag, 1280, 720, 60)
    assert 1 <= many.count("if(") <= 11
    assert all(0 <= int(x) <= 876 for x in re.findall(r",(\d+)[,)]", many))


def test_choose_layout(monkeypatch):
    monkeypatch.setattr(config.settings, "RENDER_LAYOUT", "auto")
    assert choose_layout([], 10, 720, 1280) == "pad"
    assert choose_layout([(0, 0.5)] * 4, 10, 1280, 720) == "crop"
    assert choose_layout([(0, 0.5)] * 3, 10, 1280, 720) == "pad"
    assert choose_layout([], 10, 1280, 720) == "pad"
    monkeypatch.setattr(config.settings, "RENDER_LAYOUT", "crop")
    assert choose_layout([], 10, 1280, 720) == "crop"


# --- captions ----------------------------------------------------------------------------

def test_build_ass_highlight_escape_hook():
    rules = Rules(burn_captions=True)
    ass = build_ass(WORDS, 2.0, "Wait for the twist", rules, "Anton", 1080, 1920)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass and "Style: Cap,Anton,81," in ass
    caps = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 0,")]
    assert len(caps) == len(WORDS)
    assert caps[0].startswith("Dialogue: 0,0:00:00.50,0:00:01.00,Cap")
    assert "{\\c&H00FFFF&}HELLO{\\c&HFFFFFF&}" in caps[0] and "THERE!" in caps[0]
    assert "{weird}" not in ass and "(WEIRD)∖WORD" in ass
    assert "NEXT" not in caps[0] and "NEXT LINE" in caps[3].replace("{\\c&H00FFFF&}", "").replace("{\\c&HFFFFFF&}", "")
    hook = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,")]
    assert hook == ["Dialogue: 1,0:00:00.00,0:00:03.00,Hook,,0,0,0,,{\\fad(200,0)}Wait for the twist"]
    only_hook = build_ass(WORDS, 2.0, "A very long hook that needs wrapping onto lines", Rules(burn_captions=False),
                          None, 1080, 1920)
    assert "Dialogue: 0," not in only_hook and "\\N" in only_hook
    assert "Dialogue: 0," not in build_ass([], 0, "hook", rules, None, 1080, 1920)


def test_caption_events_grouping():
    evs = caption_events(WORDS, 2.0, per_line=3)
    assert [e.words for e in evs][:3] == [["HELLO", "(WEIRD)∖WORD", "THERE!"]] * 3
    assert evs[3].words == ["NEXT", "LINE"] and evs[3].current == 0 and evs[4].end == pytest.approx(3.1)


def test_overlay_sequence(tmp_path):
    lst = render_overlay_sequence(WORDS, 2.0, "Hook here", Rules(), 6.0, 270, 480, tmp_path)
    text = lst.read_text()
    durations = [float(x) for x in re.findall(r"duration ([\d.]+)", text)]
    assert sum(durations) == pytest.approx(6.0, abs=0.01)
    assert len(list(tmp_path.glob("o*.png"))) >= 6


# --- rendering ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def source_video(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("src") / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=10:size=1280x720:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)], check=True)
    return out


@pytest.fixture
def world(tmp_path, monkeypatch, source_video):
    clips_dir = tmp_path / "clips dir: with space"  # exercises path handling
    monkeypatch.setattr(config.settings, "CLIPS_DIR", str(clips_dir))
    monkeypatch.setattr(config.settings, "RENDER_LAYOUT", "auto")
    tx = tmp_path / "t.json"
    tx.write_text(json.dumps({"language": "en", "duration": 10, "segments": [
        {"start": 2.5, "end": 5.1, "text": "x", "words": WORDS}]}))
    eng = init_db(get_engine(str(tmp_path / "r.db")))
    with get_session(eng) as s:
        s.add(Campaign(id=1, external_id="c", name="Big Campaign Name For Hooks", url="u",
                       status=CampaignStatus.joined, required_hashtags=["#clip"]))
        s.add(Source(id=1, campaign_id=1, url="https://a.test/s.mp4", kind=SourceKind.direct,
                     status=SourceStatus.downloaded, local_path=str(source_video), transcript_path=str(tx),
                     duration_sec=10.0, moments_at=datetime.now(timezone.utc)))
    yield eng, clips_dir
    eng.dispose()


def add_clip(eng, start=2.0, end=8.0, hook="Wait for the twist") -> int:
    with get_session(eng) as s:
        c = Clip(campaign_id=1, source_id=1, start_sec=start, end_sec=end, hook_title=hook, moment_score=8,
                 hashtags=[], status=ClipStatus.candidate)
        s.add(c)
        s.flush()
        return c.id


def get_clip(eng, cid) -> Clip:
    with get_session(eng) as s:
        return s.get(Clip, cid)


def test_render_pad(world):
    eng, clips_dir = world
    cid = add_clip(eng)
    assert render_clip(cid, eng, track_fn=lambda *a: []) is True
    c = get_clip(eng, cid)
    assert c.status == ClipStatus.rendered and c.layout == "pad" and c.rendered_at is not None
    info = probe(c.file_path)
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert info["duration_sec"] == pytest.approx(6.0, abs=0.3) and c.duration_sec == pytest.approx(6.0, abs=0.3)
    assert Path(c.thumb_path).exists() and Path(c.file_path).name == f"{cid}.mp4"
    assert c.caption.startswith("Wait for the twist") and "#clip" in c.caption and c.hashtags == ["#clip"]
    assert not list(clips_dir.glob(".tmp_*")) and not list(clips_dir.rglob("*.part.mp4"))


def test_render_crop_forced(world, monkeypatch):
    eng, _ = world
    monkeypatch.setattr(config.settings, "RENDER_LAYOUT", "crop")
    cid = add_clip(eng, 1.0, 5.0, hook=None)
    track = [(t / 2, 0.3 if t < 4 else 0.7) for t in range(8)]
    assert render_clip(cid, eng, track_fn=lambda *a: track) is True
    c = get_clip(eng, cid)
    assert c.layout == "crop" and c.hook_title == "Big Campaign Name For Hooks"
    assert (probe(c.file_path)["width"], probe(c.file_path)["height"]) == (1080, 1920)


def test_render_failure_then_failed(world, tmp_path):
    eng, _ = world
    cid = add_clip(eng)
    with get_session(eng) as s:
        s.get(Source, 1).local_path = str(tmp_path / "missing.mp4")
    assert render_clip(cid, eng) is False
    c = get_clip(eng, cid)
    assert (c.status, c.render_attempts) == (ClipStatus.candidate, 1)
    assert render_clip(cid, eng) is False
    c = get_clip(eng, cid)
    assert c.status == ClipStatus.failed and c.reject_reason == "render: source file missing"
    assert render_clip(cid, eng) is False and get_clip(eng, cid).render_attempts == 2


def test_source_processed_after_last_clip(world):
    eng, _ = world
    a, b = add_clip(eng, 1.0, 3.0), add_clip(eng, 4.0, 6.0)
    assert render_pending(limit=1, engine=eng, track_fn=lambda *x: [])["rendered"] == 1
    with get_session(eng) as s:
        assert s.get(Source, 1).status == SourceStatus.downloaded
    assert render_pending(limit=5, engine=eng, track_fn=lambda *x: [])["rendered"] == 1
    with get_session(eng) as s:
        assert s.get(Source, 1).status == SourceStatus.processed
    assert {get_clip(eng, a).status, get_clip(eng, b).status} == {ClipStatus.rendered}


def test_clip_pages_and_video_range(world, monkeypatch):
    from fastapi.testclient import TestClient

    from app.dashboard.app import create_app

    eng, _ = world
    cid = add_clip(eng)
    render_clip(cid, eng, track_fn=lambda *a: [])
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    client = TestClient(create_app(eng))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    grid = client.get("/clips")
    assert grid.status_code == 200 and f"/clips/{cid}/thumb" in grid.text and "Wait for the twist" in grid.text
    assert "<video" in client.get(f"/clips/{cid}").text
    part = client.get(f"/clips/{cid}/video", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert client.get(f"/clips/{cid}/thumb").headers["content-type"] == "image/jpeg"
    assert "1/1" in client.get("/campaigns/1").text
    assert client.get("/clips/999/video").status_code == 404


def test_scheduler_has_eight_jobs():
    jobs = {j.id for j in build_scheduler().get_jobs()}
    assert len(jobs) >= 8 and "render" in jobs


def test_font_present():
    assert captions.font_path("Anton").name == "Anton-Regular.ttf"
