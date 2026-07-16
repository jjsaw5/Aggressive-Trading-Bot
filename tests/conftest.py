"""Shared test fixtures.

IMPORTANT: the database URL is set to a temp SQLite file BEFORE any `app` import
so the cached settings + global (synchronous) engine bind to a local SQLite
file, never Postgres or the durable Turso backend. Any TURSO_* values from a
local .env are cleared here so the suite can never touch the cloud database.
"""

from __future__ import annotations

import os
import tempfile

# --- Must run before importing anything under `app` ---
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="atb_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
# Never let a real Turso config bleed into tests — DATABASE_URL must win.
os.environ.pop("TURSO_DATABASE_URL", None)
os.environ.pop("TURSO_AUTH_TOKEN", None)

# Pin ALL providers to mock during tests so the suite never makes live API
# calls, regardless of any local .env that enables real providers (e.g. FMP).
for _var in (
    "PROVIDER_MARKET_DATA", "PROVIDER_FUNDAMENTALS", "PROVIDER_CALENDAR",
    "PROVIDER_OPTIONS_CHAIN", "PROVIDER_OPTIONS_FLOW", "PROVIDER_BROKERAGE",
    "PROVIDER_IV_HISTORY",
):
    os.environ[_var] = "mock"

# Pin runtime toggles a local .env might flip, so tests stay hermetic.
os.environ["ALERTS_ENABLED"] = "false"
os.environ["ALERTS_CHANNEL"] = "console"

import pytest  # noqa: E402

from app.risk.policy import RiskPolicy  # noqa: E402


@pytest.fixture
def policy() -> RiskPolicy:
    return RiskPolicy(
        account_equity_usd=2_000.0,
        max_account_risk_pct=0.06,
        max_trade_risk_pct=0.02,
        max_concurrent_positions=4,
        max_defined_risk_per_trade_usd=100.0,
    )


@pytest.fixture(autouse=True)
def _ensure_tables():
    """Ensure DB tables exist for every test (the module-level TestClient does
    not trigger the app lifespan that would otherwise create them)."""
    from app.db.session import create_all

    create_all()
    yield


def pytest_sessionfinish(session, exitstatus) -> None:
    try:
        os.close(_DB_FD)
    except OSError:
        pass
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass
