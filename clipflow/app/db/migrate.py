"""Additive auto-migration for SQLite: ADD COLUMN, relax NOT NULL (table rebuild), one-time data fixes."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, MetaData, Table, inspect, text

from app.db.models import Base
from app.logger import get_logger

log = get_logger("clipflow.migrate")

FIX_CLIP_LEN_FLAG = "fix_clip_len_v1"


def _add_missing_columns(engine: Engine) -> list[str]:
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'
                if col.server_default is not None:
                    default = col.server_default.arg
                    default_sql = default.compile(dialect=engine.dialect) if hasattr(default, "compile") else default
                    ddl += f" DEFAULT {default_sql}"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
                log.info("migrated: added column %s.%s", table.name, col.name)
    return added


def _relax_not_null(engine: Engine) -> list[str]:
    """Rebuild tables whose live NOT NULL columns are nullable in the models (SQLite can't ALTER this)."""
    insp = inspect(engine)
    live_tables = set(insp.get_table_names())
    rebuilt: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in live_tables:
            continue
        live_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        relax = [c.name for c in table.columns
                 if c.nullable and not c.primary_key and c.name in live_cols and not live_cols[c.name]["nullable"]]
        if not relax:
            continue
        _rebuild(engine, table, live_cols)
        rebuilt.append(f"{table.name}({', '.join(relax)})")
        log.info("migrated: rebuilt %s to allow NULL in %s", table.name, ", ".join(relax))
    return rebuilt


def backup_db(engine: Engine) -> Optional[Path]:
    """Copy the SQLite file to <db dir>/backups/ (consistent copy via the backup API)."""
    db_path = engine.url.database
    if not db_path or db_path == ":memory:":
        return None
    src = Path(db_path)
    dest_dir = src.parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
    with sqlite3.connect(src) as live, sqlite3.connect(dest) as out:
        live.backup(out)
    log.info("database backed up to %s", dest)
    return dest


def _rebuild(engine: Engine, table: Table, live_cols: dict[str, dict]) -> None:
    backup_db(engine)
    tmp_name = f"{table.name}__new"
    tmp_md = MetaData()
    tmp = table.to_metadata(tmp_md, name=tmp_name)
    for col in tmp.columns:  # never stricter than the live table, so the copy cannot fail
        if col.name in live_cols and live_cols[col.name]["nullable"] and not col.primary_key:
            col.nullable = True
    for idx in list(tmp.indexes):  # index names are global in SQLite; recreate after the swap
        tmp.indexes.discard(idx)
    cols = ", ".join(f'"{c}"' for c in live_cols if c in table.columns)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()  # PRAGMA must run outside a transaction; end SQLAlchemy's autobegin
        try:
            with conn.begin():
                tmp.create(conn)
                conn.exec_driver_sql(f'INSERT INTO "{tmp_name}" ({cols}) SELECT {cols} FROM "{table.name}"')
                conn.exec_driver_sql(f'DROP TABLE "{table.name}"')
                conn.exec_driver_sql(f'ALTER TABLE "{tmp_name}" RENAME TO "{table.name}"')
                for idx in table.indexes:
                    idx.create(conn)
            problems = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            conn.commit()
            if problems:
                log.warning("foreign_key_check after rebuilding %s: %s", table.name, problems[:5])
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()


def _fix_clip_len_v1(engine: Engine) -> int:
    """Once: unconfirmed campaigns with the old 15/60 defaults get NULL (= follow default rules)."""
    insp = inspect(engine)
    if "app_settings" not in insp.get_table_names() or "campaigns" not in insp.get_table_names():
        return 0
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM app_settings WHERE key = :k"), {"k": FIX_CLIP_LEN_FLAG}).first():
            return 0
        n = conn.execute(text(
            "UPDATE campaigns SET clip_min_sec = NULL, clip_max_sec = NULL "
            "WHERE rules_confirmed_at IS NULL AND clip_min_sec = 15 AND clip_max_sec = 60"
        )).rowcount
        now = datetime.now(timezone.utc).isoformat(sep=" ")
        conn.execute(text("INSERT INTO app_settings (key, value, created_at, updated_at) VALUES (:k, :v, :t, :t)"),
                     {"k": FIX_CLIP_LEN_FLAG, "v": now, "t": now})
    if n:
        log.info("migrated: %s cleared default clip length on %d campaigns", FIX_CLIP_LEN_FLAG, n)
    return n


def auto_migrate(engine: Engine) -> list[str]:
    """Bring the live SQLite schema up to the models (never drops data); returns changes made."""
    changes = _add_missing_columns(engine)
    changes += _relax_not_null(engine)
    if _fix_clip_len_v1(engine):
        changes.append(FIX_CLIP_LEN_FLAG)
    return changes
