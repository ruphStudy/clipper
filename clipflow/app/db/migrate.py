"""Additive auto-migration: ADD COLUMN for model columns missing in SQLite."""
from sqlalchemy import Engine, inspect, text

from app.db.models import Base
from app.logger import get_logger

log = get_logger("clipflow.migrate")


def auto_migrate(engine: Engine) -> list[str]:
    """Add missing columns to existing tables (never drops); returns 'table.column' added."""
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
