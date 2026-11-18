"""Performance tracking tests: mocked YouTube client + httpx MockTransport, temp DB, injected now."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.dashboard.app import create_app
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, Platform, PostStatus, SourceKind, SourceStatus
from app.db.models import Account, Campaign, CampaignEarning, Clip, Post, PostStat, Source
from app.scheduler.jobs import build_scheduler
from app.scout.scorer import effective_typical_views, score_all
from app.tracker import analytics, fetchers, service
from app.tracker.service import due_posts, estimate_earnings, learn_typical_views, set_manual_views, track_due
from app.uploader import instagram_auth, youtube_auth

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
V = "v26.0"


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.messages = []

    def send_message(self, text, **_):
        self.messages.append(text)
        return True


@pytest.fixture
def engine(tmp_path, monkeypatch):
    for key, val in {"TIMEZONE": "Asia/Kolkata", "TRACK_DAYS": 30, "YOUTUBE_API_KEY": "", "AUTO_TYPICAL_VIEWS": True,
                     "VIEW_MILESTONES": "10000,50000,100000", "MY_PLATFORMS": "youtube,instagram",
                     "TYPICAL_VIEWS": 3000, "META_GRAPH_VERSION": V, "SECRETS_DIR": str(tmp_path)}.items():
        monkeypatch.setattr(config.settings, key, val)
    fetchers._ig_views_metric = None
    eng = init_db(get_engine(str(tmp_path / "t.db")))
    with get_session(eng) as s:
        s.add(Campaign(id=1, external_id="a", name="Alpha", url="u", status=CampaignStatus.joined, payout_type="cpm",
                       cpm_rate=2.0, min_payout=1.0, max_payout_per_clip=50.0, campaign_type="clipping",
                       platforms=["youtube"], budget_left=5000, creators_count=10))
        s.add(Campaign(id=2, external_id="b", name="Beta", url="u", status=CampaignStatus.joined, payout_type="pool",
                       campaign_type="clipping", platforms=["youtube"], budget_left=5000))
        for sid, cid, method in ((1, 1, "ai"), (2, 2, "heuristic")):
            s.add(Source(id=sid, campaign_id=cid, url=f"https://a.test/{sid}.mp4", kind=SourceKind.direct,
                         status=SourceStatus.processed, moments_method=method))
        s.add(Account(id=1, platform=Platform.youtube, handle="@yt", credentials_ref="youtube_x.json"))
        s.add(Account(id=2, platform=Platform.youtube, handle="@manual"))
        s.add(Account(id=3, platform=Platform.instagram, handle="ig", credentials_ref="instagram_x.json"))
        s.add(Account(id=4, platform=Platform.tiktok, handle="@tt"))
    yield eng
    eng.dispose()


def add_post(engine, *, age=timedelta(hours=1), platform=Platform.youtube, account=1, campaign=1, views=None,
             vid="v1", checked=None, status=PostStatus.posted, layout="pad", hook="Short hook", url=None) -> int:
    with get_session(engine) as s:
        clip = Clip(campaign_id=campaign, source_id=campaign, start_sec=0, end_sec=20, hook_title=hook, hashtags=[],
                    status=ClipStatus.posted, layout=layout)
        s.add(clip)
        s.flush()
        p = Post(clip_id=clip.id, account_id=account, platform=platform, status=status, posted_at=NOW - age,
                 platform_post_id=vid, views=views, last_checked_at=checked, post_url=url)
        s.add(p)
        s.flush()
        return p.id


def get(engine, pid) -> Post:
    with get_session(engine) as s:
        return s.get(Post, pid)


# --- cadence -----------------------------------------------------------------------------

def test_due_cadence(engine):
    young_due = add_post(engine, age=timedelta(hours=10), checked=NOW - timedelta(hours=3))
    young_fresh = add_post(engine, age=timedelta(hours=10), checked=NOW - timedelta(hours=1))
    mid_due = add_post(engine, age=timedelta(days=3), checked=NOW - timedelta(hours=13))
    mid_fresh = add_post(engine, age=timedelta(days=3), checked=NOW - timedelta(hours=6))
    old_due = add_post(engine, age=timedelta(days=10), checked=NOW - timedelta(hours=25))
    old_fresh = add_post(engine, age=timedelta(days=10), checked=NOW - timedelta(hours=20))
    expired = add_post(engine, age=timedelta(days=31))
    never = add_post(engine, age=timedelta(minutes=30))
    scheduled = add_post(engine, status=PostStatus.scheduled)
    with get_session(engine) as s:
        ids = {p.id for p in due_posts(s, NOW)}
    assert ids == {young_due, mid_due, old_due, never}
    assert not ids & {young_fresh, mid_fresh, old_fresh, expired, scheduled}


# --- youtube -------------------------------------------------------------------------------

class FakeYT:
    def __init__(self, missing=()):
        self.missing, self.batches = set(missing), []

    def videos(self):
        outer = self

        class V_:
            def list(self, part, id, maxResults):
                ids = id.split(",")
                outer.batches.append(len(ids))

                class R:
                    def execute(self_inner):
                        return {"items": [{"id": i, "statistics": {"viewCount": "1500", "likeCount": "40",
                                                                  "commentCount": "3"}}
                                          for i in ids if i not in outer.missing]}
                return R()
        return V_()


def test_youtube_batching_missing_and_key_fallback(engine, monkeypatch):
    monkeypatch.setattr(youtube_auth, "get_credentials", lambda acct, engine=None: "creds")
    oauth = [add_post(engine, vid=f"v{i}") for i in range(120)]
    gone = add_post(engine, vid="gone")
    manual = add_post(engine, account=2, vid="pub1")
    fake = FakeYT(missing={"gone"})
    with get_session(engine) as s:
        posts = s.scalars(select(Post)).all()
        s.expunge_all()
    out = fetchers.youtube_stats(posts, engine, service_factory=lambda creds: fake)
    assert fake.batches == [50, 50, 21] and out[oauth[0]] == {"views": 1500, "likes": 40, "comments": 3}
    assert out[gone] == {"error": "removed/private"} and out[manual] == {"error": "no youtube credentials"}
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"items": [{"id": "pub1", "statistics": {"viewCount": "777"}}]})

    monkeypatch.setattr(config.settings, "YOUTUBE_API_KEY", "AIza-KEY-SECRET")
    manual_post = [p for p in posts if p.id == manual]
    out = fetchers.youtube_stats(manual_post, engine, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out[manual]["views"] == 777 and seen[0].url.params["id"] == "pub1"


# --- instagram ------------------------------------------------------------------------------

def test_instagram_metric_fallback_and_permalink(engine, monkeypatch):
    monkeypatch.setattr(instagram_auth, "load_token", lambda acct: ("IG1", "PAGE-TOKEN"))
    known = add_post(engine, platform=Platform.instagram, account=3, vid="M1")
    manual = add_post(engine, platform=Platform.instagram, account=3, vid=None,
                      url="https://www.instagram.com/reel/ABC/")
    metrics_seen = []

    def handler(req):
        path = req.url.path
        if path == f"/{V}/IG1/media":
            return httpx.Response(200, json={"data": [{"id": "M5", "permalink": "https://www.instagram.com/reel/ABC/?x=1"}]})
        if path.endswith("/insights"):
            metric = req.url.params["metric"].split(",")[0]
            metrics_seen.append(metric)
            if metric == "views":
                return httpx.Response(400, json={"error": {"message": "metric[0] must be one of …", "code": 100}})
            return httpx.Response(200, json={"data": [{"name": metric, "values": [{"value": 1234}]},
                                                      {"name": "likes", "total_value": {"value": 10}},
                                                      {"name": "comments", "values": [{"value": 2}]}]})
        return httpx.Response(404, json={"error": {"message": "nope", "code": 803}})

    with get_session(engine) as s:
        posts = s.scalars(select(Post).order_by(Post.id)).all()
        s.expunge_all()
    out = fetchers.instagram_stats(posts, engine, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out[known] == {"views": 1234, "likes": 10, "comments": 2}
    assert out[manual] == {"views": 1234, "likes": 10, "comments": 2, "platform_post_id": "M5"}
    assert metrics_seen == ["views", "plays", "plays"] and fetchers._ig_views_metric == "plays"
    track_due(NOW, engine, fetchers={"instagram": lambda posts, engine=None: out}, notifier=FakeNotifier())
    assert get(engine, manual).platform_post_id == "M5"


# --- earnings, milestones, manual views -------------------------------------------------------

def test_estimate_earnings():
    camp = Campaign(payout_type="cpm", cpm_rate=2.0, min_payout=1.0, max_payout_per_clip=50.0)
    assert estimate_earnings(Post(views=400), camp) == 0.0  # $0.80 < min $1
    assert estimate_earnings(Post(views=10_000), camp) == 20.0
    assert estimate_earnings(Post(views=100_000), camp) == 50.0  # capped
    assert estimate_earnings(Post(views=100_000), Campaign(payout_type="pool", cpm_rate=2.0)) == 0.0


def test_track_due_milestones_and_stat_rows(engine):
    pid = add_post(engine)
    n = FakeNotifier()
    seq = iter([12_000, 12_000, 60_000])
    fetch = {"youtube": lambda posts, engine=None: {p.id: {"views": next(seq), "likes": 5, "comments": 1}
                                                     for p in posts}}
    assert track_due(NOW, engine, fetchers=fetch, notifier=n) == {"checked": 1, "updated": 1, "errors": 0}
    assert n.messages == [f"🔥 #{pid} hit 10K views on youtube — Alpha"]
    p = get(engine, pid)
    assert (p.views, p.likes, p.earnings_est, p.milestone_hit) == (12_000, 5, 24.0, 10_000)
    track_due(NOW + timedelta(hours=2), engine, fetchers=fetch, notifier=n)  # same views: no new stats row
    track_due(NOW + timedelta(hours=4), engine, fetchers=fetch, notifier=n)
    assert n.messages[-1] == f"🔥 #{pid} hit 50K views on youtube — Alpha" and len(n.messages) == 2
    with get_session(engine) as s:
        assert [r.views for r in s.scalars(select(PostStat).order_by(PostStat.id))] == [12_000, 60_000]
    err = add_post(engine, vid="x")
    track_due(NOW + timedelta(hours=6), engine, fetchers={"youtube": lambda posts, engine=None: {
        p.id: {"error": "removed/private"} for p in posts}}, notifier=n)
    assert get(engine, err).stats_error == "removed/private"


def test_set_manual_views_tiktok(engine):
    pid = add_post(engine, platform=Platform.tiktok, account=4, vid=None)
    n = FakeNotifier()
    ok, msg = set_manual_views(pid, 15_000, engine, NOW, n)
    assert ok and get(engine, pid).views == 15_000 and get(engine, pid).earnings_est == 30.0
    assert n.messages and "10K" in n.messages[0]
    assert set_manual_views(pid, -1, engine, NOW, n)[0] is False


# --- learning ---------------------------------------------------------------------------------

def test_learn_typical_views_and_scorer(engine):
    for i in range(9):
        add_post(engine, age=timedelta(days=5), views=1000 * (i + 1))
    assert learn_typical_views(engine, NOW) == {}
    with get_session(engine) as s:
        assert effective_typical_views(s) == (3000.0, "config")
    add_post(engine, age=timedelta(days=5), views=10_000)
    add_post(engine, age=timedelta(days=1), views=999_999)  # too young: ignored
    assert learn_typical_views(engine, NOW) == {"youtube": 5500}
    with get_session(engine) as s:
        assert effective_typical_views(s) == (5500, "learned")
        score_all(s, now=NOW)
        assert s.get(Campaign, 1).est_per_clip == 11.0  # 5500/1000 * $2
    config.settings.AUTO_TYPICAL_VIEWS = False
    try:
        with get_session(engine) as s:
            assert effective_typical_views(s) == (3000.0, "config")
    finally:
        config.settings.AUTO_TYPICAL_VIEWS = True


# --- analytics -------------------------------------------------------------------------------------

def test_analytics(engine):
    a1 = add_post(engine, campaign=1, views=10_000, age=timedelta(days=5), layout="crop", hook="x" * 20)
    add_post(engine, campaign=1, views=300, age=timedelta(days=4), layout="pad", hook="y" * 40)
    add_post(engine, campaign=2, views=2_000, age=timedelta(days=1), hook="z" * 50)
    with get_session(engine) as s:
        for p in s.scalars(select(Post)):
            clip = s.get(Clip, p.clip_id)
            p.earnings_est = estimate_earnings(p, s.get(Campaign, clip.campaign_id))
            p.posted_at = p.posted_at.replace(hour=4, minute=30)  # 10:00 in Asia/Kolkata
        s.add(CampaignEarning(campaign_id=1, amount_usd=15.0, period_end=date(2026, 11, 15)))
    with get_session(engine) as s:
        d = analytics.compute(s, 30, NOW)
    assert d["totals"] == {"posts": 3, "views": 12_300, "est": 20.0, "actual": 15.0}
    alpha = next(r for r in d["by_campaign"] if r["campaign"] == "Alpha")
    assert (alpha["posts"], alpha["avg_views"], alpha["est"], alpha["est_per_post"], alpha["hit_rate"], alpha["actual"]) \
        == (2, 5150, 20.0, 10.0, 50.0, 15.0)
    assert d["by_slot_hour"] == [{"hour": 10, "posts": 3, "views": 12_300, "avg_views": 4100, "est": 20.0,
                                  "est_per_post": 6.67, "hit_rate": 33.3}]
    assert d["flop_rate"] == 50.0  # 2 posts ≥3 days old, one under 500 views
    assert d["est_vs_actual"] == [{"campaign": "Alpha", "est": 20.0, "actual": 15.0, "ratio": 0.75}]
    assert d["top_posts"][0]["id"] == a1 and {r["layout"] for r in d["by_layout"]} == {"crop", "pad"}
    assert {r["method"] for r in d["by_method"]} == {"ai", "heuristic"}
    assert [r["hook_len"] for r in d["by_hook_len"]] == ["≤30", "31–45", "46+"]


# --- dashboard, report, scheduler -----------------------------------------------------------------

def test_stats_pages_and_forms(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    client = TestClient(create_app(engine))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    add_post(engine, views=4_000, age=timedelta(days=2))
    tiktok = add_post(engine, platform=Platform.tiktok, account=4, vid=None)
    page = client.get("/stats", params={"days": 7}).text
    assert "Export CSV" in page and "Alpha" in page and 'class="bar"' in page and "Posting hour" in page
    csv_resp = client.get("/stats.csv", params={"days": 7})
    assert csv_resp.status_code == 200 and csv_resp.text.splitlines()[0].startswith("post_id,platform")
    assert len(csv_resp.text.strip().splitlines()) == 3
    posted = client.get("/posts", params={"tab": "posted"}).text
    assert f'/posts/{tiktok}/views' in posted
    r = client.post(f"/posts/{tiktok}/views", data={"views": "2,500"}, follow_redirects=False)
    assert r.status_code == 303 and get(engine, tiktok).views == 2500
    r = client.post("/campaigns/1/earnings", data={"amount": "12.5", "period_end": "2026-11-10", "note": "Nov"},
                    follow_redirects=False)
    assert r.status_code == 303
    detail = client.get("/campaigns/1").text
    assert "Add actual payout" in detail and "actual $12.50" in detail
    assert "Views (7d)" in client.get("/").text


def test_weekly_report_section(engine, tmp_path):
    from app.scout.report import build_report

    add_post(engine, views=8_000, age=timedelta(days=2))
    with get_session(engine) as s:
        text, _ = build_report(s, now=NOW, reports_dir=str(tmp_path / "r"))
    section = text.split("📈")[1].split("🎯")[0]
    assert "Last 7 days" in section and "8,000 views" in section and "Best campaign: Alpha" in section
    assert len([ln for ln in ("📈" + section).strip().splitlines() if ln.strip()]) <= 8


def test_scheduler_has_fifteen_jobs():
    ids = {j.id for j in build_scheduler().get_jobs()}
    assert len(ids) == 15 and {"track_stats", "learn"} <= ids
