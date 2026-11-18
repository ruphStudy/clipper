"""FastAPI dashboard factory."""
from pathlib import Path
from typing import Iterable, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, text

from app.dashboard.auth import require_auth
from app.dashboard.deps import get_engine_for
from app.dashboard.routes import campaigns, clips, home, posts, review, rules, sources, stats


def create_app(engine: Optional[Engine] = None, extra_local_hosts: Iterable[str] = ()) -> FastAPI:
    """Build the dashboard app; `engine` and `extra_local_hosts` are for tests."""
    app = FastAPI(title="ClipFlow", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.extra_local_hosts = set(extra_local_hosts)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.get("/health")
    def health(request: Request) -> dict:
        try:
            with get_engine_for(request).connect() as conn:
                conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
        return {"ok": True, "db": db_ok}

    auth = [Depends(require_auth)]
    app.include_router(home.router, dependencies=auth)
    app.include_router(campaigns.router, dependencies=auth)
    app.include_router(sources.router, dependencies=auth)
    app.include_router(rules.router, dependencies=auth)
    app.include_router(review.router, dependencies=auth)
    app.include_router(clips.router, dependencies=auth)
    app.include_router(posts.router, dependencies=auth)
    app.include_router(stats.router, dependencies=auth)
    return app
