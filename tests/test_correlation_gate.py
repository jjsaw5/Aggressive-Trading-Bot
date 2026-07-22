"""Phase 3.4: the entry gate blocks a concentrated same-cluster same-direction bet."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.domain.enums import Direction, DTECategory, RejectReason
from app.domain.shortduration import ShortDurationRegime, ShortDurationRegimeState
from app.shortduration.risk import DailyRiskState, correlation_group, evaluate_entry_gates

_ET = ZoneInfo("America/New_York")
_MID = datetime(2026, 7, 17, 11, 0, tzinfo=_ET).astimezone(UTC)  # Fri mid-session


def _regime():
    return ShortDurationRegimeState(regime=ShortDurationRegime.RANGE_BOUND, confidence=0.5,
                                    allow_new_trades=True, as_of=_MID)


def test_correlation_group_buckets_semis() -> None:
    assert correlation_group("NVDA") == correlation_group("AMD") == "semis"
    assert correlation_group("SPY") == "index"
    assert correlation_group("TSLA") == "TSLA"  # unclustered -> own symbol


def test_second_correlated_bullish_is_blocked() -> None:
    # Already long a bullish semi (NVDA); a new bullish AMD is the same cluster/dir.
    daily = DailyRiskState(open_positions=1, open_book=[("NVDA", "bullish")])
    g = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BULLISH, regime=_regime(), now=_MID,
        quote_stale=False, daily=daily, equity=2000, symbol="AMD",
    )
    assert not g.allowed
    assert RejectReason.PORTFOLIO_LIMIT in g.reject_reasons
    assert any("cluster" in r for r in g.reasons)


def test_opposite_direction_or_other_cluster_is_allowed() -> None:
    daily = DailyRiskState(open_positions=1, open_book=[("NVDA", "bullish")])
    # bearish AMD (opposite direction) is not the same-direction concentration
    g1 = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BEARISH, regime=_regime(), now=_MID,
        quote_stale=False, daily=daily, equity=2000, symbol="AMD",
    )
    assert g1.allowed
    # bullish AAPL is a different cluster (megatech)
    g2 = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BULLISH, regime=_regime(), now=_MID,
        quote_stale=False, daily=daily, equity=2000, symbol="AAPL",
    )
    assert g2.allowed
