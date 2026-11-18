"""Downloader tests: real ffmpeg test clip, all network mocked."""
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yt_dlp
from sqlalchemy import select, update

from app import config
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, SourceKind, SourceStatus
from app.db.models import Campaign, Source
from app.downloader import fetchers, media, service
from app.downloader.fetchers import DownloadError, direct_fetch, map_ytdlp_error

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    base = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest"]
    for codecs in (["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"], ["-c:v", "mpeg4", "-c:a", "aac"]):
        if subprocess.run(base + codecs + [str(out)], capture_output=True).returncode == 0:
            return out
    pytest.skip("ffmpeg could not encode a test clip")


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(config.settings, "MAX_SOURCE_MINUTES", 240)
    monkeypatch.setattr(config.settings, "YTDLP_COOKIES_FILE", "")
    monkeypatch.setattr(service, "has_space", lambda extra_gb: True)
    monkeypatch.setattr(service, "notify", lambda *a, **k: False)
    eng = init_db(get_engine(str(tmp_path / "dl.db")))
    with get_session(eng) as s:
        s.add(Campaign(id=1, external_id="c1", name="Camp", url="https://cr.test/1", status=CampaignStatus.joined))
    yield eng
    eng.dispose()


def add_source(engine, url, kind, status=SourceStatus.pending) -> int:
    with get_session(engine) as s:
        src = Source(campaign_id=1, url=url, kind=kind, status=status)
        s.add(src)
        s.flush()
        return src.id


def get(engine, sid) -> Source:
    with get_session(engine) as s:
        return s.get(Source, sid)


def fake_ydl(sample: Path, calls: list | None = None, error: str | None = None):
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            if calls is not None:
                calls.append(url)
            if error:
                raise yt_dlp.utils.DownloadError(error)
            target = self.opts["outtmpl"].replace("%(ext)s", "mp4")
            shutil.copy(sample, target)
            return {"id": "x", "duration": 3}

    return FakeYDL


def test_probe(sample_video):
    info = media.probe(sample_video)
    assert info["duration_sec"] == pytest.approx(3.0, abs=0.2)
    assert (info["width"], info["height"]) == (320, 240)
    assert info["has_audio"] is True and info["has_video"] is True


def test_youtube_success(engine, sample_video, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake_ydl(sample_video))
    sid = add_source(engine, "https://www.youtube.com/watch?v=aqz-KE-bpKQ", SourceKind.youtube)
    assert service.download_source(sid, engine) == "downloaded"
    src = get(engine, sid)
    assert src.status == SourceStatus.downloaded and src.error is None
    assert src.duration_sec == pytest.approx(3.0, abs=0.2)
    path = Path(src.local_path)
    assert path.exists() and path.name == f"{sid}.mp4" and path.parent.name == "1"
    assert not list(path.parent.glob("*.part")) and not list(path.parent.glob(f"{sid}.dl.*"))


def test_bot_check_maps_to_cookies_message(engine, sample_video, monkeypatch):
    msg = "ERROR: [youtube] aqz-KE-bpKQ: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake_ydl(sample_video, error=msg))
    sid = add_source(engine, "https://www.youtube.com/watch?v=aqz-KE-bpKQ", SourceKind.youtube)
    assert service.download_source(sid, engine) == "failed"
    src = get(engine, sid)
    assert src.status == SourceStatus.failed and src.error == "sign-in/bot check (set YTDLP_COOKIES_FILE)"


def test_error_mapping():
    assert map_ytdlp_error("ERROR: [youtube] x: Private video. Sign in if you've been granted access") \
        == "private/unavailable"
    assert map_ytdlp_error("ERROR: [youtube] x: Video unavailable") == "private/unavailable"
    assert map_ytdlp_error("The uploader has not made this video available in your country") == "geo-blocked"
    assert map_ytdlp_error("File is larger than max-filesize") == "too large"


def test_too_long_fails_and_removes_file(engine, sample_video, monkeypatch, tmp_path):
    monkeypatch.setattr(config.settings, "MAX_SOURCE_MINUTES", 0.02)  # 1.2 s
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake_ydl(sample_video))
    sid = add_source(engine, "https://kick.com/a/videos/1", SourceKind.kick)
    assert service.download_source(sid, engine) == "failed"
    src = get(engine, sid)
    assert src.error == "too long" and src.local_path is None
    assert not any((tmp_path / "downloads" / "1").glob(f"{sid}*"))


def test_drive_html_fails(engine, monkeypatch):
    def fake_download(url, output, quiet, fuzzy):
        Path(output).write_text("<!DOCTYPE html><html><body>Quota exceeded</body></html>")
        return output

    monkeypatch.setattr(fetchers.gdown, "download", fake_download)
    sid = add_source(engine, "https://drive.google.com/file/d/abc/view", SourceKind.drive)
    assert service.download_source(sid, engine) == "failed"
    assert get(engine, sid).error == "drive: not public or quota exceeded"


