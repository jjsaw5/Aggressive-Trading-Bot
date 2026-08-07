"""Contract selection: bands, sizing, payoff maths and Greek provenance."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.enums import OptionAction, OptionType, StrategyType
from app.domain.options import Greeks, OptionChain, OptionContract
from app.multiagent.config import get_methodology
from app.multiagent.models.enums import Direction
from app.multiagent.models.measurements import Provenance
from app.multiagent.selection import NoContractError, propose_structures, select_structure
from app.multiagent.selection.contracts import strike_invariant_greeks
from app.quant.pricing import black_scholes_delta, black_scholes_price

NOW = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)


def build_chain(
    symbol: str = "TEST",
    *,
    spot: float = 100.0,
    iv: float = 0.35,
    dtes: tuple[int, ...] = (14, 21, 30),
    spread_frac: float = 0.02,
    oi: int = 2000,
    volume: int = 800,
    flat_greeks: bool = False,
    strike_step: float | None = None,
) -> OptionChain:
    """A Black-Scholes-priced ladder with controllable liquidity and Greeks.

    The strike grid scales with price, as real listed chains do (roughly $0.50
    increments under $25, $1 to $100, $2.50 to $200, $5 above). A fixed $2.50
    grid on a $20 underlying is not a realistic chain, and testing against one
    measures the fixture rather than the selector.
    """
    if strike_step is None:
        strike_step = 0.5 if spot < 25 else 1.0 if spot < 100 else 2.5 if spot < 200 else 5.0
    contracts: list[OptionContract] = []
    for dte in dtes:
        exp = NOW.date() + timedelta(days=dte)
        t = dte / 365.0
        for i in range(-20, 21):
            strike = round(spot + i * strike_step, 2)
            if strike <= 0:
                continue
            for otype in (OptionType.CALL, OptionType.PUT):
                price = black_scholes_price(spot, strike, t, iv, otype)
                if price < 0.05:
                    continue
                delta = black_scholes_delta(spot, strike, t, iv, otype)
                half = price * spread_frac / 2
                greeks = (
                    Greeks(delta=round(delta, 3), gamma=0.01, theta=-0.03, vega=0.1)
                    if flat_greeks
                    else Greeks(delta=round(delta, 3))
                )
                contracts.append(
                    OptionContract(
                        symbol=symbol,
                        option_symbol=f"{symbol}{exp:%y%m%d}{otype.value[0].upper()}{int(strike*1000):08d}",
                        expiration=exp,
                        strike=strike,
                        option_type=otype,
                        bid=round(max(0.01, price - half), 2),
                        ask=round(price + half, 2),
                        mark=round(price, 2),
                        volume=volume,
                        open_interest=oi,
                        implied_volatility=iv,
                        greeks=greeks,
                        as_of=NOW,
                        source="test",
                    )
                )
    return OptionChain(symbol=symbol, underlying_price=spot, contracts=contracts, as_of=NOW, source="test")


@pytest.fixture
def cfg():
    return get_methodology().contracts


@pytest.mark.parametrize(
    ("strategy", "direction", "expect_type", "n_legs"),
    [
        (StrategyType.LONG_CALL, Direction.BULLISH, OptionType.CALL, 1),
        (StrategyType.LONG_PUT, Direction.BEARISH, OptionType.PUT, 1),
        (StrategyType.BULL_CALL_SPREAD, Direction.BULLISH, OptionType.CALL, 2),
        (StrategyType.BEAR_PUT_SPREAD, Direction.BEARISH, OptionType.PUT, 2),
    ],
)
def test_each_allowed_strategy_selects(cfg, strategy, direction, expect_type, n_legs):
    s = select_structure(
        build_chain(spot=20.0),  # cheap underlying so everything sizes
        strategy,
        direction,
        cfg,
        candidate_id="c",
        run_id="r",
        now=NOW,
        max_risk_usd=100.0,
    )
    assert s.strategy_type is strategy
    assert len(s.legs) == n_legs
    assert all(leg.option_type is expect_type for leg in s.legs)
    assert s.legs[0].action is OptionAction.BUY_TO_OPEN


def test_a_disallowed_strategy_raises(cfg):
    with pytest.raises(NoContractError, match="not in the allowed set"):
        select_structure(
            build_chain(),
            StrategyType.IRON_CONDOR,
            Direction.BULLISH,
            cfg,
            candidate_id="c",
            run_id="r",
            now=NOW,
            max_risk_usd=100.0,
        )


def test_the_long_leg_delta_lands_inside_the_configured_band(cfg):
    s = select_structure(
        build_chain(spot=20.0), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    delta = abs(s.legs[0].greeks.delta)
    assert cfg.long_delta_min <= delta <= cfg.long_delta_max


def test_expiration_lands_inside_the_configured_dte_band(cfg):
    s = select_structure(
        build_chain(spot=20.0), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    dte = s.dte(NOW.date())
    assert cfg.preferred_dte_min <= dte <= cfg.preferred_dte_max


def test_a_bull_call_spread_sells_the_higher_strike(cfg):
    s = select_structure(
        build_chain(spot=20.0), StrategyType.BULL_CALL_SPREAD, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    long_leg, short_leg = s.legs
    assert short_leg.strike > long_leg.strike
    assert short_leg.action is OptionAction.SELL_TO_OPEN


def test_a_bear_put_spread_sells_the_lower_strike(cfg):
    s = select_structure(
        build_chain(spot=20.0), StrategyType.BEAR_PUT_SPREAD, Direction.BEARISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    long_leg, short_leg = s.legs
    assert short_leg.strike < long_leg.strike


def test_vertical_payoff_arithmetic_is_internally_consistent(cfg):
    s = select_structure(
        build_chain(spot=20.0), StrategyType.BULL_CALL_SPREAD, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    debit = s.net_debit_per_share
    assert s.max_loss_per_contract == pytest.approx(debit * 100, abs=0.01)
    assert s.max_profit_per_contract == pytest.approx((s.width - debit) * 100, abs=0.01)
    assert s.breakeven == pytest.approx(s.legs[0].strike + debit, abs=0.01)
    # Max loss plus max profit is the full width — the defining identity.
    assert s.max_loss_per_contract + s.max_profit_per_contract == pytest.approx(s.width * 100, abs=0.05)


def test_a_long_option_reports_unbounded_profit_rather_than_inventing_a_cap(cfg):
    s = select_structure(
        build_chain(spot=20.0), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    assert s.max_profit_per_contract is None
    assert s.reward_to_risk is None  # None, not a fabricated number


def test_position_size_never_exceeds_the_risk_budget(cfg):
    s = select_structure(
        build_chain(spot=20.0), StrategyType.BULL_CALL_SPREAD, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    assert s.total_max_loss <= 100.0
    assert s.contracts >= 1


def test_an_unsizeable_structure_reports_zero_contracts_rather_than_rounding_up(cfg):
    s = select_structure(
        build_chain(spot=500.0), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    assert s.max_loss_per_contract > 100.0
    assert s.contracts == 0
    assert any("unsizeable" in n for n in s.selection_notes)


def test_the_selector_prefers_a_sizeable_structure_over_a_richer_unsizeable_one(cfg):
    """Cheapness is not rewarded, but unaffordability is disqualifying."""
    chain = build_chain(spot=40.0)
    structures, _notes = propose_structures(
        chain, StrategyType.BULL_CALL_SPREAD, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
        allowed_strategies={"bull_call_spread", "long_call"},
    )
    assert structures
    assert structures[0].contracts >= 1
    assert structures[0].total_max_loss <= 100.0


def test_an_unsizeable_long_falls_back_to_a_defined_risk_spread(cfg):
    """A validated thesis is not discarded over an expression choice."""
    chain = build_chain(spot=60.0)
    structures, notes = propose_structures(
        chain, StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
        allowed_strategies={"long_call", "bull_call_spread"},
    )
    assert structures
    strategies = {s.strategy_type for s in structures}
    assert StrategyType.BULL_CALL_SPREAD in strategies
    assert any("could not be sized" in n for n in notes)


def test_the_fallback_never_loosens_the_risk_cap(cfg):
    """The fallback offers a cheaper structure, never a bigger budget."""
    structures, _ = propose_structures(
        build_chain(spot=60.0), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
        allowed_strategies={"long_call", "bull_call_spread"},
    )
    for s in structures:
        if s.contracts >= 1:
            assert s.total_max_loss <= 100.0


def test_the_fallback_respects_the_strategy_allow_list(cfg):
    structures, notes = propose_structures(
        build_chain(spot=500.0), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
        allowed_strategies={"long_call"},  # spread NOT allowed
    )
    assert all(s.strategy_type is StrategyType.LONG_CALL for s in structures)
    assert any("not in the configured allow-list" in n for n in notes)


def test_an_illiquid_chain_with_no_two_sided_market_yields_nothing(cfg):
    chain = build_chain(spot=20.0)
    for c in chain.contracts:
        c.bid = None
        c.ask = None
    with pytest.raises(NoContractError):
        select_structure(
            chain, StrategyType.LONG_CALL, Direction.BULLISH, cfg,
            candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
        )


def test_a_chain_with_no_expiry_in_band_yields_nothing(cfg):
    chain = build_chain(spot=20.0, dtes=(2, 3))  # all inside the 7-day minimum
    with pytest.raises(NoContractError, match="DTE band"):
        select_structure(
            chain, StrategyType.LONG_CALL, Direction.BULLISH, cfg,
            candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
        )


# --- Greek provenance ------------------------------------------------------


def test_missing_greeks_are_modeled_and_labeled_as_such(cfg):
    """CLAUDE.md: modeled is labeled."""
    s = select_structure(
        build_chain(spot=20.0), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    # The fixture supplies delta only, so gamma/theta/vega must be modeled.
    assert s.greeks_source is Provenance.MODELED
    assert s.net_theta is not None
    assert s.net_vega is not None


def test_strike_invariant_greeks_are_detected():
    """A gamma that is the same at every strike is not a measurement."""
    flat = build_chain(flat_greeks=True)
    assert strike_invariant_greeks(flat) == {"gamma", "theta", "vega"}

    varied = build_chain(flat_greeks=False)
    # No gamma/theta/vega supplied at all -> nothing to call invariant.
    assert strike_invariant_greeks(varied) == set()


def test_a_spread_built_from_flat_greeks_does_not_report_zero_net_theta(cfg):
    """The bug this check exists to prevent.

    With strike-invariant provider Greeks, long minus short cancels exactly and
    a vertical reports net theta of 0.0 — which makes the theta-burden rule pass
    for free and the excessive-theta hard rule unfireable.
    """
    s = select_structure(
        build_chain(spot=20.0, flat_greeks=True),
        StrategyType.BULL_CALL_SPREAD,
        Direction.BULLISH,
        cfg,
        candidate_id="c",
        run_id="r",
        now=NOW,
        max_risk_usd=100.0,
    )
    assert s.net_theta is not None
    assert s.net_theta != 0.0
    assert s.greeks_source is Provenance.MODELED


def test_a_partial_greek_set_does_not_produce_a_fabricated_net(cfg):
    """A net summed over legs with a missing component would be a made-up total."""
    chain = build_chain(spot=20.0)
    for c in chain.contracts:
        c.greeks = Greeks()  # nothing at all
        c.implied_volatility = None  # and no basis to model from
    with pytest.raises(NoContractError):
        select_structure(
            chain, StrategyType.LONG_CALL, Direction.BULLISH, cfg,
            candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
        )


# --- derived figures -------------------------------------------------------


def test_cost_drag_reflects_a_round_trip_across_the_spread(cfg):
    s = select_structure(
        build_chain(spot=20.0, spread_frac=0.10), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    tight = select_structure(
        build_chain(spot=20.0, spread_frac=0.01), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    assert s.cost_drag_pct > tight.cost_drag_pct


def test_probability_of_profit_is_computed_or_explicitly_absent(cfg):
    s = select_structure(
        build_chain(spot=20.0), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    assert s.probability_of_profit is not None
    assert 0.0 <= s.probability_of_profit <= 1.0
    assert s.pop_source is Provenance.MODELED


def test_spread_pct_uses_the_mid_not_the_ask(cfg):
    """Dividing by the ask flatters a wide market."""
    s = select_structure(
        build_chain(spot=20.0, spread_frac=0.10), StrategyType.LONG_CALL, Direction.BULLISH, cfg,
        candidate_id="c", run_id="r", now=NOW, max_risk_usd=100.0,
    )
    leg = s.legs[0]
    mid = (leg.bid + leg.ask) / 2
    assert leg.spread_pct == pytest.approx((leg.ask - leg.bid) / mid, abs=1e-4)


def test_worst_leg_spread_is_none_when_any_leg_lacks_a_market():
    from app.multiagent.models.contracts import ProposedLeg, ProposedStructure

    s = ProposedStructure(
        structure_id="s", candidate_id="c", run_id="r", ticker="T",
        strategy_type=StrategyType.LONG_CALL,
        legs=[
            ProposedLeg(
                underlying="T", expiration=date(2026, 9, 4), strike=100.0,
                option_type=OptionType.CALL, action=OptionAction.BUY_TO_OPEN,
                bid=None, ask=None, as_of=NOW,
            )
        ],
        selected_at=NOW,
    )
    assert s.worst_leg_spread_pct is None  # unknown, not zero
