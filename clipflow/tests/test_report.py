"""Weekly report, campaign CLI and scheduler tests (offline)."""
import csv
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import config, main
from app.db import get_engine, get_session, init_db
from app.db import session as db_session
from app.db.enums import CampaignStatus
from app.db.models import AppSetting, Campaign
from app.scheduler.jobs import build_scheduler, next_runs
from app.scout.report import CSV_COLUMNS, LAST_REPORT_KEY, build_report, send_weekly_report

NOW = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self, enabled=True, ok=True):
        self.enabled, self.ok = enabled, ok
        self.messages, self.documents = [], []

    def send_message(self, text, **_):
        self.messages.append(text)
        return self.ok

    def send_document(self, path, caption=""):
        self.documents.append(path)
        return self.ok


def camp(ext, score, **kw):
    base = dict(external_id=ext, name=f"Camp {ext}", url=f"https://cr.test/c/{ext}", campaign_type="clipping",
                payout_type="cpm", cpm_rate=1.5, est_per_clip=4.5, budget_left=5000.0, creators_count=20,
                platforms=["youtube"], score=score, status=CampaignStatus.discovered, is_live=True,
                first_seen_at=NOW - timedelta(days=30))
    base.update(kw)
    return Campaign(**base)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    eng = init_db(get_engine(str(tmp_path / "rep.db")))
    monkeypatch.setattr(db_session, "_engine", eng)
    for key in ("DATA_DIR", "DOWNLOAD_DIR", "CLIPS_DIR", "LOG_DIR"):
        monkeypatch.setattr(config.settings, key, str(tmp_path / "data" / key.lower()))
    yield eng
    eng.dispose()


def seed(engine):
    with get_session(engine) as s:
        s.add_all([
            camp("low", 40.0),
            camp("high", 90.0, name="<b>Evil</b> & Co", requires_application=True),
            camp("mid", 70.0, first_seen_at=NOW - timedelta(days=1)),
            camp("zero", 0.0),
            camp("mine", 55.0, status=CampaignStatus.joined, is_live=False, days_left=4.0),
        ])


def test_campaign_set_and_bad_id(engine, capsys):
    seed(engine)
    with get_session(engine) as s:
        cid = s.scalar(select(Campaign.id).where(Campaign.external_id == "low"))
    assert main.main(["campaign-set", str(cid), "selected", "--notes", "try this"]) == 0
    assert f"#{cid} Camp low → selected" in capsys.readouterr().out
    with get_session(engine) as s:
        c = s.get(Campaign, cid)
        assert c.status == CampaignStatus.selected and c.notes == "try this"
    assert main.main(["campaign-set", "99999", "joined"]) == 1
    assert main.main(["campaign-set", str(cid), "bogus"]) == 1
    assert main.main(["campaign-list", "--status", "selected"]) == 0
    assert "Camp low" in capsys.readouterr().out


def test_build_report(engine, tmp_path):
    seed(engine)
    with get_session(engine) as s:
        s.add(AppSetting(key=LAST_REPORT_KEY, value=(NOW - timedelta(days=7)).isoformat()))
    with get_session(engine) as s:
        text, csv_path = build_report(s, limit=10, now=NOW, reports_dir=str(tmp_path / "reports"))
    top = text.split("🆕")[0]
    assert top.index("&lt;b&gt;Evil&lt;/b&gt; &amp; Co") < top.index("Camp mid") < top.index("Camp mine") \
        < top.index("Camp low")
    assert "<b>Evil</b>" not in text
    assert "Camp zero" not in text
    assert "⚠️ needs application" in top
    new_section = text.split("🆕")[1].split("🎯")[0]
    assert "Camp mid" in new_section and "Camp low" not in new_section and "Evil" not in new_section
    mine = text.split("🎯")[1]
    assert "Camp mine" in mine and "NOT LIVE" in mine
    assert "Select: <code>campaign-set &lt;id&gt; selected</code>" in text
    rows = list(csv.reader(csv_path.open(encoding="utf-8")))
    assert rows[0] == CSV_COLUMNS
    assert [r[1] for r in rows[1:]] == ["<b>Evil</b> & Co", "Camp mid", "Camp mine", "Camp low"]
    assert csv_path.name == "campaigns_20261116.csv"


def _last(engine):
    with get_session(engine) as s:
        row = s.scalar(select(AppSetting).where(AppSetting.key == LAST_REPORT_KEY))
        return row.value if row else None


def test_last_report_only_on_success(engine, tmp_path, capsys):
    seed(engine)
    rd = str(tmp_path / "reports")
    assert send_weekly_report(engine, FakeNotifier(ok=False), NOW, rd) is False
    assert _last(engine) is None
    assert send_weekly_report(engine, FakeNotifier(enabled=False), NOW, rd) is False
    assert "ClipFlow weekly campaigns" in capsys.readouterr().out
    assert _last(engine) is None
    n = FakeNotifier()
    assert send_weekly_report(engine, n, NOW, rd) is True
    assert len(n.messages) == 1 and n.documents and n.documents[0].endswith(".csv")
    assert _last(engine) == NOW.isoformat()


def test_scheduler_triggers():
    sched = build_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}
    assert {"daily_scout_am", "daily_scout_pm", "weekly_report"} <= set(jobs)

    def fields(job):
        return {f.name: str(f) for f in job.trigger.fields if not f.is_default}

    assert fields(jobs["daily_scout_am"]) == {"hour": "8", "minute": "40"}
    assert fields(jobs["daily_scout_pm"]) == {"hour": "20", "minute": "40"}
    assert fields(jobs["weekly_report"]) == {"day_of_week": "mon", "hour": "9", "minute": "10"}
    for j in jobs.values():
        assert j.coalesce is True and j.max_instances == 1 and j.misfire_grace_time == 3600
    runs = dict(next_runs(sched, datetime(2026, 11, 17, 9, 0, tzinfo=jobs["weekly_report"].trigger.timezone)))
    assert (runs["daily_scout_am"].day, runs["daily_scout_am"].hour) == (18, 8)
    assert (runs["daily_scout_pm"].day, runs["daily_scout_pm"].hour) == (17, 20)
    assert (runs["weekly_report"].weekday(), runs["weekly_report"].hour, runs["weekly_report"].minute) == (0, 9, 10)
