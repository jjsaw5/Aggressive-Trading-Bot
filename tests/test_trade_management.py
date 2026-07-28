"""Trade management: invalidation capture, thesis status, DTE-aware exit plans.

The gap these close: every broker-synced position carried a generic 50%/7-day
exit plan and an invalidation note reading "Imported broker position." — no
recorded reason for holding, so "should I hold?" had no anchor but P&L, which
cannot distinguish "wrong" from "early".
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.routes.positions import _thesis_status
from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
from app.domain.trades import ContractLeg, RiskPlan, TradePlan
from app.services.position_import import (
    ImportedLeg,
    build_tracked_trade,
    dte_regime,
    extract_invalidation,
)

_TODAY = datetime(2026, 7, 28, 14, 30, tzinfo=UTC)


# --- DTE regimes --------------------------------------------------------------
def test_gamma_regime_covers_the_binary_zone() -> None:
    for dte in (0, 1, 3):
        r = dte_regime(dte)
        assert r.name == "gamma"
        # Expiry IS the time stop: don't nag on entry day, flag on expiry day.
        assert r.time_stop_dte == 0
        assert r.profit_target_pct == 0.40  # take what you get; stops can gap
        assert "binary" in r.note


def test_theta_regime_scales_its_time_stop_with_the_expiry() -> None:
    assert dte_regime(4).name == "theta"
    assert dte_regime(15).name == "theta"
    # Exit with real time value left rather than grinding into the decay cliff.
    assert dte_regime(10).time_stop_dte == 4
    assert dte_regime(15).time_stop_dte == 6
    assert dte_regime(4).time_stop_dte == 2  # floored at 2


def test_swing_regime_gives_the_thesis_room() -> None:
    for dte in (16, 30, 45):
        r = dte_regime(dte)
        assert r.name == "swing"
        assert r.time_stop_dte == 7
        assert r.profit_target_pct == 0.60


def test_regime_is_chosen_from_dte_at_entry_and_recorded() -> None:
    def _mk(exp: date):
        legs = [ImportedLeg(strike=100.0, option_type=OptionType.CALL, is_long=True,
                            quantity=1, entry_price_per_share=1.0, expiration=exp)]
        return build_tracked_trade("AAPL", legs, opened_at=_TODAY, net_per_share=1.0)

    one_dte = _mk(date(2026, 7, 29)).trade_plan.risk
    swing = _mk(date(2026, 9, 18)).trade_plan.risk
    assert one_dte.dte_regime == "gamma" and one_dte.time_stop_dte == 0
    assert swing.dte_regime == "swing" and swing.time_stop_dte == 7
    # The old flat default (0.5 / 0.5 / 7 for everything) is gone.
    assert (one_dte.profit_target_pct, one_dte.time_stop_dte) != (0.5, 7)


# --- Invalidation capture -----------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("TSLA 370/365p 7/24 @2.45 x1 inv 380", 380.0),
    ("TSLA 370/365p 7/24 @2.45 x1 INV 380.5", 380.5),
    ("TSLA 370/365p 7/24 @2.45 invalidation: $380", 380.0),
    ("TSLA 370/365p 7/24 @2.45 inv=380", 380.0),
])
def test_extract_invalidation_pulls_the_level_and_strips_it(text, expected) -> None:
    cleaned, price = extract_invalidation(text)
    assert price == expected
    assert "inv" not in cleaned.lower()
    assert cleaned.startswith("TSLA 370/365p")


def test_extract_invalidation_is_absent_by_default() -> None:
    line = "TSLA 370/365p 7/24 @2.45 x1"
    assert extract_invalidation(line) == (line, None)


def test_build_records_the_level_and_the_side_in_plain_language() -> None:
    legs = [
        ImportedLeg(strike=370.0, option_type=OptionType.PUT, is_long=True, quantity=1,
                    entry_price_per_share=0.0, expiration=date(2026, 8, 21)),
        ImportedLeg(strike=365.0, option_type=OptionType.PUT, is_long=False, quantity=1,
                    entry_price_per_share=0.0, expiration=date(2026, 8, 21)),
    ]
    t = build_tracked_trade("TSLA", legs, opened_at=_TODAY, net_per_share=2.45,
                            invalidation=380.0)
    risk = t.trade_plan.risk
    assert risk.invalidation_price == 380.0
    # A bearish structure dies when the underlying goes UP through the level.
    assert "at or above 380" in risk.invalidation_note


# --- Thesis status ------------------------------------------------------------
def _plan(direction: Direction, invalidation: float | None) -> TradePlan:
    leg = ContractLeg(symbol="X", action=OptionAction.BUY_TO_OPEN,
                      option_type=OptionType.PUT, strike=100.0,
                      expiration=date(2026, 8, 21), quantity=1, entry_price=1.0)
    return TradePlan(symbol="X", direction=direction, strategy=StrategyType.LONG_PUT,
                     legs=[leg], net_debit=100.0, contracts=1,
                     risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                                   profit_target_pct=0.5, stop_loss_pct=0.5,
                                   invalidation_price=invalidation))


def test_bearish_thesis_dies_when_price_trades_up_through_the_level() -> None:
    plan = _plan(Direction.BEARISH, 380.0)
    assert _thesis_status(plan, 360.0)[0] == "intact"
    assert _thesis_status(plan, 380.0)[0] == "invalidated"  # at the level counts
    assert _thesis_status(plan, 391.0)[0] == "invalidated"


def test_bullish_thesis_dies_when_price_trades_down_through_the_level() -> None:
    plan = _plan(Direction.BULLISH, 320.0)
    assert _thesis_status(plan, 330.0)[0] == "intact"
    assert _thesis_status(plan, 319.0)[0] == "invalidated"


def test_distance_is_signed_room_before_invalidation() -> None:
    _status, room = _thesis_status(_plan(Direction.BEARISH, 400.0), 380.0)
    assert room == pytest.approx((400 - 380) / 380, abs=1e-4)


def test_status_abstains_without_a_level_or_a_side() -> None:
    # No level recorded — the honest state for an untagged position.
    assert _thesis_status(_plan(Direction.BEARISH, None), 380.0) == ("none", None)
    # Non-directional: a side can't be inferred, so don't guess one.
    assert _thesis_status(_plan(Direction.NEUTRAL, 380.0), 380.0) == ("none", None)


def test_recorded_thesis_without_a_mark_is_unevaluated_not_missing() -> None:
    # A thesis we can't currently check is NOT the same as no thesis — the board
    # said "no thesis" for a position that had a level but no live mark.
    assert _thesis_status(_plan(Direction.BEARISH, 380.0), None) == ("unevaluated", None)


# --- End to end ---------------------------------------------------------------
def test_quick_add_accepts_an_inline_invalidation() -> None:
    import app.main as m
    from app.db import repository

    client = TestClient(m.app)
    r = client.post("/positions/quick-add",
                    json={"line": "TSLA 370/365p 8/21 @2.45 x1 inv 380"})
    assert r.status_code == 200, r.text
    t = repository.get_paper_trade(r.json()["id"])
    try:
        assert t.trade_plan.risk.invalidation_price == 380.0
        assert t.trade_plan.risk.dte_regime == "swing"
    finally:
        repository.delete_paper_trade(t.id)


def test_a_position_can_be_retagged_or_cleared_after_the_fact() -> None:
    # Entry now auto-adopts the engine's structural level, so the endpoint's job
    # is OVERRIDING it with your own read (and clearing it if you'd rather
    # manage on stops alone).
    import app.main as m
    from app.db import repository

    client = TestClient(m.app)
    tid = client.post("/positions/quick-add",
                      json={"line": "NVDA 200/190p 8/21 @2.00 x1"}).json()["id"]
    try:
        r = client.post(f"/positions/{tid}/invalidation", json={"invalidation": 215.0})
        assert r.status_code == 200, r.text
        assert repository.get_paper_trade(tid).trade_plan.risk.invalidation_price == 215.0
        # ...and can be cleared again.
        client.post(f"/positions/{tid}/invalidation", json={"invalidation": None})
        assert repository.get_paper_trade(tid).trade_plan.risk.invalidation_price is None
    finally:
        repository.delete_paper_trade(tid)


def test_tagging_a_closed_position_is_refused() -> None:
    import app.main as m
    from app.db import repository

    client = TestClient(m.app)
    tid = client.post("/positions/quick-add",
                      json={"line": "AMD 200/190p 8/21 @2.00 x1"}).json()["id"]
    try:
        client.post(f"/positions/{tid}/close", json={"exit_price_per_share": 2.50})
        r = client.post(f"/positions/{tid}/invalidation", json={"invalidation": 215.0})
        assert r.status_code == 400 and "already closed" in r.json()["detail"]
    finally:
        repository.delete_paper_trade(tid)
