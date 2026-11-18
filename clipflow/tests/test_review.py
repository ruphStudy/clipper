"""Review service, pages, bulk actions and digest (temp DB, render_clip mocked)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.dashboard.app import create_app
from app.dashboard.routes import review as review_routes
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, Platform, SourceKind, SourceStatus
from app.db.models import Account, Campaign, Clip, Post, Source
from app.review import service
from app.scheduler import jobs

OK = {"ok": True, "errors": [], "warnings": []}
WARN = {"ok": True, "errors": [], "warnings": ["hook title empty"]}
BAD = {"ok": False, "errors": ["no audio stream"], "warnings": []}
CAPS = {"youtube": {"platform": "youtube", "title": "Hook #shorts", "description": "Hook\n#brand #ad"},
        "x": {"platform": "x", "text": "Hook #brand #ad"}}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "CLIPS_DIR", str(tmp_path / "clips"))
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    eng = init_db(get_engine(str(tmp_path / "r.db")))
    with get_session(eng) as s:
        for cid, score in ((1, 50.0), (2, 80.0)):
            s.add(Campaign(id=cid, external_id=f"c{cid}", name=f"Camp {cid}", url="u", status=CampaignStatus.joined,
                           score=score, platforms=["youtube", "x"], post_platforms=["youtube", "x"],
                           required_hashtags=["#brand"], disclosure_tag="#ad", clip_min_sec=10, clip_max_sec=30))
            s.add(Source(id=cid, campaign_id=cid, url=f"https://a.test/{cid}.mp4", kind=SourceKind.direct,
                         status=SourceStatus.processed, duration_sec=60.0))
    yield eng
    eng.dispose()


def add(engine, tmp_path, campaign=1, score=5.0, status=ClipStatus.rendered, compliance=OK, start=10.0, end=25.0,
        **kw) -> int:
    f = tmp_path / f"clip_{campaign}_{score}_{start}_{len(kw)}.mp4"
    f.write_bytes(b"x")
    with get_session(engine) as s:
        c = Clip(campaign_id=campaign, source_id=campaign, start_sec=start, end_sec=end, hook_title="Hook",
                 moment_score=score, hashtags=[], status=status, compliance=compliance, file_path=str(f),
                 thumb_path=str(f) + ".jpg", platform_captions={k: dict(v) for k, v in CAPS.items()},
                 duration_sec=end - start, **kw)
        s.add(c)
        s.flush()
        return c.id


def clip(engine, cid) -> Clip:
    with get_session(engine) as s:
        return s.get(Clip, cid)


@pytest.fixture
def client(engine):
    c = TestClient(create_app(engine))
    c.auth = (config.settings.DASHBOARD_USER, "pw")
    return c


# --- service -------------------------------------------------------------------------

def test_queue_filters_and_orders(engine, tmp_path):
    low = add(engine, tmp_path, campaign=1, score=9)
    top_campaign_low = add(engine, tmp_path, campaign=2, score=3)
    top_campaign_high = add(engine, tmp_path, campaign=2, score=7, compliance=WARN)
    add(engine, tmp_path, campaign=2, score=10, compliance=BAD)
    add(engine, tmp_path, campaign=2, score=10, compliance=None)
    add(engine, tmp_path, campaign=2, score=10, status=ClipStatus.candidate)
    add(engine, tmp_path, campaign=2, score=10, status=ClipStatus.approved)
    with get_session(engine) as s:
        assert [c.id for c in service.review_queue(s)] == [top_campaign_high, top_campaign_low, low]
        assert [c.id for c in service.review_queue(s, campaign_id=1)] == [low]


def test_approve(engine, tmp_path):
    bad = add(engine, tmp_path, compliance=BAD)
    ok, msg = service.approve(bad, engine=engine)
    assert not ok and msg.startswith("fix errors first")
    good = add(engine, tmp_path)
    ok, _ = service.approve(good, engine=engine)
    c = clip(engine, good)
    assert ok and c.status == ClipStatus.approved and c.approved_at and c.reviewed_at and not c.edited


def test_approve_with_edits(engine, tmp_path):
    cid = add(engine, tmp_path)
    ok, msg = service.approve(cid, {"x": {"text": "New text #ad"}}, engine=engine)
    assert not ok and "x text: missing hashtag #brand" in msg
    ok, msg = service.approve(cid, {"x": {"text": "#brand #ad " + "y" * 280}}, engine=engine)
    assert not ok and "x text: 291 chars > 280" in msg
    ok, msg = service.approve(cid, {"youtube": {"title": "t" * 101}}, engine=engine)
    assert not ok and "youtube title: 101 chars > 100" in msg
    assert clip(engine, cid).status == ClipStatus.rendered
    ok, _ = service.approve(cid, {"x": {"text": "Better hook #brand #ad"}, "youtube": {"title": "Better #shorts"}},
                            engine=engine)
    c = clip(engine, cid)
    assert ok and c.edited is True and c.platform_captions["x"]["text"] == "Better hook #brand #ad"
    assert c.platform_captions["youtube"]["title"] == "Better #shorts"
    assert c.platform_captions["youtube"]["description"] == "Hook\n#brand #ad"


def test_reject_reasons(engine, tmp_path):
    a, b = add(engine, tmp_path), add(engine, tmp_path, score=6)
    ok, msg = service.reject(a, "meh", engine=engine)
    assert not ok and "invalid reason" in msg
    assert service.reject(a, "Bad Cut", engine=engine)[0] and clip(engine, a).reject_reason == "bad cut"
    assert service.reject(b, "other: " + "z" * 100, engine=engine)[0]
    c = clip(engine, b)
    assert c.status == ClipStatus.rejected and c.reject_reason == "other: " + "z" * 80 and c.reviewed_at
    assert not service.reject(b, "other:   ", engine=engine)[0]


def test_rerender(engine, tmp_path):
    cid = add(engine, tmp_path, start=10.0, end=25.0)
    old_file = Path(clip(engine, cid).file_path)
    assert service.rerender(cid, start_delta=-6, engine=engine) == (False, "adjustments must be within ±5s")
    assert clip(engine, cid).status == ClipStatus.rendered
    ok, msg = service.rerender(cid, hook="  New hook ", start_delta=-1, end_delta=2, engine=engine)
    assert ok and msg == f"Re-rendering #{cid}"
    c = clip(engine, cid)
    assert (c.start_sec, c.end_sec, c.hook_title, c.status, c.render_attempts, c.compliance) == \
        (9.0, 27.0, "New hook", ClipStatus.candidate, 0, None)
    assert not old_file.exists() and c.file_path is None
    with get_session(engine) as s:
        s.get(Clip, cid).status = ClipStatus.rendered
    edge = add(engine, tmp_path, start=1.0, end=20.0)
    assert service.rerender(edge, start_delta=-2, engine=engine)[1] == "start would be before the beginning of the source"
    late = add(engine, tmp_path, start=40.0, end=58.0)
    assert service.rerender(late, end_delta=3, engine=engine)[1] == "end would be past the end of the source"
    short = add(engine, tmp_path, start=10.0, end=21.0)
    assert "outside 10-30s" in service.rerender(short, end_delta=-2, engine=engine)[1]
    approved = add(engine, tmp_path, status=ClipStatus.approved)
    assert not service.rerender(approved, engine=engine)[0]


def test_undo_only_before_scheduling(engine, tmp_path):
    a, b = add(engine, tmp_path), add(engine, tmp_path, score=7)
    service.approve(a, engine=engine)
    assert service.undo(a, engine=engine)[0] and clip(engine, a).status == ClipStatus.rendered
    assert clip(engine, a).approved_at is None
    service.approve(b, engine=engine)
    with get_session(engine) as s:
        acct = Account(platform=Platform.youtube, handle="@me")
        s.add(acct)
        s.flush()
        s.add(Post(clip_id=b, account_id=acct.id, platform=Platform.youtube))
    ok, msg = service.undo(b, engine=engine)
    assert not ok and "already scheduled/posted" in msg
    assert not service.undo(a, engine=engine)[0]  # rendered: nothing to undo


def test_stats_today(engine, tmp_path):
    a, b, _ = add(engine, tmp_path), add(engine, tmp_path, score=7), add(engine, tmp_path, score=8)
    service.approve(a, engine=engine)
    service.reject(b, "boring", engine=engine)
    assert service.stats_today(engine) == {"approved": 1, "rejected": 1, "waiting": 1}


# --- pages -------------------------------------------------------------------------------

def test_review_page_flow(engine, tmp_path, client):
    empty = client.get("/review").text
    assert "All caught up" in empty and "/clips?status=rendered&compliance=error" in empty
    first = add(engine, tmp_path, campaign=2, score=9)
    second = add(engine, tmp_path, campaign=2, score=5, compliance=WARN)
    page = client.get("/review").text
    assert f"#{first} Hook" in page and "1 of 2 waiting" in page and "<video" in page and "autoplay" in page
    assert f'href="/review?id={second}"' in page and "keydown" in page and "data-limit" in page
    assert 'Review <span class="badge badge-selected">2</span>' in page
    skipped = client.get(f"/review?id={second}").text
    assert f"#{second} Hook" in skipped and "2 of 2 waiting" in skipped and "hook title empty" in skipped
    r = client.post(f"/review/{first}/approve", data={"cap__x__text": "Edited #brand #ad"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/review?msg=Approved")
    assert clip(engine, first).platform_captions["x"]["text"] == "Edited #brand #ad"
    r = client.post(f"/review/{second}/approve", data={"cap__x__text": "missing tags"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/review?id={second}&msg=caption+problems")
    r = client.post(f"/review/{second}/reject", data={"reason": "other", "other": "wrong product"},
                    follow_redirects=False)
    assert r.status_code == 303 and clip(engine, second).reject_reason == "other: wrong product"
    assert "All caught up" in client.get("/review").text


def test_rerender_route_starts_render(engine, tmp_path, client, monkeypatch):
    started: list = []
    monkeypatch.setattr(review_routes, "render_clip", lambda cid, eng=None: started.append(cid))
    cid = add(engine, tmp_path)
    r = client.post(f"/review/{cid}/rerender", data={"hook": "Fixed", "start_delta": "1", "end_delta": "-1"},
                    follow_redirects=False)
    assert r.status_code == 303 and f"Re-rendering+%23{cid}" in r.headers["location"] and started == [cid]
    r = client.post(f"/review/{cid}/rerender", data={"start_delta": "9"}, follow_redirects=False)
    assert "within" in r.headers["location"] and started == [cid]


def test_bulk_and_undo_page(engine, tmp_path, client):
    good = [add(engine, tmp_path, score=s) for s in (1, 2, 3)]
    bad = [add(engine, tmp_path, score=s, compliance=BAD) for s in (4, 5)]
    grid = client.get("/clips").text
    assert 'name="ids"' in grid and "Approve selected" in grid
    r = client.post("/clips/bulk", data={"ids": [str(i) for i in good + bad], "action": "approve", "back": "/clips"},
                    follow_redirects=False)
    assert "Approved+3%2C+skipped+2+with+errors" in r.headers["location"]
    assert all(clip(engine, i).status == ClipStatus.approved for i in good)
    assert "Undo approved" in client.get(f"/clips/{good[0]}").text
    client.post(f"/clips/{good[0]}/undo")
    assert clip(engine, good[0]).status == ClipStatus.rendered
    r = client.post("/clips/bulk", data={"ids": [str(bad[0])], "action": "reject", "reason": "quality"},
                    follow_redirects=False)
    assert "Rejected+1" in r.headers["location"] and clip(engine, bad[0]).reject_reason == "quality"


# --- digest + scheduler ---------------------------------------------------------------------

def test_digest_only_when_waiting(engine, tmp_path, monkeypatch):
    from app.db import session as db_session
    import app.notify as notify_mod

    monkeypatch.setattr(db_session, "_engine", engine)
    sent: list = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body="", level="info": sent.append((title, body)) or True)
    monkeypatch.setattr(config.settings, "DASHBOARD_PUBLIC_URL", "https://clips.example.com/")
    assert jobs.job_review_digest() is False and sent == []
    add(engine, tmp_path)
    add(engine, tmp_path, score=6)
    assert jobs.job_review_digest() is True
    assert sent == [("🎬 2 clips waiting for review",
                     "Approved today: 0 · Rejected: 0\nhttps://clips.example.com/review")]


def test_scheduler_has_nine_jobs():
    ids = {j.id for j in jobs.build_scheduler().get_jobs()}
    assert len(ids) >= 9 and "review_digest" in ids
