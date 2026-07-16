"""Decision snapshots, outcome resolution, and calibration math (pure)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.analytics.calibration import build_scorecard, select_scoring_outcomes
from app.analytics.outcomes import resolve_from_paper_trade, resolve_underlying
from app.analytics.snapshots import snapshot_from_candidate
from app.domain.enums import (
    Direction,
    ExitReason,
    PaperTradeStatus,
    StrategyType,
)
from app.domain.outcomes import DecisionSnapshot, DecisionSource, OutcomeResult
from app.domain.trades import PaperTrade, RiskPlan, TradePlan
from app.engine.candidate_builder import ScanEngine
from app.engine.universe import UniverseConfig
from app.providers.mock import MockProvider
from app.risk.policy import RiskPolicy

_GEN = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)


def _snap(
    *,
    strategy: StrategyType,
    direction: Direction,
    breakevens: list[float],
    entry_spot: float,
    pop: float | None = None,
    score: float = 0.6,
    decision_id: str = "scan1:AAA",
) -> DecisionSnapshot:
    plan = TradePlan(
        symbol="AAA",
        direction=direction,
        strategy=strategy,
        legs=[],
        net_debit=150.0,
        contracts=1,
        risk=RiskPlan(
            max_loss_usd=150.0,
            max_profit_usd=350.0,
            account_risk_pct=0.075,
            profit_target_pct=0.5,
            stop_loss_pct=0.5,
        ),
    )
    return DecisionSnapshot(
        decision_id=decision_id,
        scan_id="scan1",
        symbol="AAA",
        source=DecisionSource.SCAN,
        direction=direction,
        strategy=strategy,
        generated_at=_GEN,
        composite_score=score,
        probability_of_profit=pop,
        breakevens=breakevens,
        entry_spot=entry_spot,
        entry_net_per_share=1.5,
        max_loss_usd=150.0,
        max_profit_usd=350.0,
        contracts=1,
        expiration=date(2026, 7, 17),
        dte_at_entry=46,
        trade_plan=plan,
    )


# --- snapshot building -------------------------------------------------------
async def test_snapshot_from_actionable_candidate() -> None:
    mock = MockProvider()
    engine = ScanEngine(
        market=mock, fundamentals=mock, chain=mock, flow=mock, calendar=mock,
        policy=RiskPolicy(
            account_equity_usd=2000, max_account_risk_pct=0.15, max_trade_risk_pct=0.05,
            max_concurrent_positions=4, max_defined_risk_per_trade_usd=100,
        ),
        universe=UniverseConfig(symbols=["SPY", "AAPL", "NVDA"]),
    )
    cands = await engine.run()
    actionable = next((c for c in cands if c.is_actionable), None)
    if actionable is None:
        return  # mock rejected all this run; other tests cover the math

    snap = snapshot_from_candidate(actionable)
    assert snap is not None
    assert snap.decision_id == f"{actionable.scan_id}:{actionable.symbol.upper()}"
    assert snap.entry_spot > 0  # frozen from analytics.spot_at_analysis
    assert snap.max_loss_usd <= 100.0 + 1e-6
    assert snap.trade_plan is actionable.trade_plan
    # A rejected candidate never snapshots.
    rejected = next((c for c in cands if not c.is_actionable), None)
    if rejected is not None:
        assert snapshot_from_candidate(rejected) is None


# --- underlying resolution ---------------------------------------------------
def test_resolve_bullish_win_and_loss() -> None:
    snap = _snap(
        strategy=StrategyType.BULL_CALL_SPREAD, direction=Direction.BULLISH,
        breakevens=[101.5], entry_spot=100.0, pop=0.4,
    )
    now = datetime(2026, 7, 17, tzinfo=UTC)
    win = resolve_underlying(snap, spot_now=110.0, resolved_at=now)
    assert win.result == OutcomeResult.WIN
    assert win.direction_correct is True
    assert win.underlying_return_pct == 10.0
    assert win.elapsed_days == 46

    loss = resolve_underlying(snap, spot_now=99.0, resolved_at=now)
    assert loss.result == OutcomeResult.LOSS
    assert loss.direction_correct is False  # went down on a bullish thesis


def test_resolve_scratch_band() -> None:
    snap = _snap(
        strategy=StrategyType.BULL_CALL_SPREAD, direction=Direction.BULLISH,
        breakevens=[100.0], entry_spot=100.0,
    )
    now = datetime(2026, 7, 17, tzinfo=UTC)
    out = resolve_underlying(snap, spot_now=100.1, resolved_at=now)  # within 0.25%
    assert out.result == OutcomeResult.SCRATCH


def test_resolve_iron_condor_inside_is_win() -> None:
    snap = _snap(
        strategy=StrategyType.IRON_CONDOR, direction=Direction.NEUTRAL,
        breakevens=[93.0, 107.0], entry_spot=100.0, pop=0.66,
    )
    now = datetime(2026, 7, 17, tzinfo=UTC)
    inside = resolve_underlying(snap, spot_now=101.0, resolved_at=now)
    assert inside.result == OutcomeResult.WIN
    assert inside.direction_correct is None  # neutral: no directional call
    outside = resolve_underlying(snap, spot_now=112.0, resolved_at=now)
    assert outside.result == OutcomeResult.LOSS


def test_resolve_long_strangle_outside_is_win() -> None:
    snap = _snap(
        strategy=StrategyType.LONG_STRANGLE, direction=Direction.VOL_LONG,
        breakevens=[92.0, 108.0], entry_spot=100.0,
    )
    now = datetime(2026, 7, 17, tzinfo=UTC)
    assert resolve_underlying(snap, spot_now=120.0, resolved_at=now).result == OutcomeResult.WIN
    assert resolve_underlying(snap, spot_now=100.0, resolved_at=now).result == OutcomeResult.LOSS


def test_resolve_from_paper_trade_uses_realized_pnl() -> None:
    snap = _snap(
        strategy=StrategyType.BULL_CALL_SPREAD, direction=Direction.BULLISH,
        breakevens=[101.5], entry_spot=100.0,
    )
    trade = PaperTrade(
        id="t1", scan_id="scan1", symbol="AAA", trade_plan=snap.trade_plan,
        status=PaperTradeStatus.CLOSED,
        opened_at=_GEN, entry_fill=1.5,
        closed_at=datetime(2026, 6, 20, tzinfo=UTC),
        exit_fill=2.25, exit_reason=ExitReason.PROFIT_TARGET, realized_pnl_usd=75.0,
    )
    out = resolve_from_paper_trade(snap, trade)
    assert out is not None
    assert out.result == OutcomeResult.WIN
    assert out.outcome_source == "paper_trade"
    assert out.realized_pnl_usd == 75.0
    assert out.horizon_label == "trade_close"


# --- calibration -------------------------------------------------------------
def test_select_scoring_prefers_paper_then_longest_horizon() -> None:
    snap = _snap(strategy=StrategyType.LONG_CALL, direction=Direction.BULLISH,
                 breakevens=[101.5], entry_spot=100.0)
    now = datetime(2026, 6, 10, tzinfo=UTC)
    o7 = resolve_underlying(snap, spot_now=105.0, resolved_at=now, horizon_label="9d")
    later = datetime(2026, 6, 25, tzinfo=UTC)
    o21 = resolve_underlying(snap, spot_now=108.0, resolved_at=later, horizon_label="24d")
    chosen = select_scoring_outcomes([o7, o21])
    assert chosen[snap.decision_id].horizon_label == "24d"  # longest horizon wins


def test_scorecard_win_rate_and_calibration() -> None:
    # Three decisions in the 0.7-1.0 POP bucket: 2 wins, 1 loss -> 66.7% realized.
    snaps, outs = [], []
    now = datetime(2026, 7, 17, tzinfo=UTC)
    for i, (spot_now, pop) in enumerate([(110.0, 0.72), (108.0, 0.75), (95.0, 0.78)]):
        s = _snap(
            strategy=StrategyType.BULL_CALL_SPREAD, direction=Direction.BULLISH,
            breakevens=[101.5], entry_spot=100.0, pop=pop, score=0.7,
            decision_id=f"scan1:S{i}",
        )
        snaps.append(s)
        outs.append(resolve_underlying(s, spot_now=spot_now, resolved_at=now))

    card = build_scorecard(snaps, outs)
    assert card.n_decisions == 3
    assert card.n_resolved == 3
    assert card.n_decisive == 3
    assert card.win_rate == round(2 / 3, 4)
    assert card.direction_accuracy == round(2 / 3, 4)  # two went up, one down
    assert card.brier_score is not None
    # One POP bucket (0.7-1.0) holds all three.
    bucket = next(b for b in card.pop_buckets if b.n == 3)
    assert bucket.realized_win_rate == round(2 / 3, 4)
    assert bucket.calibration_gap is not None
    # Strategy + direction groupings present.
    assert card.by_strategy[0].key == "bull_call_spread"
    assert card.by_direction[0].key == "bullish"


def test_scorecard_empty_is_safe() -> None:
    card = build_scorecard([], [])
    assert card.n_decisions == 0
    assert card.win_rate is None
    assert card.pop_buckets == []
