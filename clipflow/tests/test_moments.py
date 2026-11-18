"""Moment picker tests: mocked Gemini (MockTransport), real ffmpeg on tiny generated media."""
import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import config
from app.ai import gemini, usage
from app.ai.usage import month_spend
from app.clipper import moments_service
from app.clipper.moments import (Moment, compact_transcript, per_window_n, pick_heuristic, refine, windows)
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, SourceKind, SourceStatus
from app.db.models import AiUsage, Campaign, Clip, Source
from app.rules import Rules
from app.scheduler.jobs import build_scheduler

KEY = "AIza-test-secret-key"


def words_tx(n=60, step=0.5):
    """Transcript with word k at [0.5k, 0.5k+0.4]; every 10th word ends a sentence."""
    words = [{"w": f"w{k}{'.' if k % 10 == 9 else ''}", "s": round(k * step, 2), "e": round(k * step + 0.4, 2),
              "p": 0.9} for k in range(n)]
    segs = [{"start": words[i]["s"], "end": words[min(i + 9, n - 1)]["e"],
             "text": " ".join(w["w"] for w in words[i:i + 10]), "words": words[i:i + 10]} for i in range(0, n, 10)]
    return {"language": "en", "duration": n * step, "segments": segs}


# --- compact transcript ----------------------------------------------------------------

def test_compact_transcript():
    segs = [{"start": 0.0, "end": 4.0, "text": " Um so so this is the the start"},
            {"start": 4.0, "end": 5.0, "text": " uh yes"},
            {"start": 65.5, "end": 70.0, "text": "Next   part!"},
            {"start": 200.0, "end": 205.0, "text": "outside"}]
    out = compact_transcript(segs, 0, 100)
    assert out == "[0:00] so this is the start yes\n[1:05] Next part!"
    assert compact_transcript(segs, 3700, 3800) == ""
    assert compact_transcript([{"start": 3725, "end": 3730, "text": "late"}], 3700, 3800) == "[1:02:05] late"


# --- gemini client -----------------------------------------------------------------------

@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "GEMINI_API_KEY", KEY)
    monkeypatch.setattr(config.settings, "GEMINI_MODEL", "gemini-test")
    monkeypatch.setattr(config.settings, "AI_MONTHLY_BUDGET_USD", 5)
    monkeypatch.setattr(config.settings, "MOMENTS_PER_SOURCE", 8)
    gemini._NO_THINKING.clear()
    eng = init_db(get_engine(str(tmp_path / "m.db")))
    yield eng
    gemini._NO_THINKING.clear()
    eng.dispose()


