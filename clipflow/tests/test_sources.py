"""Footage sources, manual campaigns, Sources page (offline)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.dashboard.app import create_app
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, SourceKind, SourceStatus
from app.db.models import Campaign, Source
from app.scout import service
from app.scout.providers.base import Provider
from app.sources.links import MAX_LINKS, detect_kind, is_drive_folder, normalize_url, parse_links


@pytest.mark.parametrize("url,kind", [
    ("https://drive.google.com/file/d/abc123/view?usp=sharing", SourceKind.drive),
    ("https://drive.google.com/drive/folders/FOLDER1", SourceKind.drive),
    ("https://docs.google.com/document/d/x/edit", SourceKind.drive),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", SourceKind.youtube),
    ("https://youtu.be/dQw4w9WgXcQ", SourceKind.youtube),
    ("https://youtube.com/shorts/dQw4w9WgXcQ", SourceKind.youtube),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", SourceKind.youtube),
    ("https://www.twitch.tv/videos/123", SourceKind.twitch),
    ("https://kick.com/someone/videos/1", SourceKind.kick),
    ("https://cdn.example.com/path/clip.MP4", SourceKind.direct),
    ("https://example.com/page", SourceKind.other),
])
def test_detect_kind(url, kind):
    assert detect_kind(url) == kind


def test_drive_folder():
    assert is_drive_folder("https://drive.google.com/drive/u/0/folders/F1?usp=x")
    assert not is_drive_folder("https://drive.google.com/file/d/abc/view")


def test_normalize_url():
    yt = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert normalize_url("  https://youtu.be/dQw4w9WgXcQ?si=XYZ&t=42 ") == yt
    assert normalize_url("https://youtube.com/shorts/dQw4w9WgXcQ?feature=share") == yt
    assert normalize_url("http://M.YouTube.com/watch?v=dQw4w9WgXcQ&t=10s&utm_source=x") == yt
    assert normalize_url("https://drive.google.com/file/d/abc_123/view?usp=sharing") == \
        "https://drive.google.com/file/d/abc_123/view"
    assert normalize_url("https://drive.google.com/open?id=abc_123") == "https://drive.google.com/file/d/abc_123/view"
    assert normalize_url("https://drive.google.com/drive/u/1/folders/F_1?usp=drive_link") == \
        "https://drive.google.com/drive/folders/F_1"
    assert normalize_url("http://Example.COM/v.mp4?utm_campaign=a&fbclid=b&keep=1") == \
        "https://example.com/v.mp4?keep=1"
    assert normalize_url("https://www.instagram.com/reel/ABC/?igshid=zz") == "https://www.instagram.com/reel/ABC/"


def test_parse_links():
    text = ("https://youtu.be/dQw4w9WgXcQ, https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=1\n"
            "not-a-url ftp://x.com/file https://drive.google.com/drive/folders/F1  www.nohttp.com\n\n")
    valid, invalid = parse_links(text)
    assert valid == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://drive.google.com/drive/folders/F1"]
    assert invalid == ["not-a-url", "ftp://x.com/file", "www.nohttp.com"]
    many = "\n".join(f"https://example.com/v{i}.mp4" for i in range(60))
    valid, invalid = parse_links(many)
    assert len(valid) == MAX_LINKS and len(invalid) == 10


@pytest.fixture
def engine(tmp_path):
    eng = init_db(get_engine(str(tmp_path / "src.db")))
    with get_session(eng) as s:
        s.add_all([
            Campaign(external_id="c1", name="Camp One", url="https://cr.test/1", campaign_type="clipping",
                     status=CampaignStatus.selected, platforms=["youtube"]),
            Campaign(external_id="c2", name="Camp Two", url="https://cr.test/2", campaign_type="clipping",
                     status=CampaignStatus.discovered, platforms=["youtube"]),
        ])
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    monkeypatch.setattr(config.settings, "DASHBOARD_USER", "admin")
    c = TestClient(create_app(engine))
    c.auth = ("admin", "pw")
    return c


def cid(engine, ext):
    with get_session(engine) as s:
        return s.scalar(select(Campaign.id).where(Campaign.external_id == ext))


def sources(engine, campaign_id):
    with get_session(engine) as s:
        return s.scalars(select(Source).where(Source.campaign_id == campaign_id).order_by(Source.id)).all()


def test_post_sources_insert_skip_and_join(client, engine):
    c1 = cid(engine, "c1")
    links = "https://youtu.be/dQw4w9WgXcQ\nhttps://drive.google.com/drive/folders/F1\nbad"
    r = client.post(f"/campaigns/{c1}/sources", data={"links": links, "mark_joined": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "Added+2%2C+skipped+0+duplicates%2C+1+invalid" in r.headers["location"]
    rows = sources(engine, c1)
    assert [(s.kind, s.status) for s in rows] == [(SourceKind.youtube, SourceStatus.pending),
                                                  (SourceKind.drive, SourceStatus.pending)]
    with get_session(engine) as s:
        assert s.get(Campaign, c1).status == CampaignStatus.joined
    r = client.post(f"/campaigns/{c1}/sources",
                    data={"links": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=a https://example.com/new.mp4"},
                    follow_redirects=False)
    assert "Added+1%2C+skipped+1+duplicates%2C+0+invalid" in r.headers["location"]
    assert len(sources(engine, c1)) == 3
    page = client.get(f"/campaigns/{c1}").text
    assert "folder" in page and "Add footage links" in page and "Join this campaign" not in page


def test_post_sources_without_join(client, engine):
    c2 = cid(engine, "c2")
    page = client.get(f"/campaigns/{c2}").text
    assert "Join this campaign on Content Rewards first" in page
    client.post(f"/campaigns/{c2}/sources", data={"links": "https://kick.com/a/videos/1"})
    with get_session(engine) as s:
        assert s.get(Campaign, c2).status == CampaignStatus.discovered
    assert sources(engine, c2)[0].kind == SourceKind.kick


def test_retry_and_delete(client, engine):
    c1 = cid(engine, "c1")
    with get_session(engine) as s:
        s.add_all([Source(campaign_id=c1, url="https://a.test/1.mp4", kind=SourceKind.direct,
                          status=SourceStatus.failed, error="boom"),
                   Source(campaign_id=c1, url="https://a.test/2.mp4", kind=SourceKind.direct,
                          status=SourceStatus.downloaded)])
    failed, done = sources(engine, c1)
    client.post(f"/sources/{done.id}/retry")
    client.post(f"/sources/{failed.id}/retry")
    failed, done = sources(engine, c1)
    assert failed.status == SourceStatus.pending and failed.error is None
    assert done.status == SourceStatus.downloaded
    r = client.post(f"/sources/{done.id}/delete", data={"back": "/sources"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/sources?msg=")
    assert [s.id for s in sources(engine, c1)] == [failed.id]
    assert client.post("/sources/99999/delete").status_code == 404


def test_manual_campaign_create(client, engine):
    assert client.get("/campaigns/new").status_code == 200
    bad = client.post("/campaigns/new", data={"name": "", "url": "nope"})
    assert bad.status_code == 400 and "Name is required" in bad.text
    r = client.post("/campaigns/new", data={"name": "My Podcast", "url": "https://example.com/camp",
                                            "platforms": ["youtube", "instagram"], "cpm_rate": "2",
                                            "budget_left": "5000", "notes": "from DM"},
                    follow_redirects=False)
    assert r.status_code == 303
    with get_session(engine) as s:
        c = s.scalar(select(Campaign).where(Campaign.name == "My Podcast"))
        assert c.external_id.startswith("manual-") and c.status == CampaignStatus.joined
        assert c.campaign_type == "clipping" and c.is_live is True and c.score > 0
        assert c.platforms == ["youtube", "instagram"] and c.notes == "from DM"
    assert r.headers["location"].startswith(f"/campaigns/{c.id}")
    assert "My Podcast" in client.get("/campaigns").text
    assert "＋ Add campaign" in client.get("/campaigns").text


class EmptyProvider(Provider):
    name = "empty"

    def fetch(self):
        return []


def test_scout_never_ends_manual(engine, monkeypatch):
    monkeypatch.setattr(service, "notify_error", lambda *a, **k: False)
    with get_session(engine) as s:
        s.add(Campaign(external_id="manual-abc123", name="Manual", url="https://x.test", campaign_type="clipping",
                       status=CampaignStatus.joined, platforms=["youtube"], cpm_rate=1.0, payout_type="cpm",
                       budget_left=5000.0))
    r = service.run_scout(EmptyProvider(), engine=engine)
    assert r.ended == 2  # c1 (selected) + c2 (discovered) end; the manual one does not
    with get_session(engine) as s:
        m = s.scalar(select(Campaign).where(Campaign.external_id == "manual-abc123"))
        assert m.status == CampaignStatus.joined and m.is_live is True and m.scored_at is not None


def test_sources_page_filter(client, engine):
    c1, c2 = cid(engine, "c1"), cid(engine, "c2")
    with get_session(engine) as s:
        s.add_all([Source(campaign_id=c1, url="https://a.test/failed.mp4", kind=SourceKind.direct,
                          status=SourceStatus.failed),
                   Source(campaign_id=c2, url="https://a.test/pending.mp4", kind=SourceKind.direct,
                          status=SourceStatus.pending)])
    page = client.get("/sources").text
    assert "failed.mp4" in page and "pending.mp4" in page and "Camp One" in page
    page = client.get("/sources", params={"status": "failed"}).text
    assert "failed.mp4" in page and "pending.mp4" not in page and "Retry" in page
