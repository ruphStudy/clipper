"""Download queue (retries) and disk cleanup tests: temp dirs/DB, injected `now`, no network."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app import config
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, SourceKind, SourceStatus
from app.db.models import AppSetting, Campaign, Clip, Source
from app.downloader import cleanup, fetchers, service
from app.downloader.fetchers import DownloadError
from app.scheduler.jobs import build_scheduler

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
MB = 1024 ** 2


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    dl, clips = tmp_path / "downloads", tmp_path / "clips"
    dl.mkdir()
    clips.mkdir()
    for key, val in {"DOWNLOAD_DIR": str(dl), "CLIPS_DIR": str(clips), "MAX_DISK_GB": 20, "KEEP_SOURCE_DAYS": 3,
                     "KEEP_CLIP_DAYS": 7, "DISK_WARN_PCT": 85}.items():
        monkeypatch.setattr(config.settings, key, val)
    notes: list = []
    monkeypatch.setattr(cleanup, "notify", lambda title, body="", level="info": notes.append((title, body)))
    monkeypatch.setattr(service, "notify", lambda *a, **k: False)
    monkeypatch.setattr(service, "has_space", lambda extra_gb: True)
    monkeypatch.setattr(cleanup, "volume_used_pct", lambda *a: 50.0)
    return {"dl": dl, "clips": clips, "notes": notes}


@pytest.fixture
def engine(tmp_path, dirs):
    eng = init_db(get_engine(str(tmp_path / "q.db")))
    with get_session(eng) as s:
        s.add_all([Campaign(id=1, external_id="j", name="Joined", url="u", status=CampaignStatus.joined),
                   Campaign(id=2, external_id="s", name="Selected", url="u", status=CampaignStatus.selected),
                   Campaign(id=3, external_id="e", name="Ended", url="u", status=CampaignStatus.ended)])
    yield eng
    eng.dispose()


def add_source(engine, campaign_id=1, status=SourceStatus.pending, **kw) -> int:
    with get_session(engine) as s:
        src = Source(campaign_id=campaign_id, url=kw.pop("url", f"https://a.test/{os.urandom(4).hex()}.mp4"),
                     kind=SourceKind.direct, status=status, **kw)
        s.add(src)
        s.flush()
        return src.id


def get(engine, model, rid):
    with get_session(engine) as s:
        return s.get(model, rid)


def make_file(path: Path, size_mb: float = 1, age: timedelta = timedelta(0)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * int(size_mb * MB))
    ts = (NOW - age).timestamp()
    os.utime(path, (ts, ts))
    return path


# --- retry policy -------------------------------------------------------------

@pytest.mark.parametrize("msg,kind", [
    ("private/unavailable", "permanent"), ("too long", "permanent"), ("too large", "permanent"),
    ("geo-blocked", "permanent"), ("drive: not public", "permanent"), ("no video stream", "permanent"),
    ("network error: ConnectTimeout", "transient"), ("HTTP 503", "transient"), ("drive: quota exceeded", "transient"),
    ("drive: not public or quota exceeded", "transient"), ("disk full", "transient"),
    ("sign-in/bot check (set YTDLP_COOKIES_FILE)", "transient"),
])
def test_classify_error(msg, kind):
    assert service.classify_error(msg) == kind


def fail_with(monkeypatch, msg):
    def boom(url, out_base, client=None):
        raise DownloadError(msg)
    monkeypatch.setattr(fetchers, "direct_fetch", boom)


def test_transient_backoff_then_permanent(engine, monkeypatch):
    fail_with(monkeypatch, "network error: ReadTimeout")
    sid = add_source(engine)
    t = NOW
    for attempt, delay in enumerate([timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6)], start=1):
        assert service.download_source(sid, engine, now=t) == "failed"
        src = get(engine, Source, sid)
        assert src.attempts == attempt and not src.error_permanent
        assert src.next_retry_at.replace(tzinfo=timezone.utc) == t + delay
        assert service.retry_due(engine, now=t + delay - timedelta(seconds=1)) == 0
        t = t + delay
        assert service.retry_due(engine, now=t) == 1
    assert service.download_source(sid, engine, now=t) == "failed"
    src = get(engine, Source, sid)
    assert src.attempts == 4 and src.error_permanent is True and src.next_retry_at is None
    assert service.retry_due(engine, now=t + timedelta(days=30)) == 0


def test_permanent_not_retried(engine, monkeypatch):
    fail_with(monkeypatch, "private/unavailable")
    sid = add_source(engine)
    service.download_source(sid, engine, now=NOW)
    src = get(engine, Source, sid)
    assert src.error_permanent is True and src.attempts == 1 and src.next_retry_at is None
    assert service.retry_due(engine, now=NOW + timedelta(days=1)) == 0


def test_manual_retry_resets_attempts(engine, monkeypatch):
    from fastapi.testclient import TestClient

    from app.dashboard.app import create_app

    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    sid = add_source(engine, status=SourceStatus.failed, attempts=4, error_permanent=True, error="too long")
    client = TestClient(create_app(engine))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    assert "permanent" in client.get("/campaigns/1").text
    client.post(f"/sources/{sid}/retry")
    src = get(engine, Source, sid)
    assert (src.status, src.attempts, src.error_permanent, src.next_retry_at) == (SourceStatus.pending, 0, False, None)
    sid2 = add_source(engine, status=SourceStatus.failed, attempts=1, next_retry_at=NOW, error="HTTP 503")
    page = client.get("/sources").text
    assert "attempt 1/3" in page and "retry at" in page
    assert sid2


def test_download_pending_only_joined(engine, monkeypatch):
    seen: list = []

    def ok(url, out_base, client=None):
        seen.append(url)
        raise DownloadError("HTTP 503")

    monkeypatch.setattr(fetchers, "direct_fetch", ok)
    joined = add_source(engine, 1, url="https://a.test/joined.mp4")
    add_source(engine, 2, url="https://a.test/selected.mp4")
    add_source(engine, 3, url="https://a.test/ended.mp4")
    service.download_pending(limit=10, engine=engine, now=NOW)
    assert seen == ["https://a.test/joined.mp4"]
    assert get(engine, Source, joined).status == SourceStatus.failed


# --- cleanup -----------------------------------------------------------------------

def test_cleanup_old_processed_source(engine, dirs):
    old = make_file(dirs["dl"] / "1" / "1.mp4")
    keep = make_file(dirs["dl"] / "1" / "2.mp4")
    a = add_source(engine, status=SourceStatus.processed, local_path=str(old), downloaded_at=NOW - timedelta(days=4))
    b = add_source(engine, status=SourceStatus.downloaded, local_path=str(keep), downloaded_at=NOW - timedelta(days=9))
    r = cleanup.run_cleanup(engine=engine, now=NOW)
    assert r["files_deleted"] == 1 and not old.exists() and keep.exists()
    src = get(engine, Source, a)
    assert src.file_deleted_at is not None and src.local_path == str(old)
    assert get(engine, Source, b).file_deleted_at is None


def test_cleanup_inactive_campaign_source(engine, dirs):
    f = make_file(dirs["dl"] / "3" / "1.mp4")
    add_source(engine, 3, status=SourceStatus.downloaded, local_path=str(f), downloaded_at=NOW - timedelta(days=5))
    assert cleanup.run_cleanup(engine=engine, now=NOW)["files_deleted"] == 1 and not f.exists()


def add_clip(engine, status, path: Path, age: timedelta) -> int:
    with get_session(engine) as s:
        src = Source(campaign_id=1, url=f"https://a.test/{path.name}", kind=SourceKind.direct,
                     status=SourceStatus.processed)
        s.add(src)
        s.flush()
        clip = Clip(campaign_id=1, source_id=src.id, start_sec=0, end_sec=30, status=status, file_path=str(path),
                    updated_at=NOW - age)
        s.add(clip)
        s.flush()
        return clip.id


def test_cleanup_clips(engine, dirs):
    posted = make_file(dirs["clips"] / "posted.mp4")
    approved = make_file(dirs["clips"] / "approved.mp4")
    pid = add_clip(engine, ClipStatus.posted, posted, timedelta(days=8))
    add_clip(engine, ClipStatus.approved, approved, timedelta(days=30))
    r = cleanup.run_cleanup(engine=engine, now=NOW)
    assert r["files_deleted"] == 1 and not posted.exists() and approved.exists()
    assert get(engine, Clip, pid).file_deleted_at is not None


def test_over_limit_deletes_oldest_first(engine, dirs, monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_DISK_GB", 10 * MB / 1024 ** 3)  # limit = 10 MB, target 9 MB
    files = []
    for i, days in enumerate([2, 1.5, 1, 0.5]):  # all newer than KEEP_SOURCE_DAYS
        f = make_file(dirs["dl"] / "1" / f"{i}.mp4", size_mb=3)
        add_source(engine, status=SourceStatus.processed, local_path=str(f), downloaded_at=NOW - timedelta(days=days))
        files.append(f)
    keep = make_file(dirs["dl"] / "1" / "busy.mp4", size_mb=0.5)
    add_source(engine, status=SourceStatus.downloaded, local_path=str(keep), downloaded_at=NOW - timedelta(days=9))
    r = cleanup.run_cleanup(engine=engine, now=NOW)  # 12.5 MB → must get ≤ 9 MB: delete the 2 oldest
    assert [f.exists() for f in files] == [False, False, True, True] and keep.exists()
    assert r["files_deleted"] == 2 and r["warnings"] == []


def test_over_limit_nothing_safe_warns(engine, dirs, monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_DISK_GB", 1 * MB / 1024 ** 3)
    f = make_file(dirs["dl"] / "1" / "busy.mp4", size_mb=2)
    add_source(engine, status=SourceStatus.downloaded, local_path=str(f), downloaded_at=NOW - timedelta(days=9))
    r = cleanup.run_cleanup(engine=engine, now=NOW)
    assert f.exists() and "Disk over limit, nothing safe to delete" in r["warnings"]
    assert any("nothing safe" in body for _, body in dirs["notes"])


def test_orphans_parts_and_dry_run(engine, dirs):
    orphan_old = make_file(dirs["dl"] / "9" / "stray.mp4", age=timedelta(hours=30))
    orphan_new = make_file(dirs["clips"] / "fresh.mp4", age=timedelta(hours=2))
    part_old = make_file(dirs["dl"] / "1" / "5.dl.mp4.part", age=timedelta(hours=7))
    part_new = make_file(dirs["dl"] / "1" / "6.dl.mp4.part", age=timedelta(hours=1))
    tracked = make_file(dirs["dl"] / "1" / "7.mp4", age=timedelta(days=5))
    add_source(engine, status=SourceStatus.downloaded, local_path=str(tracked), downloaded_at=NOW)
    old_processed = make_file(dirs["dl"] / "1" / "8.mp4")
    pid = add_source(engine, status=SourceStatus.processed, local_path=str(old_processed),
                     downloaded_at=NOW - timedelta(days=10))

    dry = cleanup.run_cleanup(dry_run=True, engine=engine, now=NOW)
    assert dry["orphans"] == 2 and dry["files_deleted"] == 1 and dry["gb_freed"] > 0
    assert all(p.exists() for p in (orphan_old, part_old, old_processed))
    assert get(engine, Source, pid).file_deleted_at is None

    real = cleanup.run_cleanup(engine=engine, now=NOW)
    assert real["orphans"] == 2
    assert not orphan_old.exists() and not part_old.exists() and not old_processed.exists()
    assert orphan_new.exists() and part_new.exists() and tracked.exists()


def test_disk_warning_throttled(engine, dirs, monkeypatch):
    monkeypatch.setattr(cleanup, "volume_used_pct", lambda *a: 91.0)
    for hours in (0, 5, 23):
        r = cleanup.run_cleanup(engine=engine, now=NOW + timedelta(hours=hours))
        assert r["warnings"] and "91%" in r["warnings"][0]
    assert len(dirs["notes"]) == 1
    cleanup.run_cleanup(engine=engine, now=NOW + timedelta(hours=25))
    assert len(dirs["notes"]) == 2
    with get_session(engine) as s:
        assert s.scalar(select(AppSetting).where(AppSetting.key == "disk_warn_at")) is not None


def test_scheduler_jobs():
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    assert {"daily_scout_am", "daily_scout_pm", "weekly_report", "download_queue", "cleanup"} <= set(jobs)
    assert isinstance(jobs["download_queue"].trigger, IntervalTrigger)
    assert jobs["download_queue"].trigger.interval == timedelta(minutes=20)
    fields = {f.name: str(f) for f in jobs["cleanup"].trigger.fields if not f.is_default}
    assert fields == {"hour": "3", "minute": "20"}
    for j in jobs.values():
        assert j.coalesce is True and j.max_instances == 1
