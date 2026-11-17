"""Campaign scout tests (offline: fixture + httpx.MockTransport)."""
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus
from app.db.migrate import auto_migrate
from app.db.models import Campaign
from app.scout import service
from app.scout.providers.apify import ApifyProvider
from app.scout.providers.base import Provider
from app.scout.providers.whop_direct import WhopDirectProvider
from app.scout.schema import CampaignDTO, ScoutError

FIXTURE = Path(__file__).parent / "fixtures" / "whop_direct_sample.html"
APIFY_SAMPLE = [
    {
        "title": "Podcast Clips",
        "brand": "PodCo",
        "payoutType": "cpm",
        "rewardPerThousandUsd": 1.5,
        "totalBudgetUsd": 10000,
        "budgetSpentUsd": 2500,
        "budgetLeftUsd": 7500,
        "progressPercentage": 25,
        "socialPlatforms": ["YouTube", "TikTok"],
        "creatorsCount": 40,
        "requiresApplication": False,
        "isVerified": True,
        "fundedAt": "2026-11-01T10:00:00Z",
        "campaignUrl": "https://whop.com/podco/exp_AbC123xyz/app/",
        "description": "Clip the podcast",
        "isLive": True,
    },
    {
        "title": "Game Launch",
        "payoutType": "cpm",
        "rewardPerThousandUsd": 3,
        "totalBudgetUsd": 2000,
        "budgetSpentUsd": 500,
        "socialPlatforms": ["instagram"],
        "campaignUrl": "https://example.com/campaigns/game-launch",
        "isLive": False,
    },
]


class FakeProvider(Provider):
    """In-memory provider for service tests."""

    def __init__(self, dtos, name="fake", partial=False, error=None):
        self.dtos, self.name, self.partial, self.error = dtos, name, partial, error
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.dtos)


def dto(ext, **kw):
    base = dict(external_id=ext, name=f"Camp {ext}", url=f"https://x.test/{ext}", cpm_rate=1.0,
                budget_total=1000.0, budget_spent=100.0)
    base.update(kw)
    return CampaignDTO(**base)


@pytest.fixture
def engine(tmp_path):
    eng = init_db(get_engine(str(tmp_path / "scout.db")))
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def no_alerts(monkeypatch):
    monkeypatch.setattr(service, "notify_error", lambda *a, **k: False)


def _direct(tmp_path, handler=None):
    html = FIXTURE.read_text(encoding="utf-8")
    handler = handler or (lambda req: httpx.Response(200, text=html))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return WhopDirectProvider(client=client, pages={"clipping": "https://cr.test/discover/clipping"},
                              raw_dir=str(tmp_path / "raw"), request_gap=0)


def test_direct_parses_fixture(tmp_path):
    prov = _direct(tmp_path)
    dtos = prov.fetch()
    assert len(dtos) >= 1
    cpm = [d for d in dtos if d.payout_type == "cpm"]
    assert cpm
    for d in cpm:
        assert d.external_id and d.name and d.url.startswith("https://contentrewards.com/c/campaigns/")
        assert d.cpm_rate is not None and d.budget_left is not None and d.platforms
    assert prov.partial is False
    assert not any(d.name.startswith("$$") for d in dtos)
    assert any(d.name.startswith("$9,000") for d in dtos)
    assert len(list((tmp_path / "raw").glob("raw_*.json"))) == 1


def test_direct_bad_page_raises(tmp_path):
    prov = _direct(tmp_path, lambda req: httpx.Response(200, text="<html>nothing</html>"))
    with pytest.raises(ScoutError):
        prov.fetch()


def test_apify_mapping():
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=APIFY_SAMPLE)))
    dtos = ApifyProvider(client=client, token="t").fetch()
    assert len(dtos) == 2
    a, b = dtos
    assert a.external_id == "exp_AbC123xyz" and a.cpm_rate == 1.5 and a.platforms == ["tiktok", "youtube"]
    assert a.funded_at.year == 2026 and a.creators_count == 40 and a.brand == "PodCo"
    assert len(b.external_id) == 40 and b.is_live is False
    assert b.budget_left == 1500 and b.progress_pct == 25


def test_apify_requires_token():
    with pytest.raises(ScoutError, match="APIFY_TOKEN not set"):
        ApifyProvider(token="").fetch()


def test_dto_computes_budget_left_and_progress():
    d = CampaignDTO(external_id="1", name="n", url="u", budget_total=200.0, budget_spent=50.0)
    assert d.budget_left == 150.0 and d.progress_pct == 25.0
    d2 = CampaignDTO(external_id="2", name="n", url="u", budget_total=200.0, budget_spent=50.0,
                     budget_left=10.0, progress_pct=90.0)
    assert d2.budget_left == 10.0 and d2.progress_pct == 90.0