def test_generate_json_retries_and_records(engine, caplog):
    bodies, headers = [], []
    ok_body = {"candidates": [{"content": {"parts": [{"text": json.dumps({"moments": []})}]}}],
               "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200}}
    replies = [httpx.Response(400, json={"error": {"message": "thinking_config is not supported for this model"}}),
               httpx.Response(429, json={"error": {"message": "rate"}}),
               httpx.Response(200, json=ok_body)]

    def handler(req):
        bodies.append(json.loads(req.content))
        headers.append(req.headers)
        return replies.pop(0)

    sleeps: list = []
    with caplog.at_level(logging.DEBUG):
        out = gemini.generate_json("prompt", {"type": "OBJECT"}, "moments", max_output_tokens=500,
                                   client=httpx.Client(transport=httpx.MockTransport(handler)), engine=engine,
                                   sleep=sleeps.append)
    assert out == {"moments": []} and sleeps == [2]
    first = bodies[0]["generationConfig"]
    assert first["thinkingConfig"] == {"thinkingBudget": 0} and first["responseSchema"] == {"type": "OBJECT"}
    assert first["responseMimeType"] == "application/json" and first["maxOutputTokens"] == 500
    assert "thinkingConfig" not in bodies[1]["generationConfig"] and "thinkingConfig" not in bodies[2]["generationConfig"]
    assert headers[0]["x-goog-api-key"] == KEY and "key=" not in str(headers[0])
    assert KEY not in caplog.text
    with get_session(engine) as s:
        rows = s.scalars(select(AiUsage).order_by(AiUsage.id)).all()
    assert [r.ok for r in rows] == [False, False, True]
    assert rows[-1].cost_usd == pytest.approx((1000 * 0.30 + 200 * 2.50) / 1e6)
    assert month_spend(engine) == pytest.approx(0.0008)
    assert "gemini-test" in gemini._NO_THINKING


def test_generate_json_no_key(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "GEMINI_API_KEY", "")
    assert gemini.generate_json("p", {}, "moments", engine=engine) is None


# --- refine --------------------------------------------------------------------------

R = Rules(clip_min_sec=5, clip_max_sec=10)


def test_refine_snaps_and_extends_to_sentence():
    out = refine([Moment(2.1, 8.2, "h", 7)], words_tx(), Rules(clip_min_sec=5, clip_max_sec=20), 30)
    assert [(m.start, m.end) for m in out] == [(2.0, 9.9)]


def test_refine_min_max_clamp_banned_dedupe():
    tx = words_tx()
    assert [(m.start, m.end) for m in refine([Moment(2.0, 3.0, "", 5)], tx, R, 30)] == [(2.0, 7.4)]
    assert [(m.start, m.end) for m in refine([Moment(0.0, 30.0, "", 5)], tx, R, 30)] == [(0.0, 9.9)]
    assert [(m.start, m.end) for m in refine([Moment(0.0, 9.0, "", 5)], tx, R, 9.5)] == [(0.0, 9.5)]
    assert refine([Moment(40.0, 45.0, "", 5)], tx, R, 30) == []
    banned = Rules(clip_min_sec=5, clip_max_sec=10, banned_words=["w7"])
    assert refine([Moment(2.0, 8.0, "", 5)], tx, banned, 30) == []
    dup = refine([Moment(0.0, 9.0, "low", 3), Moment(1.0, 9.5, "high", 9), Moment(15.0, 20.0, "other", 5)], tx, R, 30)
    assert [m.hook for m in dup] == ["high", "other"]


def test_windows_and_n(monkeypatch):
    monkeypatch.setattr(config.settings, "MOMENTS_PER_SOURCE", 8)
    w = windows(100 * 60, 45)
    assert w == [(0.0, 2700.0), (2670.0, 5370.0), (5340.0, 6000.0)]
    assert [per_window_n(e - s, 6000) for s, e in w] == [4, 4, 2]
    assert windows(600, 45) == [(0.0, 600.0)]


# --- heuristic ----------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_heuristic_finds_loud_scene(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "MOMENTS_PER_SOURCE", 3)
    video = tmp_path / "h.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10:d=20",
                    "-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=20",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=40,volume=0.02",
                    "-f", "lavfi", "-i", "sine=frequency=800:duration=5,volume=1.0,adelay=20000|20000",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a][3:a]amix=inputs=2:duration=first:"
                                       "normalize=0[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "mpeg4", "-c:a", "aac", str(video)], check=True)
    picked = pick_heuristic(SimpleNamespace(local_path=str(video), duration_sec=40.0), R, 40.0)
    top = picked[0]
    assert top.start <= 20 <= top.end and top.end - top.start == pytest.approx(7.5, abs=1)
    assert top.score == 10 and top.hook == ""
    assert all(a.end <= b.start or b.end <= a.start for i, a in enumerate(picked) for b in picked[i + 1:])


# --- service ---------------------------------------------------------------------------

@pytest.fixture
def world(engine, tmp_path, monkeypatch):
    tx_path = tmp_path / "1.json"
    tx_path.write_text(json.dumps(words_tx()))
    media = tmp_path / "src.mp4"
    media.write_bytes(b"x")
    with get_session(engine) as s:
        s.add_all([Campaign(id=1, external_id="j", name="Joined", url="u", status=CampaignStatus.joined,
                            clip_min_sec=5, clip_max_sec=10),
                   Campaign(id=2, external_id="d", name="Disc", url="u", status=CampaignStatus.discovered)])
    sent: list = []
    monkeypatch.setattr(usage, "notify", lambda *a, **k: sent.append(a))
    return SimpleNamespace(tx=tx_path, media=media, sent=sent)


def add_source(engine, world, campaign_id=1, **kw) -> int:
    values = dict(campaign_id=campaign_id, url=f"https://a.test/{len(str(kw))}{campaign_id}{kw.get('tag', '')}.mp4",
                  kind=SourceKind.direct, status=SourceStatus.downloaded, local_path=str(world.media),
                  transcript_path=str(world.tx), speech_ratio=0.9, word_count=60, duration_sec=30.0)
    kw.pop("tag", None)
    values.update(kw)
    with get_session(engine) as s:
        src = Source(**values)
        s.add(src)
        s.flush()
        return src.id


def ai_gen(moments, calls=None):
    def gen(prompt, schema, purpose, max_output_tokens=1200):
        if calls is not None:
            calls.append(prompt)
        return {"moments": moments}
    return gen


def heur(calls):
    def h(source, rules, duration):
        calls.append(source.id)
        return [Moment(10.0, 17.5, "", 6.0)]
    return h


def clips(engine, sid):
    with get_session(engine) as s:
        return s.scalars(select(Clip).where(Clip.source_id == sid).order_by(Clip.start_sec)).all()


