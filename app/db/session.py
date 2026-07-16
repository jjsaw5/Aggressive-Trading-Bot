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

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


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
    """Create tables if missing. Convenient for dev/tests; production uses
    Alembic migrations (idempotent — create_all only adds missing tables)."""
    from app.db import models  # noqa: F401  (register models on Base.metadata)
    from app.db.base import Base

    Base.metadata.create_all(engine)
