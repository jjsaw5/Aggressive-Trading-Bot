"""Phase 1: resolve a decision against REAL option marks, net of round-trip costs.

The forward outcome ledger must record what a trade actually made — option
intrinsic + extrinsic value minus commissions and slippage — not an
underlying-vs-breakeven directional proxy. These lock in that math.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.analytics.outcomes import resolve_from_marks
from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
from app.domain.options import Greeks, OptionChain, OptionContract
from app.domain.outcomes import DecisionSnapshot, DecisionSource, OutcomeResult
from app.domain.trades import ContractLeg, RiskPlan, TradePlan

_GEN = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
_EXP = date(2026, 7, 17)
_NOW = datetime(2026, 6, 20, 15, 0, tzinfo=UTC)


def _leg(action: OptionAction, strike: float, mid: float) -> ContractLeg:
    return ContractLeg(
        symbol="AAA", action=action, option_type=OptionType.CALL,
        strike=strike, expiration=_EXP, quantity=1, entry_price=mid,
    )


def _snap(entry_net: float, contracts: int = 2) -> DecisionSnapshot:
    # Bull call debit spread: BUY 100C / SELL 105C.
    plan = TradePlan(
        symbol="AAA", direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD,
        legs=[_leg(OptionAction.BUY_TO_OPEN, 100.0, 3.0), _leg(OptionAction.SELL_TO_OPEN, 105.0, 1.0)],
        net_debit=entry_net * 100 * contracts, contracts=contracts,
        risk=RiskPlan(max_loss_usd=entry_net * 100 * contracts, max_profit_usd=500.0,
                      account_risk_pct=0.05, profit_target_pct=0.5, stop_loss_pct=0.5),
    )
    return DecisionSnapshot(
        decision_id="scan1:AAA", scan_id="scan1", symbol="AAA", source=DecisionSource.SCAN,
        direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD, generated_at=_GEN,
        composite_score=0.6, breakevens=[102.0], entry_spot=100.0, entry_iv=0.35,
        entry_net_per_share=entry_net, max_loss_usd=entry_net * 100 * contracts,
        max_profit_usd=500.0, contracts=contracts, expiration=_EXP, dte_at_entry=46, trade_plan=plan,
    )


def _contract(strike: float, mid: float, half_spread: float = 0.025) -> OptionContract:
    return OptionContract(
        symbol="AAA", expiration=_EXP, strike=strike, option_type=OptionType.CALL,
        bid=mid - half_spread, ask=mid + half_spread, mark=mid, implied_volatility=0.35,
        greeks=Greeks(delta=0.5), as_of=_NOW, source="test",
    )


def _chain(c100_mid: float, c105_mid: float, *, spot: float = 103.0, legs=("100", "105")) -> OptionChain:
    contracts = []
    if "100" in legs:
        contracts.append(_contract(100.0, c100_mid))
    if "105" in legs:
        contracts.append(_contract(105.0, c105_mid))
    return OptionChain(symbol="AAA", underlying_price=spot, contracts=contracts, as_of=_NOW, source="test")


def test_marks_gross_net_costs_and_win() -> None:
    # entry net 2.00; now the spread is worth 2.50 -> +0.50/share x100 x2 = $100 gross.
    snap = _snap(entry_net=2.00, contracts=2)
    chain = _chain(4.00, 1.50)  # net now 2.50, each leg 0.05 spread
    out = resolve_from_marks(snap, chain, resolved_at=_NOW)
    assert out is not None
    assert out.realized_pnl_gross_usd == 100.0
    # costs: commission 2 legs x 2 contracts x $0.65 x 2 (open+close) = 5.20 ;
    # slippage 2 contracts x 100 x 0.10 total leg spread = 20.00 -> 25.20
    assert out.costs_usd == 25.20
    assert out.realized_pnl_usd == 74.80
    assert out.realized_pnl_usd < out.realized_pnl_gross_usd  # NET < GROSS (costs bite)
    assert out.result == OutcomeResult.WIN
    assert out.outcome_source == "option_marks"
    assert out.used_bs_fallback is False


def test_marks_loss_when_value_drops() -> None:
    # The spread lost value: entry 2.00 -> now 1.50 = -$50 gross, worse net -> LOSS.
    snap = _snap(entry_net=2.00, contracts=1)
    chain = _chain(4.00, 2.50)  # net now 1.50
    out = resolve_from_marks(snap, chain, resolved_at=_NOW)
    assert out.realized_pnl_gross_usd == -50.0
    assert out.realized_pnl_usd < out.realized_pnl_gross_usd  # costs deepen the loss
    assert out.result == OutcomeResult.LOSS


def test_costs_erode_a_thin_gross_edge() -> None:
    # A barely-positive gross ($10) is more than eaten by round-trip costs: the
    # ledger records a NEGATIVE net (here inside the scratch band, not a false win).
    snap = _snap(entry_net=2.00, contracts=1)
    chain = _chain(4.00, 1.90)  # net 2.10 -> gross +$10 on 1 contract
    out = resolve_from_marks(snap, chain, resolved_at=_NOW)
    assert out.realized_pnl_gross_usd == 10.0
    assert out.realized_pnl_usd < 0 < out.realized_pnl_gross_usd  # gross gain, net loss
    assert out.result != OutcomeResult.WIN


def test_marks_bs_fallback_when_a_leg_is_missing() -> None:
    # The chain can't quote the 105 leg -> it is Black-Scholes-marked and labeled.
    snap = _snap(entry_net=2.00, contracts=1)
    chain = _chain(4.00, 0.0, legs=("100",))  # only the 100 leg present
    out = resolve_from_marks(snap, chain, resolved_at=_NOW)
    assert out is not None
    assert out.used_bs_fallback is True
    assert out.outcome_source == "option_marks_bs_fallback"


def test_marks_unpriceable_returns_none() -> None:
    # A leg missing AND no IV anywhere -> cannot mark; caller falls back to proxy.
    snap = _snap(entry_net=2.00, contracts=1)
    snap.entry_iv = None
    chain = OptionChain(symbol="AAA", underlying_price=0.0, contracts=[_contract(100.0, 4.0)],
                        as_of=_NOW, source="test")
    assert resolve_from_marks(snap, chain, resolved_at=_NOW) is None