def test_drive_file_success(engine, sample_video, monkeypatch):
    def fake_download(url, output, quiet, fuzzy):
        shutil.copy(sample_video, output)
        return output

    monkeypatch.setattr(fetchers.gdown, "download", fake_download)
    sid = add_source(engine, "https://drive.google.com/file/d/abc/view", SourceKind.drive)
    assert service.download_source(sid, engine) == "downloaded"
    assert Path(get(engine, sid).local_path).suffix == ".mp4"


def test_drive_folder_expands(engine, monkeypatch):
    listing = [SimpleNamespace(id="f1", path="clips/a.mp4", local_path="x"),
               SimpleNamespace(id="f2", path="clips/b.MOV", local_path="x"),
               SimpleNamespace(id="f3", path="clips/readme.txt", local_path="x")]
    monkeypatch.setattr(fetchers.gdown, "download_folder", lambda **kw: listing)
    add_source(engine, "https://drive.google.com/file/d/f1/view", SourceKind.drive)  # already present
    folder = add_source(engine, "https://drive.google.com/drive/folders/FOLDER", SourceKind.drive)
    assert service.download_source(folder, engine) == "processed"
    with get_session(engine) as s:
        urls = sorted(s.scalars(select(Source.url).where(Source.id != folder)))
        assert s.get(Source, folder).status == SourceStatus.processed and s.get(Source, folder).error is None
    assert urls == ["https://drive.google.com/file/d/f1/view", "https://drive.google.com/file/d/f2/view"]


def test_claim_is_atomic(engine, sample_video, monkeypatch):
    calls: list = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake_ydl(sample_video, calls))
    sid = add_source(engine, "https://www.youtube.com/watch?v=aaaaaaaaaaa", SourceKind.youtube,
                     status=SourceStatus.downloading)
    assert service.download_source(sid, engine) == "downloading"
    assert calls == [] and get(engine, sid).status == SourceStatus.downloading


def test_disk_full(engine, monkeypatch):
    monkeypatch.setattr(service, "has_space", lambda extra_gb: False)
    sid = add_source(engine, "https://www.youtube.com/watch?v=aaaaaaaaaaa", SourceKind.youtube)
    assert service.download_source(sid, engine) == "failed"
    assert get(engine, sid).error == "disk full"


def test_has_space_respects_max_disk(tmp_path, monkeypatch):
    (tmp_path / "big.bin").write_bytes(b"0" * 1024)
    monkeypatch.setattr(config.settings, "MAX_DISK_GB", 0)
    assert media.has_space(0.001, tmp_path) is False
    assert media.disk_usage_gb(tmp_path) > 0


def test_reset_stuck(engine):
    old = add_source(engine, "https://a.test/1.mp4", SourceKind.direct, status=SourceStatus.downloading)
    fresh = add_source(engine, "https://a.test/2.mp4", SourceKind.direct, status=SourceStatus.downloading)
    with get_session(engine) as s:
        s.execute(update(Source).where(Source.id == old)
                  .values(updated_at=datetime.now(timezone.utc) - timedelta(hours=3)))
    assert service.reset_stuck(120, engine) == 1
    assert get(engine, old).status == SourceStatus.pending
    assert get(engine, fresh).status == SourceStatus.downloading


def test_download_pending_order_and_limit(engine, sample_video, monkeypatch):
    calls: list = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", fake_ydl(sample_video, calls))
    ids = [add_source(engine, f"https://www.youtube.com/watch?v=vid{i:08d}", SourceKind.youtube) for i in range(3)]
    summary = service.download_pending(limit=2, engine=engine)
    assert summary == {"downloaded": 2, "failed": 0, "expanded": 0, "skipped": 0}
    assert calls == [f"https://www.youtube.com/watch?v=vid{i:08d}" for i in range(2)]
    assert get(engine, ids[2]).status == SourceStatus.pending


def test_direct_fetch(tmp_path, sample_video):
    body = sample_video.read_bytes()

    def handler(request):
        if request.url.path.endswith("page.mp4"):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>")
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    path = direct_fetch("https://cdn.test/clip.mp4", tmp_path / "7", client=client)
    assert path.read_bytes() == body
    with pytest.raises(DownloadError, match="not a video"):
        direct_fetch("https://cdn.test/page.mp4", tmp_path / "8", client=client)
    assert not list(tmp_path.glob("8*"))


def test_dashboard_download_button(engine, monkeypatch):
    from fastapi.testclient import TestClient

    from app.dashboard.app import create_app
    from app.dashboard.routes import sources as sources_routes

    started: list = []
    monkeypatch.setattr(sources_routes, "download_source", lambda sid, eng=None: started.append(sid))
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    client = TestClient(create_app(engine))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    sid = add_source(engine, "https://a.test/f.mp4", SourceKind.direct, status=SourceStatus.failed)
    assert "Download</button>" in client.get("/campaigns/1").text
    r = client.post(f"/sources/{sid}/download", follow_redirects=False)
    assert r.status_code == 303 and "Download+started" in r.headers["location"]
    assert started == [sid] and get(engine, sid).status == SourceStatus.pending
