"""Submit alerts, deadlines, window-aware planning, bot callbacks, To-submit tab (temp DB, mocks, injected now)."""
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
from app.posting import executor, submit
from app.posting.executor import execute_due
from app.posting.manual import mark_posted
from app.posting.planner import plan_posts
from app.posting.submit import check_submit_deadlines, mark_submitted, send_submit_alert
from app.scheduler.jobs import build_scheduler
from app.uploader.common import finish_post_success

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self, enabled=True, updates=None, fail=False):
        self.enabled, self.updates, self.fail = enabled, updates or [], fail
        self.messages, self.edits, self.answers, self.videos = [], [], [], []
        self.last_message_id = None
        self._next = 500

    def send_message(self, text, reply_markup=None, **_):
        if self.fail:
            raise RuntimeError("telegram down")
        self._next += 1
        self.last_message_id = self._next
        self.messages.append((text, reply_markup))
        return True

    def send_video(self, path, caption=""):
        self.videos.append(path)
        return True

    def edit_message_text(self, message_id, text, reply_markup=None):
        self.edits.append((message_id, text))
        return True

    def answer_callback_query(self, cid, text=""):
        self.answers.append((cid, text))
        return True

    def get_updates(self, offset=None, allowed_updates=None):
        self.allowed = allowed_updates
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    for key, val in {"TIMEZONE": "UTC", "SUBMIT_WINDOW": "09:00-23:30", "SUBMIT_DEADLINE_MIN": 30,
                     "SUBMIT_REMIND_MIN": 15, "POSTING_SLOTS": "youtube:08:30,10:00,23:20;tiktok:12:00",
                     "SLOT_JITTER_MIN": 0, "MIN_GAP_MIN": 60, "PLAN_DAYS_AHEAD": 1,
                     "POST_MODE": "youtube:api,tiktok:manual", "TELEGRAM_CHAT_ID": "111"}.items():
        monkeypatch.setattr(config.settings, key, val)
    monkeypatch.setattr(executor, "notify", lambda *a, **k: None)
    monkeypatch.setattr(executor, "notify_error", lambda *a, **k: None)
    eng = init_db(get_engine(str(tmp_path / "s.db")))
    eng.tmp = tmp_path
    with get_session(eng) as s:
        s.add(Campaign(id=1, external_id="c", name="Ring Drone", url="https://contentrewards.com/c/campaigns/abc",
                       status=CampaignStatus.joined, platforms=["youtube", "tiktok"], post_platforms=["youtube"],
                       max_posts_per_day=50))
        s.add(Source(id=1, campaign_id=1, url="https://a.test/1.mp4", kind=SourceKind.direct,
                     status=SourceStatus.processed))
        s.add(Account(id=1, platform=Platform.youtube, handle="@yt", credentials_ref="t.json", daily_post_limit=50))
        s.add(Account(id=2, platform=Platform.tiktok, handle="@tt", daily_post_limit=50))
    yield eng
    eng.dispose()


def add_post(engine, status=PostStatus.posted, platform=Platform.youtube, posted_at=NOW, alerted=True, **kw) -> int:
    f = engine.tmp / f"c{kw.get('n', 0)}_{platform.value}_{posted_at.timestamp()}.mp4"
    f.write_bytes(b"x")
    with get_session(engine) as s:
        clip = Clip(campaign_id=1, source_id=1, start_sec=0, end_sec=20, hook_title="Hook", hashtags=[],
                    status=ClipStatus.approved, file_path=str(f), approved_at=NOW - timedelta(hours=1),
                    compliance={"ok": True, "errors": [], "warnings": []})
        s.add(clip)
        s.flush()
        post = Post(clip_id=clip.id, account_id=1 if platform == Platform.youtube else 2, platform=platform,
                    status=status, posted_at=posted_at if status == PostStatus.posted else None,
                    post_url=f"https://www.youtube.com/shorts/v{clip.id}" if status == PostStatus.posted else None,
                    submit_alert_at=posted_at if alerted and status == PostStatus.posted else None,
                    scheduled_at=kw.get("scheduled_at"), mode=kw.get("mode", "api"))
        s.add(post)
        s.flush()
        return post.id


def get_post(engine, pid) -> Post:
    with get_session(engine) as s:
        return s.get(Post, pid)


# --- planner / executor window ----------------------------------------------------------------

