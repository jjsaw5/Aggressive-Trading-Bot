"""Positions board robustness: the poison-row 500 (mislabeled structure) class."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import (
    Direction,
    OptionAction,
    OptionType,
    PaperTradeStatus,
    StrategyType,
)
from app.domain.trades import ContractLeg, PaperTrade, RiskPlan, TradePlan
from app.quant.analytics import structure_breakevens
from app.services.position_import import ImportedLeg, _infer


def _long_put_leg(strike: float) -> ContractLeg:
    return ContractLeg(symbol="TSLA", action=OptionAction.BUY_TO_OPEN,
                       option_type=OptionType.PUT, strike=strike,
                       expiration=date(2026, 7, 24), quantity=1, entry_price=1.0)


def _poison_trade() -> PaperTrade:
    # The exact stored shape that 500'd the board: strategy says LONG_CALL but the
    # legs are two LONG PUTS (full-form entry with leg 2 left as "Long").
    plan = TradePlan(
        symbol="TSLA", direction=Direction.NEUTRAL, strategy=StrategyType.LONG_CALL,
        legs=[_long_put_leg(370.0), _long_put_leg(365.0)], net_debit=245.0, contracts=1,
        risk=RiskPlan(max_loss_usd=245.0, account_risk_pct=0.1,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )
    return PaperTrade(
        id=uuid.uuid4().hex[:12], scan_id="manual", symbol="TSLA", trade_plan=plan,
        status=PaperTradeStatus.OPEN, opened_at=datetime(2026, 7, 20, tzinfo=UTC),
        entry_fill=2.45, entry_slippage=0.0,
    )


def test_breakevens_degrade_to_empty_on_label_leg_mismatch() -> None:
    # The honest answer for a malformed record is "no breakeven computable" — never
    # an exception (min() on an empty sequence was the 500).
    assert structure_breakevens(_poison_trade().trade_plan) == []


def test_infer_rejects_two_long_puts_with_corrective_message() -> None:
    legs = [
        ImportedLeg(strike=370.0, option_type=OptionType.PUT, is_long=True,
                    quantity=1, entry_price_per_share=0.0, expiration=date(2026, 7, 24)),
        ImportedLeg(strike=365.0, option_type=OptionType.PUT, is_long=True,
                    quantity=1, entry_price_per_share=0.0, expiration=date(2026, 7, 24)),
    ]
    with pytest.raises(ValueError, match="mark the second leg as Sold"):
        _infer(legs)


def test_full_form_import_rejects_mislabeled_entry_with_400() -> None:
    # The entry path that created the poison row now refuses it, with the fix
    # spelled out — instead of saving a record that breaks the board.
    import app.main as m

    client = TestClient(m.app)
    r = client.post("/positions/import", json={
        "symbol": "TSLA", "net_debit_per_share": 2.45,
        "legs": [
            {"strike": 370.0, "option_type": "put", "is_long": True, "quantity": 1,
             "expiration": "2026-07-24"},
            {"strike": 365.0, "option_type": "put", "is_long": True, "quantity": 1,
             "expiration": "2026-07-24"},
        ],
    })
    assert r.status_code == 400
    assert "Sold" in r.json()["detail"]


def test_board_isolates_a_poison_row_instead_of_500ing() -> None:
    # A malformed record already in the DB (saved before the entry-side fix) must
    # degrade to a visible, deletable warning row — the rest of the board renders.
    import app.main as m
    from app.db import repository

    t = _poison_trade()
    repository.save_paper_trade(t)
    try:
        client = TestClient(m.app)
        r = client.get("/positions")
        assert r.status_code == 200
        row = next((p for p in r.json() if p["id"] == t.id), None)
        assert row is not None
        assert any("malformed" in w for w in row["warnings"])
    finally:
        repository.delete_paper_trade(t.id)