def test_upsert_insert_then_update(engine):
    r1 = service.run_scout(FakeProvider([dto("a"), dto("b")]), engine=engine)
    assert (r1.fetched, r1.new, r1.updated, r1.ended) == (2, 2, 0, 0)
    with get_session(engine) as s:
        first = {c.external_id: (c.first_seen_at, c.last_seen_at) for c in s.scalars(select(Campaign))}
    r2 = service.run_scout(FakeProvider([dto("a", cpm_rate=2.0), dto("b")]), engine=engine)
    assert (r2.new, r2.updated) == (0, 2)
    with get_session(engine) as s:
        rows = {c.external_id: c for c in s.scalars(select(Campaign))}
    for ext, (fs, ls) in first.items():
        assert rows[ext].first_seen_at == fs
        assert rows[ext].last_seen_at > ls
    assert rows["a"].cpm_rate == 2.0 and rows["a"].status == CampaignStatus.discovered


def test_user_fields_survive_update(engine):
    service.run_scout(FakeProvider([dto("a")]), engine=engine)
    with get_session(engine) as s:
        row = s.scalars(select(Campaign)).one()
        row.status, row.notes, row.clip_max_sec = CampaignStatus.joined, "mine", 45
        row.required_hashtags = ["#ad"]
    service.run_scout(FakeProvider([dto("a", name="Renamed")]), engine=engine)
    with get_session(engine) as s:
        row = s.scalars(select(Campaign)).one()
    assert row.name == "Renamed"
    assert (row.status, row.notes, row.clip_max_sec) == (CampaignStatus.joined, "mine", 45)
    assert row.scored_at is not None
    assert row.required_hashtags == ["#ad"]


def test_missing_campaigns_end_or_flag(engine):
    service.run_scout(FakeProvider([dto("d"), dto("j"), dto("keep")]), engine=engine)
    with get_session(engine) as s:
        s.scalars(select(Campaign).where(Campaign.external_id == "j")).one().status = CampaignStatus.joined
    r = service.run_scout(FakeProvider([dto("keep")]), engine=engine)
    assert r.ended == 1
    with get_session(engine) as s:
        rows = {c.external_id: c for c in s.scalars(select(Campaign))}
    assert rows["d"].status == CampaignStatus.ended
    assert rows["j"].status == CampaignStatus.joined and rows["j"].is_live is False
    assert rows["keep"].status == CampaignStatus.discovered


def test_not_live_campaign_ends(engine):
    service.run_scout(FakeProvider([dto("x")]), engine=engine)
    r = service.run_scout(FakeProvider([dto("x", is_live=False)]), engine=engine)
    assert r.ended == 1
    with get_session(engine) as s:
        assert s.scalars(select(Campaign)).one().status == CampaignStatus.ended


def test_partial_provider_does_not_end_missing(engine):
    service.run_scout(FakeProvider([dto("a"), dto("b")]), engine=engine)
    r = service.run_scout(FakeProvider([dto("a")], partial=True), engine=engine)
    assert r.ended == 0
    with get_session(engine) as s:
        assert {c.status for c in s.scalars(select(Campaign))} == {CampaignStatus.discovered}


def test_dry_run_writes_nothing(engine):
    r = service.run_scout(FakeProvider([dto("a"), dto("b")]), dry_run=True, engine=engine)
    assert r.fetched == 2 and r.new == 0
    with get_session(engine) as s:
        assert s.scalars(select(Campaign)).all() == []


def test_auto_falls_back_to_apify(engine):
    direct = FakeProvider([], name="direct", error=ScoutError("layout changed"))
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=APIFY_SAMPLE)))
    auto = service.AutoProvider(direct=direct, apify=ApifyProvider(client=client, token="t"), apify_enabled=True)
    r = service.run_scout(auto, engine=engine)
    assert direct.calls == 1 and r.provider_used == "apify" and r.fetched == 2


def test_auto_without_apify_reraises(engine):
    direct = FakeProvider([], name="direct", error=ScoutError("layout changed"))
    auto = service.AutoProvider(direct=direct, apify=ApifyProvider(token=""), apify_enabled=False)
    with pytest.raises(ScoutError):
        service.run_scout(auto, engine=engine)


def test_auto_migrate_adds_missing_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE campaigns (id INTEGER PRIMARY KEY, external_id VARCHAR(255) UNIQUE, "
            "name VARCHAR(512), url VARCHAR(1024))"
        ))
        conn.execute(text("INSERT INTO campaigns (external_id, name, url) VALUES ('old', 'Old', 'u')"))
    added = auto_migrate(eng)
    assert "campaigns.is_live" in added and "campaigns.payout_type" in added
    cols = {c["name"] for c in inspect(eng).get_columns("campaigns")}
    assert {"is_live", "budget_spent", "funded_at", "progress_pct"} <= cols
    with eng.connect() as conn:
        assert conn.execute(text("SELECT is_live FROM campaigns")).scalar() == 1
    assert auto_migrate(eng) == []
    eng.dispose()
