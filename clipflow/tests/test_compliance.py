"""Compliance checks: real ffmpeg on tiny generated media, temp DB."""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import config
from app.clipper import compliance
from app.clipper.compliance import check_clip, recheck_rules_changed
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, SourceKind, SourceStatus
from app.db.models import Campaign, Clip, Source

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

RULES = dict(clip_min_sec=10, clip_max_sec=30, post_platforms=["youtube", "instagram"], required_hashtags=["#brand"],
             required_mentions=["@brand"], disclosure_tag="#ad", banned_words=["damn"])


def make(path: Path, size="1080x1920", seconds=12, video="testsrc", audio="sine") -> Path:
    vsrc = {"testsrc": f"testsrc=size={size}:rate=10", "black": f"color=c=black:s={size}:r=10"}[video]
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-t", str(seconds), "-i", vsrc]
    if audio == "sine":
        args += ["-f", "lavfi", "-t", str(seconds), "-i", "sine=frequency=440:sample_rate=48000"]
    elif audio == "silent":
        args += ["-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=r=48000:cl=mono"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    subprocess.run(args + [str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {"good": make(d / "good.mp4"), "small": make(d / "small.mp4", size="720x1280"),
            "noaudio": make(d / "noaudio.mp4", audio=None), "short": make(d / "short.mp4", seconds=5),
            "black": make(d / "black.mp4", video="black"), "silent": make(d / "silent.mp4", audio="silent"),
            "wide": make(d / "wide.mp4", size="1280x720", seconds=10)}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "PLATFORM_MAX_SEC", "youtube:180,instagram:90,tiktok:600,x:140")
    monkeypatch.setattr(config.settings, "MAX_CLIP_MB", 200)
    monkeypatch.setattr(config.settings, "CLIPS_DIR", str(tmp_path / "clips"))
    tx = tmp_path / "t.json"
    tx.write_text(json.dumps({"segments": [{"start": 0, "end": 12, "text": "x", "words": [
        {"w": "hello", "s": 1.0, "e": 1.4, "p": 0.9}, {"w": "world", "s": 2.0, "e": 2.4, "p": 0.9}]}]}))
    eng = init_db(get_engine(str(tmp_path / "c.db")))
    with get_session(eng) as s:
        s.add(Campaign(id=1, external_id="c", name="Camp", url="u", status=CampaignStatus.joined,
                       platforms=["youtube", "instagram"], rules_confirmed_at=datetime.now(timezone.utc), **RULES))
        s.add(Source(id=1, campaign_id=1, url="https://a.test/1.mp4", kind=SourceKind.direct,
                     status=SourceStatus.processed, transcript_path=str(tx), local_path=str(tmp_path / "src.mp4")))
        s.add(Source(id=2, campaign_id=1, url="https://a.test/2.mp4", kind=SourceKind.direct,
                     status=SourceStatus.processed))
    yield eng
    eng.dispose()


def add_clip(engine, path, start=0.0, end=12.0, hook="Great hook", source_id=1, status=ClipStatus.rendered,
             **kw) -> int:
    with get_session(engine) as s:
        c = Clip(campaign_id=1, source_id=source_id, start_sec=start, end_sec=end, hook_title=hook, hashtags=[],
                 status=status, file_path=str(path) if path else None, moment_score=7, **kw)
        s.add(c)
        s.flush()
        return c.id


def campaign_set(engine, **values):
    with get_session(engine) as s:
        c = s.get(Campaign, 1)
        for k, v in values.items():
            setattr(c, k, v)


def get_clip(engine, cid) -> Clip:
    with get_session(engine) as s:
        return s.get(Clip, cid)


def test_good_clip_ok(engine, media):
    cid = add_clip(engine, media["good"])
    r = check_clip(cid, engine=engine)
    assert r == {"ok": True, "errors": [], "warnings": []}
    c = get_clip(engine, cid)
    assert c.compliance == r and c.compliance_at is not None
    assert set(c.platform_captions) == {"youtube", "instagram"}
    assert c.platform_captions["youtube"]["title"].endswith("#shorts")


@pytest.mark.parametrize("key,needle", [("small", "resolution 720x1280"), ("noaudio", "no audio stream"),
                                        ("short", "duration 5.0s outside 10-30s"), ("wide", "not 9:16")])
def test_media_errors(engine, media, key, needle):
    r = check_clip(add_clip(engine, media[key]), engine=engine)
    assert not r["ok"] and any(needle in e for e in r["errors"]), r["errors"]


def test_too_long_and_platform_limit(engine, media, monkeypatch):
    fake = {"duration_sec": 100.0, "width": 1080, "height": 1920, "has_audio": True, "has_video": True}
    monkeypatch.setattr(compliance, "probe", lambda p: fake)
    monkeypatch.setattr(compliance, "analyze_av", lambda p, d: {"black": 0, "silence": 0})
    r = check_clip(add_clip(engine, media["good"]), engine=engine)
    assert any("duration 100.0s outside" in e for e in r["errors"])
    campaign_set(engine, clip_max_sec=120)
    r = check_clip(add_clip(engine, media["good"], start=20, end=120), engine=engine)
    assert r["errors"] == ["too long for instagram (100s > 90s)"]


def test_banned_words(engine, media):
    r = check_clip(add_clip(engine, media["good"], hook="Damn that was fast"), engine=engine)
    assert "banned word in hook: damn" in r["errors"]
    assert any(e.startswith("banned word in youtube title") for e in r["errors"])
    campaign_set(engine, banned_words=["world"])
    r = check_clip(add_clip(engine, media["good"]), engine=engine)
    assert "banned word spoken in clip: world" in r["errors"]
    campaign_set(engine, banned_words=["brand"])
    r = check_clip(add_clip(engine, media["good"]), engine=engine)
    assert any(e.startswith("banned word in instagram caption") for e in r["errors"])


def test_autofix_restores_required_tags(engine, media):
    stale = {"instagram": {"platform": "instagram", "caption": "Great hook"},
             "youtube": {"platform": "youtube", "title": "Great hook", "description": "Great hook #brand"}}
    cid = add_clip(engine, media["good"], caption="Great hook", platform_captions=stale)
    r = check_clip(cid, autofix=False, engine=engine)
    assert "instagram: missing hashtag #brand" in r["errors"] and "youtube: missing disclosure #ad" in r["errors"]
    r = check_clip(cid, autofix=True, engine=engine)
    assert r["ok"] and r["errors"] == []
    c = get_clip(engine, cid)
    assert "#brand" in c.caption and "#ad" in c.caption and "@brand" in c.caption and c.hashtags == ["#brand"]
    assert "#ad" in c.platform_captions["instagram"]["caption"]


def test_no_platforms_error(engine, media):
    campaign_set(engine, post_platforms=[], platforms=["tiktok"])
    r = check_clip(add_clip(engine, media["good"]), engine=engine)
    assert any("post_platforms empty" in e for e in r["errors"])


def test_warnings(engine, media):
    campaign_set(engine, rules_confirmed_at=None)
    r = check_clip(add_clip(engine, media["good"], hook="x" * 70), engine=engine)
    assert r["ok"] and "campaign rules not confirmed (rules ?)" in r["warnings"]
    assert "hook title 70 chars (> 60)" in r["warnings"]
    campaign_set(engine, rules_confirmed_at=datetime.now(timezone.utc))
    assert any(w.startswith("black frames") for w in check_clip(add_clip(engine, media["black"]), engine=engine)["warnings"])
    assert any(w.startswith("silence") for w in check_clip(add_clip(engine, media["silent"]), engine=engine)["warnings"])
    posted = add_clip(engine, media["good"], start=0, end=12, status=ClipStatus.posted)
    r = check_clip(add_clip(engine, media["good"], start=2, end=14), engine=engine)
    assert f"near-duplicate of #{posted} (posted)" in r["warnings"]
    r = check_clip(add_clip(engine, media["good"], source_id=2, hook=""), engine=engine)
    assert "no speech captions (no transcript words in range)" in r["warnings"] and "hook title empty" in r["warnings"]


def test_file_missing_and_unexpected(engine, tmp_path, monkeypatch):
    cid = add_clip(engine, tmp_path / "gone.mp4")
    r = check_clip(cid, engine=engine)
    assert not r["ok"] and "clip file missing" in r["errors"]
    assert get_clip(engine, cid).compliance["errors"] == r["errors"]

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(compliance, "_evaluate", boom)
    r = check_clip(cid, engine=engine)
    assert r["errors"] == ["compliance: RuntimeError: kaboom"]
    assert get_clip(engine, cid).compliance["errors"] == r["errors"]


def test_render_triggers_compliance(engine, media, tmp_path):
    from app.clipper.render import render_clip

    with get_session(engine) as s:
        src = s.get(Source, 1)
        src.local_path, src.status, src.moments_at = str(media["wide"]), SourceStatus.downloaded, datetime.now()
    cid = add_clip(engine, None, start=0, end=10, status=ClipStatus.candidate)
    assert render_clip(cid, engine, track_fn=lambda *a: []) is True
    c = get_clip(engine, cid)
    assert c.compliance is not None and c.compliance_at is not None and "youtube" in c.platform_captions


def test_rules_save_rechecks(engine, media, monkeypatch):
    from fastapi.testclient import TestClient

    from app.dashboard.app import create_app

    cid = add_clip(engine, media["good"])
    assert get_clip(engine, cid).compliance is None
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    client = TestClient(create_app(engine))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    form = {"clip_min_sec": "10", "clip_max_sec": "30", "hashtags": "#brand #new", "mentions": "@brand",
            "banned_words": "damn", "post_platforms_present": "1", "post_platforms": ["youtube"],
            "caption_template": "{hook}\n{mentions} {hashtags} {disclosure}", "credit_text": "",
            "disclosure_tag": "#ad", "max_posts_per_day": "3", "burn_captions": "1", "language": "en"}
    assert client.post("/campaigns/1/rules", data=form, follow_redirects=False).status_code == 303
    c = get_clip(engine, cid)
    assert c.compliance is not None and c.compliance["ok"] and "#new" in c.caption
    assert set(c.platform_captions) == {"youtube"}
    page = client.get(f"/clips/{cid}").text
    assert "Platform captions" in page and "Re-check" in page
    assert client.post(f"/clips/{cid}/check", follow_redirects=False).status_code == 303
    assert f"/clips/{cid}" in client.get("/clips", params={"compliance": "ok"}).text
    assert f"/clips/{cid}" not in client.get("/clips", params={"compliance": "error"}).text
    assert recheck_rules_changed(1, engine) == 1


def test_platform_max_sec_parse():
    assert config.platform_max_sec("youtube:180, instagram:90,bad,x:abc") == {"youtube": 180.0, "instagram": 90.0}
