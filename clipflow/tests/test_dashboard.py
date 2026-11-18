"""Dashboard tests (TestClient, temp DB, no network)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.dashboard.app import create_app
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus
from app.db.models import Campaign, CampaignSnapshot
from app.scout import service as scout_service
from app.scout.service import ScoutResult

NOW = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)


def tc(app, auth=None):
    """TestClient with optional Basic auth credentials."""
    c = TestClient(app)
    if auth:
        c.auth = auth
    return c


@pytest.fixture
def engine(tmp_path):
    eng = init_db(get_engine(str(tmp_path / "dash.db")))
    with get_session(eng) as s:
        a = Campaign(external_id="a", name="Alpha Podcast", brand="PodCo", url="https://cr.test/a",
                     campaign_type="clipping", payout_type="cpm", cpm_rate=2.0, est_per_clip=6.0,
                     budget_left=9000.0, creators_count=10, platforms=["youtube"], score=80.0,
                     score_breakdown={"earnings": 18.0, "budget": 12.5}, score_reasons=["high $/clip ($6.00)"],
                     requires_application=True, last_seen_at=NOW)
        b = Campaign(external_id="b", name="<i>Beta</i> Gaming", brand="GameCo", url="https://cr.test/b",
                     campaign_type="clipping", payout_type="cpm", cpm_rate=1.0, score=60.0, last_seen_at=NOW)
        u = Campaign(external_id="u", name="Gamma UGC", url="https://cr.test/u", campaign_type="ugc",
                     score=0.0, last_seen_at=NOW)
        s.add_all([a, b, u])
        s.flush()
        s.add_all([CampaignSnapshot(campaign_id=a.id, budget_left=9500.0, creators_count=8,
                                    captured_at=NOW - timedelta(hours=12)),
                   CampaignSnapshot(campaign_id=a.id, budget_left=9000.0, creators_count=10, captured_at=NOW)])
    yield eng
    eng.dispose()


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setattr(config.settings, "DASHBOARD_USER", "admin")
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "s3cret")


@pytest.fixture
def client(engine, password):
    return tc(create_app(engine), ("admin", "s3cret"))


def ids(engine):
    with get_session(engine) as s:
        return {c.external_id: c.id for c in s.scalars(select(Campaign))}


def test_health_no_auth(engine, password):
    r = TestClient(create_app(engine)).get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": True}


def test_auth_required(engine, password):
    app = create_app(engine)
    assert TestClient(app).get("/").status_code == 401
    assert tc(app, ("admin", "nope")).get("/").status_code == 401
    assert tc(app, ("root", "s3cret")).get("/").status_code == 401
    assert tc(app, ("admin", "s3cret")).get("/").status_code == 200


def test_no_password_localhost_only(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "")
    local = TestClient(create_app(engine, extra_local_hosts={"testclient"}))
    r = local.get("/")
    assert r.status_code == 200 and "Set DASHBOARD_PASSWORD before exposing this dashboard" in r.text
    assert TestClient(create_app(engine)).get("/").status_code == 403


def test_home_counts(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Live clipping campaigns" in r.text and "Posts today" in r.text and "Run scout now" in r.text


def test_campaign_list_sort_filter_search(client):
    r = client.get("/campaigns")
    assert r.status_code == 200
    assert r.text.index("Alpha Podcast") < r.text.index("&lt;i&gt;Beta&lt;/i&gt; Gaming")
    assert "Gamma UGC" not in r.text and "<i>Beta</i>" not in r.text and "needs app" in r.text
    r = client.get("/campaigns", params={"type": "ugc"})
    assert "Gamma UGC" in r.text and "Alpha Podcast" not in r.text
    r = client.get("/campaigns", params={"q": "gameco"})
    assert "Beta" in r.text and "Alpha Podcast" not in r.text
    r = client.get("/campaigns", params={"sort": "cpm", "type": "all", "status": "all"})
    assert r.status_code == 200 and "Gamma UGC" in r.text


def test_status_post(client, engine):
    cid = ids(engine)["a"]
    r = client.post(f"/campaigns/{cid}/status", data={"status": "joined", "notes": "go"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/campaigns/{cid}?msg=")
    with get_session(engine) as s:
        c = s.get(Campaign, cid)
        assert c.status == CampaignStatus.joined and c.notes == "go" and c.score == 80.0
    r = client.post(f"/campaigns/{cid}/status", data={"status": "selected", "back": "/campaigns?sort=cpm"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/campaigns?sort=cpm&msg=")
    r = client.post(f"/campaigns/{cid}/status", data={"status": "selected", "back": "//evil.test/x"},
                    follow_redirects=False)
    assert r.headers["location"].startswith(f"/campaigns/{cid}?")
    assert client.post(f"/campaigns/{cid}/status", data={"status": "bogus"}).status_code == 400
    assert client.post("/campaigns/99999/status", data={"status": "joined"}).status_code == 404


def test_detail_page(client, engine):
    r = client.get(f"/campaigns/{ids(engine)['a']}")
    assert r.status_code == 200
    assert "earnings" in r.text and "18.0" in r.text and "high $/clip ($6.00)" in r.text
    assert "$9,500" in r.text and "$9,000" in r.text
    assert "Footage sources" in r.text and 'target="_blank"' in r.text
    assert client.get("/campaigns/99999").status_code == 404


def test_action_scout(client, monkeypatch):
    monkeypatch.setattr(scout_service, "run_scout", lambda *a, **k: ScoutResult(fetched=12, new=3))
    r = client.post("/actions/scout", follow_redirects=False)
    assert r.status_code == 303 and "Scout+done%3A+fetched+12%2C+new+3" in r.headers["location"]
    assert "Scout done: fetched 12, new 3" in client.get(r.headers["location"]).text

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(scout_service, "run_scout", boom)
    r = client.post("/actions/scout", follow_redirects=False)
    assert r.status_code == 303 and "Scout+failed" in r.headers["location"]
