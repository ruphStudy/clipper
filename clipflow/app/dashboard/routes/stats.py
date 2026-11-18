"""Performance stats page, CSV export, actual payouts."""
import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.dashboard.deps import get_db, local_time, redirect, render
from app.db.models import Campaign, CampaignEarning
from app.tracker.analytics import compute

router = APIRouter()
PERIODS = (7, 30, 90)
CSV_COLUMNS = ["post_id", "platform", "account", "campaign", "posted_at", "views", "likes", "est_usd", "hour",
               "layout", "method", "hook_len", "url"]


def _days(days: int) -> int:
    return days if days in PERIODS else 30


@router.get("/stats")
def stats_page(request: Request, days: int = 30, db: Session = Depends(get_db)):
    days = _days(days)
    data = compute(db, days)
    tables = [("Campaigns", "campaign", data["by_campaign"]), ("Platforms", "platform", data["by_platform"]),
              ("Accounts", "account", data["by_account"]), ("Posting hour", "hour", data["by_slot_hour"]),
              ("Layout", "layout", data["by_layout"]), ("Moment method", "method", data["by_method"]),
              ("Hook length", "hook_len", data["by_hook_len"])]
    return render(request, "stats.html", d=data, days=days, periods=PERIODS, tables=tables, nav="stats")


@router.get("/stats.csv")
def stats_csv(days: int = 30, db: Session = Depends(get_db)):
    days = _days(days)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in compute(db, days)["rows"]:
        w.writerow([r["id"], r["platform"], r["account"], r["campaign"], local_time(r["posted_at"], "%Y-%m-%d %H:%M"),
                    r["views"], r["likes"] if r["likes"] is not None else "", f"{r['est']:.2f}", r["hour"],
                    r["layout"], r["method"], r["hook"], r["url"] or ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="clipflow_posts_{days}d.csv"'})


@router.post("/campaigns/{campaign_id}/earnings")
def add_earnings(campaign_id: int, amount: str = Form(...), period_end: str = Form(""), note: str = Form(""),
                 db: Session = Depends(get_db)):
    camp = db.get(Campaign, campaign_id)
    if camp is None:
        raise HTTPException(404, "Campaign not found")
    try:
        value = float(amount)
        if value < 0:
            raise ValueError
    except ValueError:
        return redirect(f"/campaigns/{campaign_id}#performance", "Amount must be a positive number")
    try:
        period = date.fromisoformat(period_end) if period_end else date.today()
    except ValueError:
        return redirect(f"/campaigns/{campaign_id}#performance", "Date must be YYYY-MM-DD")
    db.add(CampaignEarning(campaign_id=camp.id, amount_usd=value, period_end=period, note=note.strip()[:255] or None))
    return redirect(f"/campaigns/{campaign_id}#performance", f"Recorded ${value:.2f} payout")
