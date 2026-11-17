"""Weekly campaign report: Telegram HTML text + CSV attachment."""
import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.config import csv_list, settings
from app.db import get_session
from app.db.enums import CampaignStatus
from app.db.models import AppSetting, Campaign
from app.logger import get_logger
from app.notify import esc, get_notifier
from app.scout.scorer import utc

log = get_logger("clipflow.report")

LAST_REPORT_KEY = "last_report_at"
KEEP_CSV = 8
NEW_LIMIT = 5
CSV_COLUMNS = ["id", "name", "type", "score", "cpm", "est_per_clip", "budget_left", "days_left",
               "creators", "platforms", "requires_application", "url"]


def _money(v: Optional[float], digits: int = 0) -> str:
    return "?" if v is None else f"${v:,.{digits}f}"


def _cpm(c: Campaign) -> str:
    return f"${c.cpm_rate:g}/1K" if c.cpm_rate is not None else (c.payout_type or "?")


def _days(c: Campaign) -> str:
    return f"{c.days_left:g}d" if c.days_left is not None else "?"


def _link(c: Campaign) -> str:
    name = c.name if len(c.name) <= 45 else c.name[:44] + "…"
    return f"<a href='{esc(c.url).replace(chr(39), '&#x27;')}'>{esc(name)}</a>"


def _entry(c: Campaign) -> str:
    lines = [
        f"<b>#{c.id}</b> {_link(c)}",
        f"⭐ {c.score:.1f} · 💵 {_cpm(c)} · ~{_money(c.est_per_clip, 2)}/clip · 💰 {_money(c.budget_left)} left"
        f" · ⏳ {_days(c)} · 👥 {c.creators_count if c.creators_count is not None else '?'}"
        f" · {esc(', '.join(c.platforms or []))}",
    ]
    if c.requires_application:
        lines.append("⚠️ needs application")
    return "\n".join(lines)


def _compact(c: Campaign) -> str:
    return f"<b>#{c.id}</b> {_link(c)} · ⭐ {c.score:.1f} · 💵 {_cpm(c)} · 💰 {_money(c.budget_left)}"


def get_last_report_at(session: Session) -> Optional[datetime]:
    row = session.scalar(select(AppSetting).where(AppSetting.key == LAST_REPORT_KEY))
    if not row or not row.value:
        return None
    try:
        return utc(datetime.fromisoformat(row.value))
    except ValueError:
        return None


def set_last_report_at(session: Session, when: datetime) -> None:
    row = session.scalar(select(AppSetting).where(AppSetting.key == LAST_REPORT_KEY))
    if row is None:
        session.add(AppSetting(key=LAST_REPORT_KEY, value=when.isoformat()))
    else:
        row.value = when.isoformat()


def _write_csv(rows: list[Campaign], reports_dir: Path, now: datetime) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"campaigns_{now.strftime('%Y%m%d')}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for c in rows:
            w.writerow([c.id, c.name, c.campaign_type or "", c.score, c.cpm_rate, c.est_per_clip, c.budget_left,
                        c.days_left, c.creators_count, ",".join(c.platforms or []),
                        bool(c.requires_application), c.url])
    for old in sorted(reports_dir.glob("campaigns_*.csv"))[:-KEEP_CSV]:
        old.unlink()
    return path


def build_report(session: Session, limit: int = 10, now: Optional[datetime] = None,
                 reports_dir: Optional[str] = None) -> tuple[str, Path]:
    """Return (telegram_html, csv_path)."""
    now = utc(now) or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(settings.TIMEZONE))
    types = csv_list(settings.MY_CAMPAIGN_TYPES)
    allc = session.scalars(select(Campaign)).all()
    live = [c for c in allc if c.status != CampaignStatus.ended and c.is_live is not False
            and (not types or (c.campaign_type or "").lower() in types)]
    scored = sorted([c for c in allc if (c.score or 0) > 0 and c.status != CampaignStatus.ended],
                    key=lambda c: c.score, reverse=True)
    last = get_last_report_at(session)

    parts = [
        f"📊 <b>ClipFlow weekly campaigns</b> — {local.strftime('%a %d %b %Y')}",
        f"{len(live)} live clipping campaigns · {len(scored)} scored &gt; 0",
        "",
        f"🏆 <b>Top {limit}</b>",
    ]
    top = scored[:limit]
    parts += [_entry(c) + "\n" for c in top] if top else ["none", ""]

    parts.append("🆕 <b>New since last report</b>")
    new = [c for c in scored if last is None or (utc(c.first_seen_at) and utc(c.first_seen_at) > last)][:NEW_LIMIT]
    parts += [_compact(c) for c in new] if new else ["none"]
    parts.append("")

    parts.append("🎯 <b>Your campaigns</b>")
    mine = [c for c in allc if c.status in (CampaignStatus.joined, CampaignStatus.selected)]
    for c in sorted(mine, key=lambda c: c.id):
        live_flag = "✅ live" if c.is_live is not False else "⚠️ NOT LIVE"
        parts.append(f"<b>#{c.id}</b> {_link(c)} [{c.status.value}] · ⭐ {(c.score or 0):.1f}"
                     f" · ⏳ {_days(c)} · {live_flag}")
    if not mine:
        parts.append("none")
    parts += ["", "Select: <code>campaign-set &lt;id&gt; selected</code>"]

    csv_path = _write_csv(scored, Path(reports_dir) if reports_dir else Path(settings.DATA_DIR) / "reports", local)
    return "\n".join(parts), csv_path


def strip_tags(text: str) -> str:
    """HTML to plain console text."""
    plain = re.sub(r"<[^>]+>", "", text)
    return plain.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&#x27;", "'")


def send_weekly_report(engine: Optional[Engine] = None, notifier=None, now: Optional[datetime] = None,
                       reports_dir: Optional[str] = None, limit: int = 10) -> bool:
    """Build and send the report; only records last_report_at on a successful send."""
    notifier = notifier or get_notifier()
    now = utc(now) or datetime.now(timezone.utc)
    with get_session(engine) as s:
        text, csv_path = build_report(s, limit=limit, now=now, reports_dir=reports_dir)
    if not notifier.enabled:
        print(strip_tags(text))
        print(f"CSV: {csv_path}")
        log.warning("telegram disabled; weekly report printed to console only")
        return False
    ok = notifier.send_message(text) and notifier.send_document(str(csv_path), caption="All scored campaigns (CSV)")
    if ok:
        with get_session(engine) as s:
            set_last_report_at(s, now)
        log.info("weekly report sent")
    else:
        log.error("weekly report send failed; last_report_at unchanged")
    return ok
