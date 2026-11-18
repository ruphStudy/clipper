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
from app.sources.links import is_drive_folder


def _compliance_state(compliance):
    from app.clipper.compliance import compliance_state
    return compliance_state(compliance)

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


def clip_counts(db: Session, source_ids: list[int]) -> dict[int, dict]:
    """Per source id: {'candidate': n, 'rendered': n, 'total': n} (rendered = rendered or later)."""
    from sqlalchemy import func, select

    from app.db.enums import ClipStatus
    from app.db.models import Clip

    if not source_ids:
        return {}
    done = {ClipStatus.rendered, ClipStatus.approved, ClipStatus.scheduled, ClipStatus.posted}
    out: dict[int, dict] = {}
    for sid, status, n in db.execute(select(Clip.source_id, Clip.status, func.count())
                                     .where(Clip.source_id.in_(source_ids)).group_by(Clip.source_id, Clip.status)):
        c = out.setdefault(sid, {"candidate": 0, "rendered": 0, "total": 0})
        c["total"] += n
        if status == ClipStatus.candidate:
            c["candidate"] += n
        if status in done:
            c["rendered"] += n
    return out


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


def _review_waiting(request: Request) -> int:
    """Clips waiting for review (nav badge); 0 if anything goes wrong."""
    try:
        from app.review.service import review_queue

        with get_session(get_engine_for(request)) as s:
            return len(review_queue(s))
    except Exception:  # noqa: BLE001
        return 0


def _submit_badge(request: Request) -> dict:
    """Posts waiting to be submitted (nav badge) and whether any are late."""
    try:
        from app.posting.submit import to_submit

        with get_session(get_engine_for(request)) as s:
            rows = to_submit(s)
        return {"count": len(rows), "late": any(late for _p, _a, late in rows)}
    except Exception:  # noqa: BLE001
        return {"count": 0, "late": False}


def render(request: Request, name: str, **ctx):
    """Render a template with common context."""
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx["no_password"] = getattr(request.state, "no_password", False)
    ctx.setdefault("review_waiting", _review_waiting(request))
    ctx.setdefault("submit_badge", _submit_badge(request))
    return templates.TemplateResponse(request, name, ctx)


def duration(sec: Optional[float]) -> str:
    """Seconds as m:ss (or h:mm:ss)."""
    if sec is None:
        return "–"
    s = int(round(sec))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def filesize(path: Optional[str]) -> str:
    """Human file size for a local path ('–' if missing)."""
    if not path:
        return "–"
    try:
        size = Path(path).stat().st_size
    except OSError:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "–"


templates.env.filters.update(local_time=local_time, money=money, num=num, duration=duration, filesize=filesize)
templates.env.globals.update(is_folder=is_drive_folder, compliance_state=_compliance_state)
