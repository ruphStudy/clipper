"""Shared dashboard dependencies and helpers."""
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.db.session import default_engine
from app.scout.scorer import utc

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_engine_for(request: Request) -> Engine:
    """Engine bound to the app (tests inject a temp one)."""
    return getattr(request.app.state, "engine", None) or default_engine()


def get_db(request: Request) -> Iterator[Session]:
    """Request-scoped session: commit on success, rollback on error."""
    with get_session(get_engine_for(request)) as s:
        yield s


def local_time(dt: Optional[datetime], fmt: str = "%d %b %Y %H:%M") -> str:
    """Format a UTC datetime in settings.TIMEZONE."""
    if dt is None:
        return "–"
    return utc(dt).astimezone(ZoneInfo(settings.TIMEZONE)).strftime(fmt)


def money(v: Optional[float], digits: int = 0) -> str:
    return "–" if v is None else f"${v:,.{digits}f}"


def num(v: Optional[float]) -> str:
    return "–" if v is None else f"{v:g}"


def redirect(path: str, msg: Optional[str] = None) -> RedirectResponse:
    """303 redirect (PRG) with an optional flash message."""
    if msg:
        path += ("&" if "?" in path else "?") + urlencode({"msg": msg})
    return RedirectResponse(path, status_code=303)


def safe_back(back: Optional[str], default: str) -> str:
    """Only allow local relative redirect targets."""
    if back and back.startswith("/") and not back.startswith("//"):
        return back
    return default


def render(request: Request, name: str, **ctx):
    """Render a template with common context."""
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx["no_password"] = getattr(request.state, "no_password", False)
    return templates.TemplateResponse(request, name, ctx)


templates.env.filters.update(local_time=local_time, money=money, num=num)
