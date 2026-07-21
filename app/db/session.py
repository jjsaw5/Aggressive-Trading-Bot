"""Synchronous SQLAlchemy engine + session factory.

The durable backend is Turso / libSQL (cloud SQLite). Its SQLAlchemy dialect
(`sqlite+libsql`) is **synchronous only**, so the whole persistence layer is
sync. Async callers (FastAPI routes, the scheduler) cross the boundary with
`run_in_threadpool` / `asyncio.to_thread` rather than awaiting the DB directly.

URL precedence:
  1. `TURSO_DATABASE_URL` (+ token)  -> durable cloud SQLite (production default)
  2. `DATABASE_URL`                  -> explicit override (tests use local sqlite)
  3. Postgres from discrete settings -> legacy fallback
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


def _engine_args() -> tuple[str, dict]:
    """Resolve (url, connect_args) for the configured backend."""
    if settings.turso_database_url:
        # libsql://<db>.turso.io -> sqlite+libsql://<db>.turso.io?secure=true
        # The auth token MUST go through connect_args; putting it in the URL
        # query string yields "empty JWT token" 401s.
        host = settings.turso_database_url.split("://", 1)[-1]
        url = f"sqlite+libsql://{host}?secure=true"
        connect_args = {
            "auth_token": settings.turso_auth_token or "",
            "check_same_thread": False,
        }
        return url, connect_args

    url = settings.sqlalchemy_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return url, connect_args


_url, _connect_args = _engine_args()

engine = create_engine(
    _url,
    pool_pre_ping=True,
    future=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(engine, expire_on_commit=False, class_=Session)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def create_all() -> None:
    """Ensure the schema on startup: create missing tables AND add any missing
    columns to existing ones.

    Plain ``create_all`` only creates absent *tables* — it never evolves a table
    that already exists. Because this deployment ensures schema on boot (no separate
    migration step), a code pull that PROMOTES a new column (the promoted-columns +
    JSON payload pattern) would otherwise leave the live table one column short, and
    every query on it would fail with ``no such column``. ``_sync_columns`` closes
    that gap: it is add-only, idempotent, and safe over a populated table."""
    from app.db import models  # noqa: F401  (register models on Base.metadata)
    from app.db.base import Base

    Base.metadata.create_all(engine)
    _sync_columns(Base, engine)


def _sync_columns(base, eng) -> None:
    """Add any ORM-declared columns that are missing from an already-existing table.

    Add-only and idempotent. NOT NULL columns are added with a safe default so the
    ALTER succeeds even when the table already holds rows. A failure on one column is
    logged and skipped rather than crashing startup."""
    insp = inspect(eng)
    existing_tables = set(insp.get_table_names())
    for table in base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # freshly created by create_all — already has every column
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            try:
                type_sql = col.type.compile(dialect=eng.dialect)
                if col.nullable:
                    suffix = ""
                else:
                    up = type_sql.upper()
                    default = "''" if ("CHAR" in up or "TEXT" in up or "CLOB" in up) else "0"
                    suffix = f" NOT NULL DEFAULT {default}"
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {type_sql}{suffix}'
                with eng.begin() as conn:
                    conn.execute(text(ddl))
                log.info("db_column_added", table=table.name, column=col.name)
            except Exception as exc:  # noqa: BLE001 - never brick startup on one column
                log.warning("db_column_add_failed", table=table.name, column=col.name, error=str(exc))
