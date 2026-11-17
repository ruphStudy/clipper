"""Engine, session factory and DB init."""
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Base

SessionLocal = sessionmaker(expire_on_commit=False)
_engine: Optional[Engine] = None


def _set_pragmas(dbapi_conn, _record) -> None:
    """Enable FK enforcement and WAL on every connection."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def get_engine(db_path: Optional[str] = None) -> Engine:
    """Build a SQLite engine for db_path (defaults to settings.DB_PATH)."""
    path = Path(db_path or settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", _set_pragmas)
    return engine


def default_engine() -> Engine:
    """Lazily created app-wide engine."""
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


def init_db(engine: Optional[Engine] = None) -> Engine:
    """Create all tables."""
    from app.db.migrate import auto_migrate

    engine = engine or default_engine()
    Base.metadata.create_all(engine)
    auto_migrate(engine)
    return engine


@contextmanager
def get_session(engine: Optional[Engine] = None) -> Iterator[Session]:
    """Session scope: commit on success, rollback on error, always close."""
    session = SessionLocal(bind=engine or default_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
