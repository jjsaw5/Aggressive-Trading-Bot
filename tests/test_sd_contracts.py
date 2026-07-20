"""Short-duration Phase 4 — contract selection + risk gates."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.enums import Direction, DTECategory, OptionType, RejectReason
from app.domain.options import Greeks, OptionChain, OptionContract
from app.domain.shortduration import ShortDurationRegimeState
from app.scheduling.clock import MarketClock
from app.shortduration.contracts import select_short_duration_contract
from app.shortduration.risk import (
    DailyRiskState,
    RiskGateConfig,
    evaluate_entry_gates,
    short_duration_policy,
)

_ET = ZoneInfo("America/New_York")


def _spot() -> float:
    return 100.0


def _contract(strike, otype, dte, *, spot=100.0, bid=1.0, ask=1.05, oi=5000, vol=500, delta=0.5):
    now = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    return OptionContract(
        symbol="SPY", expiration=date(2026, 7, 17) + timedelta(days=dte), strike=strike,
        option_type=otype, bid=bid, ask=ask, open_interest=oi, volume=vol,
        implied_volatility=0.3, greeks=Greeks(delta=delta), as_of=now, source="test",
    )


def _chain(dte=2, otype=OptionType.CALL) -> OptionChain:
    now = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    # A ladder of strikes around spot at the target DTE, all liquid.
    strikes = [95, 98, 100, 102, 105, 108]
    deltas = {95: 0.8, 98: 0.65, 100: 0.5, 102: 0.35, 105: 0.2, 108: 0.1}
    contracts = [
        _contract(k, otype, dte, bid=max(0.2, (100 - k) * 0.1 + 1) if otype == OptionType.CALL else max(0.2, (k - 100) * 0.1 + 1),
                  ask=max(0.25, (100 - k) * 0.1 + 1.1) if otype == OptionType.CALL else max(0.25, (k - 100) * 0.1 + 1.1),
                  delta=deltas[k])
        for k in strikes
    ]
    return OptionChain(symbol="SPY", underlying_price=100.0, contracts=contracts, as_of=now, source="test")


# --- Contract selection ------------------------------------------------------
def test_selects_defined_risk_spread_within_cap() -> None:
    policy = short_duration_policy(DTECategory.SHORT_DTE)  # $100 cap
    res = select_short_duration_contract(
        _chain(dte=3, otype=OptionType.CALL), Direction.BULLISH, DTECategory.SHORT_DTE,
        policy=policy, as_of=date(2026, 7, 17),
    )
    assert res.is_tradeable
    assert res.plan.risk.max_loss_usd <= policy.max_trade_risk_usd
    assert res.recommendation.legs  # concrete legs present
    assert res.recommendation.breakevens


def test_rejects_when_no_liquid_contract() -> None:
    now = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    illiquid = OptionChain(
        symbol="SPY", underlying_price=100.0, as_of=now, source="test",
        contracts=[
            OptionContract(symbol="SPY", expiration=date(2026, 7, 20), strike=100.0,
                           option_type=OptionType.CALL, bid=1.0, ask=3.0,  # 100%+ spread
                           open_interest=5, volume=1, greeks=Greeks(delta=0.5),
                           as_of=now, source="test")
        ],
    )
    res = select_short_duration_contract(
        illiquid, Direction.BULLISH, DTECategory.SHORT_DTE,
        policy=short_duration_policy(DTECategory.SHORT_DTE), as_of=date(2026, 7, 17),
    )
    assert not res.is_tradeable
    assert RejectReason.ILLIQUID_OPTION in res.reject_reasons


def test_short_duration_policy_is_tighter_for_0dte() -> None:
    p0 = short_duration_policy(DTECategory.ZERO_DTE)
    p5 = short_duration_policy(DTECategory.SHORT_DTE)
    assert p0.max_trade_risk_pct < p5.max_trade_risk_pct
    assert p0.default_time_stop_dte == 0  # close same day
    assert p5.default_time_stop_dte == 1


# --- Entry gates -------------------------------------------------------------
def _regime(allow=True, reduce=False):
    return ShortDurationRegimeState(
        regime=__import__("app.domain.enums", fromlist=["ShortDurationRegime"]).ShortDurationRegime.RANGE_BOUND,
        confidence=0.5, allow_new_trades=allow, reduce_size=reduce,
        as_of=datetime(2026, 7, 17, 15, 0, tzinfo=UTC),
    )


def _clock() -> MarketClock:
    return MarketClock()


def _mid_session() -> datetime:
    # Fri 2026-07-17, 11:00 ET -> UTC
    return datetime(2026, 7, 17, 11, 0, tzinfo=_ET).astimezone(UTC)


def test_entry_gate_clear_mid_session() -> None:
    g = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BULLISH, regime=_regime(),
        now=_mid_session(), quote_stale=False, daily=DailyRiskState(), equity=2000, clock=_clock(),
    )
    assert g.allowed and g.size_modifier == 1.0


def test_entry_gate_blocks_first_minutes() -> None:
    open_plus_2 = datetime(2026, 7, 17, 9, 32, tzinfo=_ET).astimezone(UTC)
    g = evaluate_entry_gates(
        dte=DTECategory.ZERO_DTE, direction=Direction.BULLISH, regime=_regime(),
        now=open_plus_2, quote_stale=False, daily=DailyRiskState(), equity=2000, clock=_clock(),
    )
    assert not g.allowed
    assert RejectReason.TIME_OF_DAY_BLOCKED in g.reject_reasons


def test_entry_gate_blocks_0dte_after_cutoff() -> None:
    late = datetime(2026, 7, 17, 15, 30, tzinfo=_ET).astimezone(UTC)  # past 15:00 cutoff
    g = evaluate_entry_gates(
        dte=DTECategory.ZERO_DTE, direction=Direction.BULLISH, regime=_regime(),
        now=late, quote_stale=False, daily=DailyRiskState(), equity=2000, clock=_clock(),
    )
    assert not g.allowed and RejectReason.TIME_OF_DAY_BLOCKED in g.reject_reasons


def test_entry_gate_blocks_stale_quote_and_daily_loss() -> None:
    g = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BULLISH, regime=_regime(),
        now=_mid_session(), quote_stale=True,
        daily=DailyRiskState(realized_pnl_usd=-120, consecutive_losses=2), equity=2000, clock=_clock(),
    )
    assert not g.allowed
    assert RejectReason.STALE_QUOTE in g.reject_reasons
    assert RejectReason.DAILY_LOSS_LIMIT in g.reject_reasons


def test_entry_gate_reduce_size_in_cautious_regime() -> None:
    g = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BULLISH, regime=_regime(reduce=True),
        now=_mid_session(), quote_stale=False, daily=DailyRiskState(), equity=2000, clock=_clock(),
    )
    assert g.allowed and g.size_modifier == 0.5


def test_entry_gate_blocks_when_regime_disallows() -> None:
    g = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BULLISH, regime=_regime(allow=False),
        now=_mid_session(), quote_stale=False, daily=DailyRiskState(), equity=2000, clock=_clock(),
    )
    assert not g.allowed and RejectReason.RESTRICTED_EVENT_WINDOW in g.reject_reasons


def test_gate_config_parses_cutoff() -> None:
    cfg = RiskGateConfig.from_settings()
    assert isinstance(cfg.cutoff_0dte_et, time)


def test_plural_offers_both_long_and_spread_when_unconstrained(monkeypatch) -> None:
    """With the cap lifted, the plural selector returns BOTH a single-leg long and
    a defined-risk spread as separate pickable expressions."""
    from app.config import settings
    from app.shortduration.contracts import select_short_duration_contracts

    monkeypatch.setattr(settings, "short_duration_paper_unconstrained", True, raising=False)
    res = select_short_duration_contracts(
        _chain(dte=3, otype=OptionType.CALL), Direction.BULLISH, DTECategory.SHORT_DTE,
        policy=short_duration_policy(DTECategory.SHORT_DTE), as_of=date(2026, 7, 17),
    )
    leg_counts = sorted(len(r.recommendation.legs) for r in res if r.is_tradeable)
    assert leg_counts == [1, 2]  # one single-leg long + one two-leg spread


def test_moneyness_fallback_selects_atm_when_delta_missing(monkeypatch) -> None:
    """When provider greeks are degenerate (delta 0 for a liquid ATM contract) the
    0DTE selector falls back to moneyness and still finds a near-ATM contract,
    instead of rejecting a genuinely liquid name."""
    from app.config import settings
    from app.shortduration.contracts import select_short_duration_contracts

    monkeypatch.setattr(settings, "short_duration_paper_unconstrained", True, raising=False)
    now = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
    # Liquid 0DTE calls around spot 100, but every delta is 0.0 (missing greeks).
    contracts = [
        _contract(k, OptionType.CALL, dte=0, bid=b, ask=b + 0.05, oi=4000, vol=5000, delta=0.0)
        for k, b in [(99, 1.4), (100, 0.9), (101, 0.5), (102, 0.25), (105, 0.05)]
    ]
    chain = OptionChain(symbol="SPY", underlying_price=100.0, contracts=contracts,
                        as_of=now, source="test")
    res = select_short_duration_contracts(
        chain, Direction.BULLISH, DTECategory.ZERO_DTE,
        policy=short_duration_policy(DTECategory.ZERO_DTE), as_of=date(2026, 7, 17),
    )
    tradeable = [r for r in res if r.is_tradeable]
    assert tradeable, "moneyness fallback should yield at least the single-leg long"
    # The chosen long leg is a near-ATM strike (within the 3% 0DTE band of spot 100).
    long_leg = next(r for r in tradeable if len(r.recommendation.legs) == 1)
    assert abs(long_leg.recommendation.legs[0]["strike"] - 100.0) <= 3.0
