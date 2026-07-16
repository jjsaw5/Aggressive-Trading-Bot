"""Shared test fixtures."""

from __future__ import annotations

import pytest

from app.risk.policy import RiskPolicy


@pytest.fixture
def policy() -> RiskPolicy:
    return RiskPolicy(
        account_equity_usd=2_000.0,
        max_account_risk_pct=0.06,
        max_trade_risk_pct=0.02,
        max_concurrent_positions=4,
        max_defined_risk_per_trade_usd=100.0,
    )
