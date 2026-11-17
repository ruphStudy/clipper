"""Rule-based scorer tests (offline, temp DB)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus
from app.db.models import Campaign, CampaignSnapshot
from app.scout.scorer import ScoreConfig, burn_rate, est_per_clip, score_all, score_campaign
from app.scout.service import record_snapshots

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
CFG = ScoreConfig(platforms=["youtube", "instagram"], niches=[], typical_views=3000,
                  min_cpm=0.3, min_budget_left=500, min_days_left=3)


def camp(**kw):
    base = dict(external_id="e1", name="Podcast Clips", url="u", brand="PodCo", payout_type="cpm",
                campaign_type="clipping",
                cpm_rate=2.0, budget_total=20000.0, budget_spent=10001.0, budget_left=9999.0,
                creators_count=40, platforms=["youtube", "tiktok"], is_verified=True,
                description="Clip the show", requires_application=True, is_live=True,
                status=CampaignStatus.discovered, funded_at=NOW - timedelta(days=30), last_seen_at=NOW)
    base.update(kw)
    return Campaign(**base)


def snap(spent, at, left=None):
    return CampaignSnapshot(budget_spent=spent, budget_left=left, captured_at=at)


@pytest.fixture
def engine(tmp_path):
    eng = init_db(get_engine(str(tmp_path / "score.db")))
    yield eng
    eng.dispose()


def test_burn_rate_from_snapshots_24h_apart():
    c = camp(budget_spent=2000.0, budget_left=8000.0, last_seen_at=NOW)
    burn, days = burn_rate(c, [snap(1000.0, NOW - timedelta(hours=24)), snap(2000.0, NOW)], NOW)
    assert burn == 1000.0 and days == 8.0


def test_burn_rate_none_when_under_12h():
    c = camp(budget_spent=2000.0, last_seen_at=NOW)
    assert burn_rate(c, [snap(1000.0, NOW - timedelta(hours=6)), snap(2000.0, NOW)], NOW) == (None, None)


def test_burn_rate_ignores_snapshots_older_than_7_days():
    c = camp(budget_spent=2000.0, last_seen_at=NOW)
    assert burn_rate(c, [snap(0.0, NOW - timedelta(days=8))], NOW) == (None, None)


def test_est_per_clip_min_payout_and_cap():
    assert est_per_clip(camp(cpm_rate=2.0), CFG) == 6.0
    assert est_per_clip(camp(cpm_rate=2.0, min_payout=10.0), CFG) == 0.0
    assert est_per_clip(camp(cpm_rate=2.0, max_payout_per_clip=4.0), CFG) == 4.0
    assert est_per_clip(camp(cpm_rate=None), CFG) == 0.0


@pytest.mark.parametrize("overrides,snaps,reason", [
    ({"is_live": False}, [], "ended / not live"),
    ({"status": CampaignStatus.ended}, [], "ended / not live"),
    ({"platforms": ["tiktok", "x"]}, [], "not on your platforms"),
    ({"budget_left": 100.0}, [], "low budget left ($100)"),
    ({"cpm_rate": 0.1}, [], "CPM below minimum ($0.1)"),
    ({"budget_spent": 3000.0, "budget_left": 1500.0}, [(1000.0, 24)], "budget almost gone: ~0.8 days left"),
])
def test_hard_filters(overrides, snaps, reason):
    c = camp(**overrides)
    s = [snap(spent, NOW - timedelta(hours=h)) for spent, h in snaps]
    r = score_campaign(c, s, CFG, NOW)
    assert r.score == 0 and reason in r.reasons and r.breakdown == {}


def test_ugc_filtered_clipping_scored():
    ugc = score_campaign(camp(campaign_type="ugc"), [], CFG, NOW)
    assert ugc.score == 0 and "not a clipping campaign" in ugc.reasons
    assert score_campaign(camp(campaign_type="clipping"), [], CFG, NOW).score > 0


def test_pool_campaign_not_filtered_by_cpm():
    r = score_campaign(camp(payout_type="pool", cpm_rate=None), [], CFG, NOW)
    assert r.score > 0


def test_exact_breakdown():
    r = score_campaign(camp(), [], CFG, NOW)
    assert r.breakdown == {
        "earnings": 18.0, "budget": 12.77, "runway": 5.0, "competition": 7.5,
        "trust": 10, "platform": 5.0, "niche": 5.0, "bonus": -5,
    }
    assert r.score == 58.3
    assert r.est_per_clip == 6.0
    assert "high $/clip ($6.00)" in r.reasons and "needs application" in r.reasons
    assert len(r.reasons) <= 4


def test_fresh_funding_bonus():
    r = score_campaign(camp(funded_at=NOW - timedelta(days=3), requires_application=False), [], CFG, NOW)
    assert r.breakdown["bonus"] == 5


def test_niche_component():
    assert score_campaign(camp(), [], CFG, NOW).breakdown["niche"] == 5.0
    cfg = ScoreConfig(platforms=CFG.platforms, niches=["podcast", "gaming"], typical_views=3000,
                      min_cpm=0.3, min_budget_left=500, min_days_left=3)
    assert score_campaign(camp(), [], cfg, NOW).breakdown["niche"] == 10.0
    assert score_campaign(camp(name="Car Clips", brand="Cars", description="cars"), [], cfg, NOW).breakdown["niche"] == 0.0


def test_score_all_keeps_user_fields(engine):
    with get_session(engine) as s:
        s.add(camp(status=CampaignStatus.joined, notes="keep me"))
        s.add(camp(external_id="e2", status=CampaignStatus.ended))
    with get_session(engine) as s:
        assert score_all(s, CFG, NOW) == 1
    with get_session(engine) as s:
        rows = {c.external_id: c for c in s.scalars(select(Campaign))}
    j = rows["e1"]
    assert j.status == CampaignStatus.joined and j.notes == "keep me"
    assert j.score == 58.3 and j.scored_at is not None and j.score_breakdown["earnings"] == 18.0
    assert rows["e2"].scored_at is None


def test_snapshot_throttle_6h(engine):
    with get_session(engine) as s:
        s.add(camp())
    with get_session(engine) as s:
        rows = s.scalars(select(Campaign)).all()
        assert record_snapshots(s, rows, NOW) == 1
        assert record_snapshots(s, rows, NOW + timedelta(hours=5)) == 0
        assert record_snapshots(s, rows, NOW + timedelta(hours=7)) == 1
        s.add(CampaignSnapshot(campaign_id=rows[0].id, budget_spent=1.0, captured_at=NOW - timedelta(days=61)))
    with get_session(engine) as s:
        rows = s.scalars(select(Campaign)).all()
        record_snapshots(s, rows, NOW + timedelta(hours=8))
    with get_session(engine) as s:
        assert s.scalar(select(func.count()).select_from(CampaignSnapshot)) == 2
