"""AI call accounting and the monthly budget."""
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, func, select

from app.config import settings
from app.db import get_session
from app.db.models import AiUsage, AppSetting
from app.logger import get_logger
from app.notify import notify

log = get_logger("clipflow.ai")


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Estimated $ cost from configured per-1M-token prices."""
    return (input_tokens * settings.GEMINI_PRICE_IN + output_tokens * settings.GEMINI_PRICE_OUT) / 1_000_000


def record_usage(purpose: str, model: str, input_tokens: int, output_tokens: int, ok: bool,
                 engine: Optional[Engine] = None) -> float:
    """Store one call; returns its cost."""
    cost = cost_usd(input_tokens, output_tokens)
    try:
        with get_session(engine) as s:
            s.add(AiUsage(purpose=purpose, model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                          cost_usd=round(cost, 6), ok=ok))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record ai usage: %s", exc)
    return cost


def month_start(now: Optional[datetime] = None) -> datetime:
    """Start of the current month in TIMEZONE, as UTC."""
    tz = ZoneInfo(settings.TIMEZONE)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    return datetime.combine(local.date().replace(day=1), time.min, tzinfo=tz).astimezone(timezone.utc)


def month_usage(engine: Optional[Engine] = None, now: Optional[datetime] = None) -> dict:
    """This month's calls, tokens and spend."""
    with get_session(engine) as s:
        calls, tin, tout, spend = s.execute(
            select(func.count(), func.coalesce(func.sum(AiUsage.input_tokens), 0),
                   func.coalesce(func.sum(AiUsage.output_tokens), 0), func.coalesce(func.sum(AiUsage.cost_usd), 0.0))
            .where(AiUsage.created_at >= month_start(now))).one()
    return {"calls": calls, "input_tokens": tin, "output_tokens": tout, "spend": float(spend),
            "budget": float(settings.AI_MONTHLY_BUDGET_USD)}


def month_spend(engine: Optional[Engine] = None, now: Optional[datetime] = None) -> float:
    return month_usage(engine, now)["spend"]


def budget_ok(engine: Optional[Engine] = None, now: Optional[datetime] = None) -> bool:
    return month_spend(engine, now) < settings.AI_MONTHLY_BUDGET_USD


def warn_budget_once(engine: Optional[Engine] = None, now: Optional[datetime] = None) -> bool:
    """Notify that the AI budget is used up, at most once per month. Returns True if sent."""
    key = "ai_budget_warned_" + month_start(now).astimezone(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m")
    with get_session(engine) as s:
        if s.scalar(select(AppSetting).where(AppSetting.key == key)):
            return False
        s.add(AppSetting(key=key, value=(now or datetime.now(timezone.utc)).isoformat()))
    try:
        notify("AI budget reached", f"Spent ${month_spend(engine, now):.2f} of ${settings.AI_MONTHLY_BUDGET_USD:g} "
               "this month; moment picking uses the free heuristic until next month.", level="warning")
    except Exception as exc:  # noqa: BLE001
        log.warning("budget notify failed: %s", exc)
    return True
