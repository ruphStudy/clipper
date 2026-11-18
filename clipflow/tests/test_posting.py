"""Posting planner, executor, manual mode, bot commands and dashboard (temp DB, mocks, injected now)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.dashboard.app import create_app
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, Platform, PostStatus, SourceKind, SourceStatus
from app.db.models import Account, Campaign, Clip, Post, Source
from app.notify import commands
from app.posting import executor, modes
from app.posting.executor import execute_due
from app.posting.manual import mark_posted, normalize_post_url
from app.posting.modes import is_paused, post_mode, slots
from app.posting.planner import plan_posts
from app.scheduler.jobs import build_scheduler
from app.uploader.common import get_setting, set_setting

NOW = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)
SLOTS = "youtube:10:00,14:00,18:00;instagram:11:00,19:00;tiktok:12:00"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    for key, val in {"TIMEZONE": "UTC", "POSTING_SLOTS": SLOTS, "SLOT_JITTER_MIN": 0, "MIN_GAP_MIN": 90,
                     "PLAN_DAYS_AHEAD": 1, "POST_MODE": "youtube:api,instagram:api,tiktok:manual,x:manual",
                     "YOUTUBE_API_AUDITED": False, "MANUAL_REMIND_HOURS": 12, "MANUAL_EXPIRE_HOURS": 48,
                     "DASHBOARD_PUBLIC_URL": "", "TELEGRAM_CHAT_ID": "111"}.items():
        monkeypatch.setattr(config.settings, key, val)
    monkeypatch.setattr(executor, "notify", lambda *a, **k: notes.append(("notify",) + a))
    monkeypatch.setattr(executor, "notify_error", lambda where, exc: notes.append(("error", where, str(exc))))
    notes: list = []
    eng = init_db(get_engine(str(tmp_path / "p.db")))
    eng.notes = notes
    eng.tmp = tmp_path
    with get_session(eng) as s:
        for cid in (1, 2):
            s.add(Campaign(id=cid, external_id=f"c{cid}", name=f"Camp {cid}", url="u", status=CampaignStatus.joined,
                           platforms=["youtube", "instagram", "tiktok"], post_platforms=["youtube"],
                           max_posts_per_day=10))
            s.add(Source(id=cid, campaign_id=cid, url=f"https://a.test/{cid}.mp4", kind=SourceKind.direct,
                         status=SourceStatus.processed))
        s.add(Account(id=1, platform=Platform.youtube, handle="@yt", credentials_ref="youtube_x.json",
                      daily_post_limit=10, is_active=True))
        s.add(Account(id=2, platform=Platform.instagram, handle="ig", credentials_ref="instagram_x.json",
                      daily_post_limit=10, is_active=True))
        s.add(Account(id=3, platform=Platform.tiktok, handle="@tt", daily_post_limit=10, is_active=True))
    yield eng
    eng.dispose()


def add_clip(engine, campaign=1, approved=NOW - timedelta(hours=1), status=ClipStatus.approved) -> int:
    f = engine.tmp / f"clip_{campaign}_{approved.timestamp()}.mp4"
    f.write_bytes(b"x")
    with get_session(engine) as s:
        c = Clip(campaign_id=campaign, source_id=campaign, start_sec=0, end_sec=20, hook_title="Hook", status=status,
                 approved_at=approved, file_path=str(f), hashtags=["#brand"],
                 compliance={"ok": True, "errors": [], "warnings": []},
                 platform_captions={"youtube": {"title": "T #shorts", "description": "D #brand"},
                                    "tiktok": {"caption": "TikTok cap #brand"}})
        s.add(c)
        s.flush()
        return c.id


def set_campaign(engine, cid=1, **values):
    with get_session(engine) as s:
        c = s.get(Campaign, cid)
        for k, v in values.items():
            setattr(c, k, v)


def set_account(engine, aid, **values):
    with get_session(engine) as s:
        a = s.get(Account, aid)
        for k, v in values.items():
            setattr(a, k, v)


def posts(engine, **where):
    with get_session(engine) as s:
        stmt = select(Post).order_by(Post.scheduled_at, Post.id)
        for k, v in where.items():
            stmt = stmt.where(getattr(Post, k) == v)
        return s.scalars(stmt).all()


def hhmm(dt):
    return dt.replace(tzinfo=timezone.utc).strftime("%d %H:%M") if dt.tzinfo is None else dt.strftime("%d %H:%M")


# --- modes ----------------------------------------------------------------------------------

def test_post_mode_auto_and_override(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "POST_MODE", "youtube:auto,instagram:api,tiktok:api")
    assert post_mode("youtube") == "manual"
    monkeypatch.setattr(config.settings, "YOUTUBE_API_AUDITED", True)
    assert post_mode("youtube") == "api" and post_mode("instagram") == "api"
    assert post_mode("tiktok") == "manual"  # no uploader → manual
    with get_session(engine) as s:
        set_setting(s, "youtube_mode_override", "manual")
        assert post_mode("youtube", s) == "manual"
    assert [t.strftime("%H:%M") for t in slots("youtube")] == ["10:00", "14:00", "18:00"] and slots("x") == []


# --- planner ----------------------------------------------------------------------------

def test_plan_basic_and_clip_scheduled(engine):
    cid = add_clip(engine)
    assert plan_posts(NOW, engine) == 1
    (p,) = posts(engine)
    assert (p.platform, p.account_id, p.mode, p.status) == (Platform.youtube, 1, "api", PostStatus.scheduled)
    assert hhmm(p.scheduled_at) == "18 10:00"
    with get_session(engine) as s:
        assert s.get(Clip, cid).status == ClipStatus.scheduled
    assert plan_posts(NOW, engine) == 0  # no duplicate (clip, platform)


def test_lead_time_and_jitter(engine, monkeypatch):
    add_clip(engine)
    assert plan_posts(NOW.replace(hour=9, minute=55), engine) == 1
    assert hhmm(posts(engine)[0].scheduled_at) == "18 14:00"  # 10:00 is within the 10-minute lead
    monkeypatch.setattr(config.settings, "SLOT_JITTER_MIN", 12)
    monkeypatch.setattr(config.settings, "MIN_GAP_MIN", 0)
    set_account(engine, 1, daily_post_limit=100)
    for i in range(6):
        add_clip(engine, approved=NOW - timedelta(minutes=30 - i))
    plan_posts(NOW, engine)
    offsets = set()
    for p in posts(engine):
        at = p.scheduled_at.replace(tzinfo=timezone.utc)
        nearest = min((abs((at - at.replace(hour=h, minute=0)).total_seconds()) / 60) for h in (10, 14, 18))
        assert nearest <= 12
        offsets.add(at.minute)
    assert len(offsets) > 1  # jitter actually varies


def test_jitter_is_deterministic(engine):
    from app.posting.planner import jittered
    base = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    assert jittered(base, "seed-a") == jittered(base, "seed-a")


def test_account_daily_limit_and_days_ahead(engine):
    set_account(engine, 1, daily_post_limit=2)
    ids = [add_clip(engine, approved=NOW - timedelta(minutes=50 - i)) for i in range(6)]
    assert plan_posts(NOW, engine) == 4  # 2 per day × (today + 1 day ahead)
    days = [p.scheduled_at.day for p in posts(engine)]
    assert days.count(18) == 2 and days.count(19) == 2
    with get_session(engine) as s:
        assert [s.get(Clip, i).status for i in ids[4:]] == [ClipStatus.approved] * 2


def test_campaign_max_posts_per_day(engine):
    set_campaign(engine, 1, max_posts_per_day=1)
    for i in range(3):
        add_clip(engine, approved=NOW - timedelta(minutes=10 - i))
    assert plan_posts(NOW, engine) == 2
    assert sorted(p.scheduled_at.day for p in posts(engine)) == [18, 19]


def test_min_gap_between_account_posts(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "POSTING_SLOTS", "youtube:10:00,10:30,13:00")
    add_clip(engine, approved=NOW - timedelta(minutes=5))
    add_clip(engine, approved=NOW - timedelta(minutes=4))
    plan_posts(NOW, engine)
    assert [hhmm(p.scheduled_at) for p in posts(engine)] == ["18 10:00", "18 13:00"]


def test_cross_platform_gap(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "POSTING_SLOTS", "youtube:10:00;instagram:10:15,13:00")
    set_campaign(engine, 1, post_platforms=["youtube", "instagram"])
    add_clip(engine)
    assert plan_posts(NOW, engine) == 2
    times = {p.platform.value: hhmm(p.scheduled_at) for p in posts(engine)}
    assert times == {"youtube": "18 10:00", "instagram": "18 13:00"}


def test_round_robin_across_campaigns(engine):
    set_account(engine, 1, daily_post_limit=2)
    monkeypatch_days = config.settings.PLAN_DAYS_AHEAD
    config.settings.PLAN_DAYS_AHEAD = 0
    try:
        a = [add_clip(engine, 1, NOW - timedelta(hours=5 - i)) for i in range(3)]
        b = add_clip(engine, 2, NOW - timedelta(minutes=1))
        assert plan_posts(NOW, engine) == 2
        assert {p.clip_id for p in posts(engine)} == {a[0], b}
    finally:
        config.settings.PLAN_DAYS_AHEAD = monkeypatch_days


def test_manual_platform_and_unusable_accounts(engine):
    set_campaign(engine, 1, post_platforms=["youtube", "tiktok"])
    set_account(engine, 1, auth_error="reauthorize needed")
    cid = add_clip(engine)
    assert plan_posts(NOW, engine) == 1
    (p,) = posts(engine)
    assert p.platform == Platform.tiktok and p.mode == "manual" and p.account_id == 3
    with get_session(engine) as s:
        assert s.get(Clip, cid).status == ClipStatus.scheduled  # youtube has no usable account → not a target


def test_paused_plans_nothing(engine):
    add_clip(engine)
    modes.set_paused(True, engine)
    assert plan_posts(NOW, engine) == 0
    modes.set_paused(False, engine)
    assert plan_posts(NOW, engine) == 1


# --- executor ----------------------------------------------------------------------------

def fake_uploader(result="posted", error=None, visibility="public", calls=None):
    def upload(clip_id, account_id, engine=None):
        if calls is not None:
            calls.append(clip_id)
        with get_session(engine) as s:
            post = s.scalar(select(Post).where(Post.clip_id == clip_id, Post.account_id == account_id))
            if result == "posted":
                post.status, post.visibility, post.post_url = PostStatus.posted, visibility, "https://youtu.be/x"
                post.posted_at = NOW
                s.get(Clip, clip_id).status = ClipStatus.posted
            else:
                post.status, post.error = PostStatus.failed, error
            s.flush()
            s.expunge(post)
        return post, error or "ok"
    return upload


class FakeNotifier:
    def __init__(self, enabled=True, updates=None):
        self.enabled, self.updates = enabled, updates or []
        self.videos, self.messages, self.offsets = [], [], []

    def send_video(self, path, caption=""):
        self.videos.append((path, caption))
        return True

    def send_message(self, text, **_):
        self.messages.append(text)
        return True

    def get_updates(self, offset=None, allowed_updates=None):
        self.offsets.append(offset)
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]


def planned(engine, **clip_kw):
    cid = add_clip(engine, **clip_kw)
    plan_posts(NOW, engine)
    return cid, posts(engine, clip_id=cid)[0]


def test_executor_api_success_and_locked_override(engine):
    cid, post = planned(engine)
    due = post.scheduled_at.replace(tzinfo=timezone.utc) + timedelta(minutes=1)
    r = execute_due(due, engine, uploaders={"youtube": fake_uploader(visibility="private_locked")},
                    notifier=FakeNotifier())
    assert r["posted"] == 1 and posts(engine)[0].status == PostStatus.posted
    with get_session(engine) as s:
        assert get_setting(s, "youtube_mode_override") == "manual" and s.get(Clip, cid).status == ClipStatus.posted
    assert sum(1 for n in engine.notes if n[0] == "notify") == 1
    assert execute_due(due, engine, uploaders={}, notifier=FakeNotifier())["posted"] == 0


def test_executor_transient_retry_then_fail(engine):
    _, post = planned(engine)
    t = post.scheduled_at.replace(tzinfo=timezone.utc) + timedelta(minutes=1)
    up = {"youtube": fake_uploader("failed", "youtube HTTP 503 backendError")}
    for attempt in (1, 2):
        assert execute_due(t, engine, uploaders=up, notifier=FakeNotifier())["retry"] == 1
        p = posts(engine)[0]
        assert p.status == PostStatus.scheduled and p.attempts == attempt
        assert p.scheduled_at.replace(tzinfo=timezone.utc) == t + timedelta(minutes=60)
        assert execute_due(t + timedelta(minutes=59), engine, uploaders=up, notifier=FakeNotifier())["retry"] == 0
        t += timedelta(minutes=61)
    assert execute_due(t, engine, uploaders=up, notifier=FakeNotifier())["failed"] == 1
    assert posts(engine)[0].status == PostStatus.failed and any(n[0] == "error" for n in engine.notes)


def test_executor_permanent_fails_at_once(engine):
    _, post = planned(engine)
    t = post.scheduled_at.replace(tzinfo=timezone.utc)
    r = execute_due(t, engine, uploaders={"youtube": fake_uploader("failed", "auth: reauthorize needed")},
                    notifier=FakeNotifier())
    assert r["failed"] == 1 and posts(engine)[0].attempts == 0


def test_executor_manual_handoff(engine):
    set_campaign(engine, 1, post_platforms=["tiktok"])
    _, post = planned(engine)
    t = post.scheduled_at.replace(tzinfo=timezone.utc)
    n = FakeNotifier()
    assert execute_due(t, engine, notifier=n)["manual"] == 1
    p = posts(engine)[0]
    assert p.status == PostStatus.manual_pending and p.notified_at is not None
    assert n.videos and n.videos[0][1].startswith(f"#{p.id} tiktok @tt")
    assert "<pre>TikTok cap #brand</pre>" in n.messages[0] and f"/posted {p.id} &lt;link&gt;" in n.messages[0]
    cid2 = add_clip(engine, approved=NOW)
    plan_posts(NOW, engine)
    p2 = posts(engine, clip_id=cid2)[0]
    off = FakeNotifier(enabled=False)
    execute_due(p2.scheduled_at.replace(tzinfo=timezone.utc), engine, notifier=off)
    assert posts(engine, clip_id=cid2)[0].status == PostStatus.manual_pending and off.videos == []


def test_manual_reminder_and_expiry(engine):
    set_campaign(engine, 1, post_platforms=["tiktok"])
    cid, post = planned(engine)
    with get_session(engine) as s:
        p = s.get(Post, post.id)
        p.status, p.notified_at = PostStatus.manual_pending, NOW
    n = FakeNotifier()
    assert execute_due(NOW + timedelta(hours=11), engine, notifier=n)["reminded"] == 0
    assert execute_due(NOW + timedelta(hours=13), engine, notifier=n)["reminded"] == 1
    assert execute_due(NOW + timedelta(hours=14), engine, notifier=n)["reminded"] == 0
    assert len(n.messages) == 1 and "Reminder" in n.messages[0]
    assert execute_due(NOW + timedelta(hours=49), engine, notifier=n)["expired"] == 1
    p = posts(engine)[0]
    assert p.status == PostStatus.failed and p.error == "manual not posted"
    with get_session(engine) as s:
        assert s.get(Clip, cid).status == ClipStatus.approved


def test_executor_paused(engine):
    modes.set_paused(True, engine)
    assert execute_due(NOW, engine, uploaders={}, notifier=FakeNotifier())["paused"] is True


# --- mark posted ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform,url,norm,pid", [
    ("youtube", "https://youtube.com/shorts/abcDEF12345?feature=share", "https://www.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
    ("youtube", "https://youtu.be/abcDEF12345", "https://www.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
    ("youtube", "https://m.youtube.com/watch?v=abcDEF12345", "https://www.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
    ("instagram", "https://www.instagram.com/reel/C1x_Y-z/?igsh=abc", "https://www.instagram.com/reel/C1x_Y-z/", "C1x_Y-z"),
    ("instagram", "https://instagram.com/p/XYZ123/", "https://www.instagram.com/p/XYZ123/", "XYZ123"),
    ("tiktok", "https://www.tiktok.com/@me/video/7300000000000000001?lang=en",
     "https://www.tiktok.com/@me/video/7300000000000000001", "7300000000000000001"),
    ("x", "https://twitter.com/me/status/1790000000000000000?s=20", "https://x.com/me/status/1790000000000000000",
     "1790000000000000000"),
])
def test_normalize_post_url(platform, url, norm, pid):
    assert normalize_post_url(platform, url) == (norm, pid, "")


@pytest.mark.parametrize("platform,url", [("youtube", "https://www.tiktok.com/@a/video/1"),
                                          ("instagram", "https://www.instagram.com/someuser/"),
                                          ("x", "https://x.com/me"), ("tiktok", "ftp://tiktok.com/x")])
def test_normalize_post_url_rejects(platform, url):
    assert normalize_post_url(platform, url)[2]


def test_mark_posted_and_duplicate(engine):
    set_campaign(engine, 1, post_platforms=["tiktok"])
    cid, post = planned(engine)
    assert mark_posted(post.id, "https://youtube.com/shorts/abc123456", engine)[0] is False
    ok, msg = mark_posted(post.id, "https://www.tiktok.com/@me/video/7300000000000000001", engine)
    assert ok
    p = posts(engine)[0]
    assert (p.status, p.platform_post_id, p.visibility) == (PostStatus.posted, "7300000000000000001", "public")
    with get_session(engine) as s:
        assert s.get(Clip, cid).status == ClipStatus.posted
    cid2, post2 = planned(engine, approved=NOW)
    ok, msg = mark_posted(post2.id, "https://www.tiktok.com/@me/video/7300000000000000001?x=1", engine)
    assert not ok and f"post #{post.id}" in msg


# --- telegram commands ------------------------------------------------------------------------

def test_poll_commands(engine):
    set_campaign(engine, 1, post_platforms=["tiktok"])
    _, post = planned(engine)
    with get_session(engine) as s:
        s.get(Post, post.id).status = PostStatus.manual_pending
    updates = [
        {"update_id": 10, "message": {"chat": {"id": 999}, "text": "/pause"}},
        {"update_id": 11, "message": {"chat": {"id": 111}, "text": "/queue"}},
        {"update_id": 12, "message": {"chat": {"id": 111},
                                      "text": f"/posted@ClipBot {post.id} https://www.tiktok.com/@me/video/123"}},
        {"update_id": 13, "message": {"chat": {"id": 111}, "text": "/pause"}},
        {"update_id": 14, "message": {"chat": {"id": 111}, "text": "hello there"}},
    ]
    n = FakeNotifier(updates=updates)
    assert commands.poll_commands(engine, n, NOW) == 3
    assert f"#{post.id} tiktok @tt" in n.messages[0] and n.messages[1].startswith("🚀 <b>LIVE")
    assert posts(engine)[0].status == PostStatus.posted
    with get_session(engine) as s:
        assert is_paused(s) and get_setting(s, "telegram_update_offset") == "15"
    assert commands.poll_commands(engine, n, NOW) == 0 and n.offsets[-1] == 15


# --- dashboard ----------------------------------------------------------------------------------

def test_dashboard_posts_actions(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    client = TestClient(create_app(engine))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    cid, post = planned(engine)
    original = post.scheduled_at.replace(tzinfo=timezone.utc)
    page = client.get("/posts").text
    assert "Upcoming" in page and "Post now" in page and "Tomorrow" in page and "⏸ Pause posting" in page
    client.post(f"/posts/{post.id}/tomorrow")
    assert posts(engine)[0].scheduled_at.replace(tzinfo=timezone.utc) == original + timedelta(days=1)
    client.post(f"/posts/{post.id}/later")
    assert posts(engine)[0].scheduled_at.replace(tzinfo=timezone.utc) == original + timedelta(days=1, hours=1)
    client.post(f"/posts/{post.id}/now")
    assert posts(engine)[0].scheduled_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    client.post(f"/posts/{post.id}/cancel")
    assert posts(engine) == []
    with get_session(engine) as s:
        assert s.get(Clip, cid).status == ClipStatus.approved
    client.post("/posting/toggle")
    with get_session(engine) as s:
        assert is_paused(s)
    assert "▶️ Resume posting" in client.get("/posts").text
    client.post("/posting/toggle")

    set_campaign(engine, 1, post_platforms=["tiktok"])
    plan_posts(NOW, engine)
    manual = posts(engine)[0]
    with get_session(engine) as s:
        s.get(Post, manual.id).status = PostStatus.manual_pending
    page = client.get("/posts", params={"tab": "manual"}).text
    assert f"/clips/{manual.clip_id}/video?download=1" in page and "TikTok cap #brand" in page and "Copy caption" in page
    r = client.post(f"/posts/{manual.id}/mark-posted", data={"url": "https://www.tiktok.com/@me/video/55"},
                    follow_redirects=False)
    assert r.status_code == 303 and posts(engine)[0].status == PostStatus.posted
    home = client.get("/").text
    assert "posted today" in home and "waiting for you" in home


def test_video_download_disposition(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    monkeypatch.setattr(config.settings, "CLIPS_DIR", str(engine.tmp))
    client = TestClient(create_app(engine))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    cid = add_clip(engine)
    r = client.get(f"/clips/{cid}/video", params={"download": 1})
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")


def test_scheduler_has_twelve_jobs():
    ids = {j.id for j in build_scheduler().get_jobs()}
    assert len(ids) >= 12 and {"plan_posts", "execute_posts", "telegram_commands"} <= ids
