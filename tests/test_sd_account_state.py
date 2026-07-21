"""Phase 3 — account state + risk-aware sizing."""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.account import AccountState
from app.domain.enums import DTECategory
from app.domain.shortduration import ShortDurationTrade

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _state(**kw) -> AccountState:
    base = {
        "source": "paper", "verified": False, "equity_usd": 2000.0,
        "buying_power_usd": 2000.0, "as_of": _NOW,
    }
    base.update(kw)
    return AccountState(**base)


def test_available_risk_is_equity_minus_committed() -> None:
    s = _state(open_risk_usd=300.0, pending_risk_usd=100.0)
    assert s.committed_risk_usd == 400.0
    assert s.available_risk_usd == 1600.0  # 2000 - 400


def test_available_risk_capped_by_buying_power() -> None:
    s = _state(equity_usd=2000.0, buying_power_usd=500.0, open_risk_usd=0.0)
    assert s.available_risk_usd == 500.0  # BP is the binding constraint


def test_available_risk_floored_at_zero() -> None:
    s = _state(equity_usd=2000.0, open_risk_usd=2500.0)
    assert s.available_risk_usd == 0.0


async def test_fallback_provider_is_configured_constant_and_unverified() -> None:
    from app.providers.account import ConfiguredFallbackAccountState

    st = await ConfiguredFallbackAccountState().get_account_state(now=_NOW)
    assert st.source == "fallback" and st.verified is False
    assert st.equity_usd > 0 and st.buying_power_usd == st.equity_usd
    assert st.committed_risk_usd == 0.0


async def test_paper_provider_reflects_open_defined_risk() -> None:
    from app.db import repository
    from app.db.models import ShortDurationTradeRow
    from app.db.session import SessionLocal
    from app.providers.account import PaperAccountState

    prov = PaperAccountState()
    before = await prov.get_account_state(now=_NOW)
    trade = ShortDurationTrade(
        id="acct_test01", candidate_id="c1", paper_trade_id="p1", symbol="SPY",
        dte_category=DTECategory.ZERO_DTE, opened_at=_NOW, max_loss_usd=150.0, status="open",
    )
    repository.save_short_duration_trade(trade)
    try:
        after = await prov.get_account_state(now=_NOW)
        assert after.source == "paper" and after.verified is False
        assert after.open_risk_usd >= before.open_risk_usd + 150.0
        # Buying power drops by the newly committed risk.
        assert after.buying_power_usd <= after.equity_usd - 150.0 + 1e-6
    finally:
        # Don't leak an open position into the shared DB — later scans size against it.
        with SessionLocal() as s:
            row = s.get(ShortDurationTradeRow, "acct_test01")
            if row is not None:
                s.delete(row)
                s.commit()


def test_account_state_endpoint() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.get("/account/state")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] in {"paper", "fallback"} and body["verified"] is False
    assert "available_risk_usd" not in body or body.get("equity_usd") is not None


async def test_run_detection_sizes_against_account_state() -> None:
    # Sizing now reads the account snapshot; the scan still produces candidates.
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.ZERO_DTE, now=_NOW)
    assert cands  # account wiring did not break the pipeline
