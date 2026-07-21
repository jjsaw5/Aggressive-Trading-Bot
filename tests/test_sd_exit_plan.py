"""Phase 3 — structure-aware short-duration exit plans."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.enums import (
    Direction,
    DTECategory,
    ShortDurationStrategy,
    StrategyType,
)
from app.domain.shortduration import IntradayLevels
from app.domain.trades import ExitLevel, RiskPlan, TradePlan
from app.domain.trades import ExitPlan as CoreExitPlan
from app.shortduration.exit_plan import build_short_duration_exit_plan
from app.shortduration.strategies.base import StrategyDetection

_NOW = datetime(2026, 7, 17, 14, 30, tzinfo=UTC)


def _levels(**kw) -> IntradayLevels:
    base = {
        "symbol": "SPY", "session_date": date(2026, 7, 17), "last": 101.5, "vwap": 100.0,
        "opening_range_high": 101.0, "opening_range_low": 99.0, "relative_volume": 2.0,
        "computed_at": _NOW,
    }
    base.update(kw)
    return IntradayLevels(**base)


def _det(strategy=ShortDurationStrategy.OPENING_RANGE_BREAKOUT, dte=DTECategory.ZERO_DTE,
         direction=Direction.BULLISH, level=101.0) -> StrategyDetection:
    return StrategyDetection(
        strategy=strategy, dte_category=dte, direction=direction, setup_score=0.6,
        entry_trigger="e", invalidation="Back inside the opening range / lose VWAP.",
        metadata={"level": level} if level is not None else {},
    )


def _plan(strategy=StrategyType.LONG_CALL, dte_stop=None) -> TradePlan:
    core = CoreExitPlan(
        method="long_option", action="sell_to_close", entry_net_per_share=1.0, contracts=1,
        max_profit_usd=None, max_loss_usd=100.0, breakevens=[],
        time_stop_dte=dte_stop,
        levels=[
            ExitLevel(kind="take_profit", label="Take profit (+50%)", net_price=1.5, pnl_usd=50.0),
            ExitLevel(kind="take_profit", label="Take profit (+100%)", net_price=2.0, pnl_usd=100.0),
            ExitLevel(kind="stop", label="Stop (-50%)", net_price=0.5, pnl_usd=-50.0),
            ExitLevel(kind="time_stop", label="Time stop", net_price=None, pnl_usd=None),
        ],
    )
    risk = RiskPlan(
        max_loss_usd=100.0, account_risk_pct=0.03, profit_target_pct=0.5, stop_loss_pct=0.5,
        time_stop_dte=dte_stop,
    )
    return TradePlan(symbol="SPY", direction=Direction.BULLISH, strategy=strategy, legs=[],
                     net_debit=100.0, contracts=1, risk=risk, exit_plan=core)


def test_0dte_orb_plan_is_structure_and_clock_aware() -> None:
    ep = build_short_duration_exit_plan(_det(), levels=_levels(), plan=_plan())
    # Primary invalidation is the opening-range structure at the broken level.
    assert "opening-range high" in ep.primary_invalidation and ep.primary_invalidation_price == 101.0
    # Secondary is the VWAP guardrail.
    assert "VWAP" in ep.secondary_invalidation and ep.secondary_invalidation_price == 100.0
    # 0DTE end-of-life is explicit — never held to settlement.
    assert ep.eod_action.startswith("close_all")
    assert "Expires today" in ep.expiration_action
    assert "Flatten by" in ep.time_stop
    assert "consecutive 1-min closes" in ep.momentum_stop


def test_0dte_vwap_plan_uses_vwap_as_primary() -> None:
    det = _det(strategy=ShortDurationStrategy.VWAP_TREND_CONTINUATION, level=None)
    ep = build_short_duration_exit_plan(det, levels=_levels(), plan=None)
    assert "VWAP" in ep.primary_invalidation and ep.primary_invalidation_price == 100.0
    assert "opening-range" in ep.secondary_invalidation


def test_premium_backstop_and_staged_targets_from_plan() -> None:
    ep = build_short_duration_exit_plan(_det(), levels=_levels(), plan=_plan())
    assert ep.premium_stop_net == 0.5  # the stop leg
    assert ep.max_loss_usd == 100.0
    labels = [t.label for t in ep.profit_targets]
    assert labels == ["PT1", "PT2"]
    assert ep.profit_targets[0].premium_net == 1.5 and ep.profit_targets[1].premium_net == 2.0
    assert "Scale out" in ep.profit_targets[0].note  # PT1 is a scale-out


def test_structural_plan_stands_alone_without_a_contract() -> None:
    ep = build_short_duration_exit_plan(_det(), levels=_levels(), plan=None)
    assert ep.premium_stop_net is None and ep.profit_targets == []
    assert ep.primary_invalidation and ep.eod_action  # structure + clock still present


def test_bearish_orb_invalidation_flips_side() -> None:
    det = _det(direction=Direction.BEARISH, level=99.0)
    ep = build_short_duration_exit_plan(det, levels=_levels(), plan=None)
    assert "above the opening-range low" in ep.primary_invalidation
    assert ep.primary_invalidation_price == 99.0


def test_1_5dte_plan_is_dte_time_stopped_not_intraday() -> None:
    det = _det(strategy=ShortDurationStrategy.TREND_CONTINUATION, dte=DTECategory.SHORT_DTE)
    ep = build_short_duration_exit_plan(det, levels=_levels(), plan=_plan(dte_stop=2))
    assert "DTE" in ep.time_stop and "2 DTE" in ep.time_stop
    assert "reassess" in ep.eod_action and not ep.eod_action.startswith("close_all")
    assert "expiration" in ep.expiration_action.lower()


async def test_run_detection_attaches_exit_plan() -> None:
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.ZERO_DTE, now=_NOW)
    assert cands
    assert all(c.exit_plan is not None for c in cands)
    top = cands[0]
    assert top.exit_plan.primary_invalidation
    assert top.exit_plan.dte_category == DTECategory.ZERO_DTE