def test_planner_respects_window_and_margin(engine, monkeypatch):
    monkeypatch.setattr(submit, "get_notifier", lambda: FakeNotifier(enabled=False))
    for i in range(4):
        with get_session(engine) as s:
            s.add(Clip(campaign_id=1, source_id=1, start_sec=0, end_sec=20, status=ClipStatus.approved, hashtags=[],
                       approved_at=NOW - timedelta(minutes=10 - i)))
    plan_posts(NOW.replace(hour=6), engine)
    with get_session(engine) as s:
        times = sorted(p.scheduled_at.strftime("%H:%M") for p in s.scalars(select(Post)))
    # 08:30 is before the window; 23:20 + 15 min would pass 23:30 → only the 10:00 slots are usable
    assert times == ["10:00", "10:00"]
    monkeypatch.setattr(config.settings, "SLOT_JITTER_MIN", 12)
    monkeypatch.setattr(config.settings, "POSTING_SLOTS", "youtube:09:05,23:10")
    from app.posting.modes import in_submit_window
    from app.posting.planner import _candidate_times
    from zoneinfo import ZoneInfo
    cands = _candidate_times("youtube", 99, 1, NOW.replace(hour=6), ZoneInfo("UTC"), days=10)
    assert cands and all(in_submit_window(t) for t in cands)
    assert all(t.time() >= datetime(2026, 1, 1, 9, 0).time() for t in cands)
    assert all((t + timedelta(minutes=15)).time() <= datetime(2026, 1, 1, 23, 30).time() for t in cands)


def test_executor_reschedules_out_of_window(engine):
    pid = add_post(engine, status=PostStatus.scheduled, scheduled_at=NOW.replace(hour=1))
    calls: list = []
    r = execute_due(NOW.replace(hour=2), engine, uploaders={"youtube": lambda *a, **k: calls.append(a)},
                    notifier=FakeNotifier())
    assert r["rescheduled"] == 1 and calls == []
    p = get_post(engine, pid)
    assert p.status == PostStatus.scheduled and p.scheduled_at.strftime("%H:%M") == "10:00"


# --- alerts --------------------------------------------------------------------------------

def test_api_success_triggers_one_alert(engine, monkeypatch):
    n = FakeNotifier()
    monkeypatch.setattr(submit, "get_notifier", lambda: n)
    pid = add_post(engine, status=PostStatus.uploading, alerted=False)
    with get_session(engine) as s:
        clip_id = s.get(Post, pid).clip_id
    finish_post_success(pid, clip_id, "vid", "https://www.youtube.com/shorts/vid", "public", now=NOW, engine=engine)
    assert len(n.messages) == 1
    text, markup = n.messages[0]
    assert text.startswith("🚀 <b>LIVE — submit within 30 min</b> (by 12:30)")
    assert "Ring Drone · youtube @yt" in text and "<code>https://www.youtube.com/shorts/vid</code>" in text
    assert markup == {"inline_keyboard": [[{"text": "📋 Open campaign", "url": "https://contentrewards.com/c/campaigns/abc"}],
                                          [{"text": "✅ Submitted", "callback_data": f"sub:{pid}"}]]}
    p = get_post(engine, pid)
    assert p.alert_message_id == 501 and p.submit_alert_at is not None


def test_alert_failure_does_not_break_posting(engine, monkeypatch):
    monkeypatch.setattr(submit, "get_notifier", lambda: FakeNotifier(fail=True))
    pid = add_post(engine, status=PostStatus.uploading, alerted=False)
    with get_session(engine) as s:
        clip_id = s.get(Post, pid).clip_id
    post = finish_post_success(pid, clip_id, "vid", "https://www.youtube.com/shorts/vid", "public", now=NOW,
                               engine=engine)
    assert post.status == PostStatus.posted and get_post(engine, pid).status == PostStatus.posted


def test_alert_without_telegram_sets_timestamp(engine):
    pid = add_post(engine, alerted=False)
    assert send_submit_alert(pid, engine, FakeNotifier(enabled=False), NOW) is False
    p = get_post(engine, pid)
    assert p.submit_alert_at is not None and p.alert_message_id is None


def test_manual_posted_reply_is_the_alert(engine):
    pid = add_post(engine, status=PostStatus.manual_pending, platform=Platform.tiktok, mode="manual")
    n = FakeNotifier(updates=[{"update_id": 1, "message": {"chat": {"id": 111},
                                                           "text": f"/posted {pid} https://www.tiktok.com/@tt/video/77"}}])
    assert commands.poll_commands(engine, n, NOW) == 1
    assert len(n.messages) == 1 and n.messages[0][0].startswith("🚀 <b>LIVE")
    assert get_post(engine, pid).status == PostStatus.posted and "callback_query" in n.allowed


def test_dashboard_mark_posted_sends_alert(engine, monkeypatch):
    n = FakeNotifier()
    monkeypatch.setattr(submit, "get_notifier", lambda: n)
    pid = add_post(engine, status=PostStatus.manual_pending, platform=Platform.tiktok, mode="manual")
    ok, _ = mark_posted(pid, "https://www.tiktok.com/@tt/video/88", engine)
    assert ok and len(n.messages) == 1 and get_post(engine, pid).alert_message_id == 501


# --- mark submitted + deadlines ---------------------------------------------------------------