def src_row(engine, sid):
    with get_session(engine) as s:
        return s.get(Source, sid)


def test_ai_path_creates_candidates(engine, world):
    sid = add_source(engine, world)
    calls: list = []
    gen = ai_gen([{"start": 2.1, "end": 8.2, "hook": "  The twist nobody saw  ", "score": 9},
                  {"start": 15, "end": 20, "hook": "x" * 80, "score": 4}], calls)
    assert moments_service.pick_moments(sid, engine, gen, heur([])) == 2
    rows = clips(engine, sid)
    assert [(c.start_sec, c.end_sec, c.status) for c in rows] == [(2.0, 9.9, ClipStatus.candidate),
                                                                  (15.0, 20.4, ClipStatus.candidate)]
    assert rows[0].hook_title == "The twist nobody saw" and len(rows[1].hook_title) == 60 and rows[0].hashtags == []
    src = src_row(engine, sid)
    assert src.moments_method == "ai" and src.moments_at is not None and src.status == SourceStatus.downloaded
    assert "Joined" in calls[0] and "5-10 seconds" in calls[0] and "[0:00]" in calls[0]


def test_ai_none_falls_back_to_heuristic(engine, world):
    sid = add_source(engine, world)
    hcalls: list = []
    assert moments_service.pick_moments(sid, engine, lambda *a, **k: None, heur(hcalls)) == 1
    assert hcalls == [sid] and src_row(engine, sid).moments_method == "heuristic"


def test_budget_exceeded_uses_heuristic_and_warns_once(engine, world):
    with get_session(engine) as s:
        s.add(AiUsage(purpose="moments", model="m", input_tokens=1, output_tokens=1, cost_usd=6.0, ok=True))
    a, b = add_source(engine, world, tag="a"), add_source(engine, world, tag="b")
    gcalls, hcalls = [], []
    for sid in (a, b):
        moments_service.pick_moments(sid, engine, ai_gen([], gcalls), heur(hcalls))
    assert gcalls == [] and hcalls == [a, b] and len(world.sent) == 1
    assert src_row(engine, a).moments_method == "heuristic"


def test_low_speech_uses_heuristic(engine, world):
    sid = add_source(engine, world, speech_ratio=0.05)
    gcalls, hcalls = [], []
    moments_service.pick_moments(sid, engine, ai_gen([], gcalls), heur(hcalls))
    assert gcalls == [] and hcalls == [sid]


def test_no_duplicates_and_eligibility(engine, world):
    sid = add_source(engine, world)
    gen = ai_gen([{"start": 2.1, "end": 8.2, "hook": "h", "score": 9}])
    assert moments_service.pick_moments(sid, engine, gen, heur([])) == 1
    assert moments_service.pick_moments(sid, engine, gen, heur([])) == 0  # moments_at set → not eligible
    with get_session(engine) as s:
        s.get(Source, sid).moments_at = None
    assert moments_service.pick_moments(sid, engine, gen, heur([])) == 0  # same moment → not duplicated
    assert len(clips(engine, sid)) == 1
    other = add_source(engine, world, campaign_id=2)
    untranscribed = add_source(engine, world, transcript_path=None, tag="u")
    tired = add_source(engine, world, moments_attempts=2, tag="t")
    gave_up = add_source(engine, world, transcript_path=None, transcribe_attempts=2, tag="g")
    hcalls: list = []
    for s_id in (other, untranscribed, tired):
        assert moments_service.pick_moments(s_id, engine, gen, heur(hcalls)) == 0
    assert hcalls == []
    assert moments_service.pick_moments(gave_up, engine, gen, heur(hcalls)) == 1 and hcalls == [gave_up]


def test_failure_counts_attempt(engine, world):
    sid = add_source(engine, world)

    def boom(*a, **k):
        raise RuntimeError("ffmpeg exploded")

    assert moments_service.pick_moments(sid, engine, lambda *a, **k: None, boom) == 0
    src = src_row(engine, sid)
    assert src.moments_attempts == 1 and src.status == SourceStatus.downloaded
    assert src.error.startswith("moments: RuntimeError")


def test_pick_pending(engine, world):
    add_source(engine, world, tag="1")
    add_source(engine, world, campaign_id=2, tag="2")
    assert moments_service.pick_pending(limit=3, engine=engine, generate=lambda *a, **k: None,
                                        heuristic=heur([])) == {"sources": 1, "candidates": 1}


def test_scheduler_has_seven_jobs():
    jobs = {j.id for j in build_scheduler().get_jobs()}
    assert len(jobs) >= 7 and "moments" in jobs