def test_mark_submitted(engine):
    n = FakeNotifier()
    pid = add_post(engine)
    with get_session(engine) as s:
        s.get(Post, pid).alert_message_id = 42
    ok, msg = mark_submitted(pid, engine, n, NOW + timedelta(minutes=12))
    assert ok and msg == f"Submitted #{pid} in 12 min"
    p = get_post(engine, pid)
    assert p.status == PostStatus.submitted and p.submitted_at is not None
    assert n.edits == [(42, f"✅ Submitted #{pid} (in 12 min)")]
    late = add_post(engine, n=1)
    ok, msg = mark_submitted(late, engine, n, NOW + timedelta(minutes=45))
    assert ok and "late" in msg
    assert mark_submitted(pid, engine, n, NOW)[0] is False
    sched = add_post(engine, status=PostStatus.scheduled, n=2)
    ok, msg = mark_submitted(sched, engine, n, NOW)
    assert not ok and "not posted" in msg


def test_deadlines_remind_and_miss_once(engine):
    n = FakeNotifier()
    pid = add_post(engine)
    done = add_post(engine, n=1)
    mark_submitted(done, engine, n, NOW + timedelta(minutes=5))
    old = add_post(engine, n=2, alerted=False, posted_at=NOW - timedelta(days=3))  # pre-feature post: ignored
    assert check_submit_deadlines(NOW + timedelta(minutes=14), engine, n) == {"reminded": 0, "missed": 0}
    assert check_submit_deadlines(NOW + timedelta(minutes=15), engine, n) == {"reminded": 1, "missed": 0}
    assert n.messages[-1][0] == f"⏰ 15 min left to submit #{pid}"
    assert n.messages[-1][1]["inline_keyboard"][-1][0]["callback_data"] == f"sub:{pid}"
    assert check_submit_deadlines(NOW + timedelta(minutes=20), engine, n) == {"reminded": 0, "missed": 0}
    assert check_submit_deadlines(NOW + timedelta(minutes=30), engine, n) == {"reminded": 0, "missed": 1}
    assert n.messages[-1][0].startswith(f"⚠️ Window passed for #{pid}")
    assert check_submit_deadlines(NOW + timedelta(minutes=90), engine, n) == {"reminded": 0, "missed": 0}
    p = get_post(engine, pid)
    assert p.status == PostStatus.posted and p.submit_missed and p.submit_reminded
    assert get_post(engine, old).submit_reminded is False and len(n.messages) == 2


# --- telegram callbacks / commands ----------------------------------------------------------------

def test_callback_and_text_commands(engine):
    a = add_post(engine, posted_at=NOW - timedelta(minutes=40))
    b = add_post(engine, n=1, posted_at=NOW - timedelta(minutes=5))
    c = add_post(engine, n=2, posted_at=NOW - timedelta(minutes=20))
    updates = [
        {"update_id": 5, "callback_query": {"id": "cbX", "data": f"sub:{a}", "message": {"chat": {"id": 999}}}},
        {"update_id": 6, "callback_query": {"id": "cb1", "data": f"sub:{a}", "message": {"chat": {"id": 111}}}},
        {"update_id": 7, "message": {"chat": {"id": 111}, "text": "/pending"}},
        {"update_id": 8, "message": {"chat": {"id": 111}, "text": f"/submitted {b}"}},
    ]
    n = FakeNotifier(updates=updates)
    assert commands.poll_commands(engine, n, NOW) == 3
    assert n.answers == [("cb1", "Submitted ✅")] and get_post(engine, a).status == PostStatus.submitted
    pending = n.messages[0][0]
    assert pending.index(f"#{c} youtube · 20 min") < pending.index(f"#{b} youtube · 5 min")
    assert f"#{a}" not in pending
    assert n.messages[1][0].startswith("✅ Submitted") and get_post(engine, b).status == PostStatus.submitted


# --- dashboard -------------------------------------------------------------------------------

def test_to_submit_tab(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    client = TestClient(create_app(engine))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    now = datetime.now(timezone.utc)
    fresh = add_post(engine, posted_at=now - timedelta(minutes=10))
    late = add_post(engine, n=1, posted_at=now - timedelta(minutes=45))
    page = client.get("/posts").text  # default tab when something is waiting
    assert 'http-equiv="refresh" content="30"' in page and "To submit" in page
    assert ":" in page.split('class="badge badge-paused countdown">')[1][:8] and "left" in page
    assert 'badge badge-ended">late</span>' in page and "📋 Open campaign" in page
    assert 'Posts <span class="badge badge-ended" title="to submit">2</span>' in page
    assert 'http-equiv="refresh"' not in client.get("/posts", params={"tab": "upcoming"}).text
    r = client.post(f"/posts/{fresh}/submitted", follow_redirects=False)
    assert r.status_code == 303 and get_post(engine, fresh).status == PostStatus.submitted
    assert "to submit" in client.get("/").text
    assert get_post(engine, late).status == PostStatus.posted


def test_scheduler_has_thirteen_jobs():
    ids = {j.id for j in build_scheduler().get_jobs()}
    assert len(ids) >= 13 and "submit_deadlines" in ids
