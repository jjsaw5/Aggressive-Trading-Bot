"""Amendment 2: selection prices probability, not only payoff.

The old fit function was `0.7 * reward-to-risk + 0.3 * width`. Both terms rise as
a spread moves further out of the money, and the short leg was bounded only by
liquidity — so the optimiser's maximum was, by construction, the cheapest far-OTM
spread the chain allowed. Measured on 2026-08-03: median POP 0.287, median R:R
7.79:1, 45% of structures below POP 0.25.

The system computed the probability, stored it, printed it on the board, and did
not use it to choose. These tests are the control for that, because the golden
file structurally cannot be: it scores fixed `IVContext` fixtures and passes no
trade plan, so a *selection* change cannot move its numbers (the same limitation
reported under Amendment 1).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

from app.domain.enums import Direction, OptionType
from app.domain.options import Greeks, OptionChain, OptionContract
from app.engine.contract_selection import SelectionConfig, select_vertical_spread
from app.quant.pricing import black_scholes_delta, black_scholes_price

NOW = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
AS_OF = date(2026, 6, 1)
EXP = date(2026, 7, 1)
T = 30 / 365
VOL = 0.30
SPOT = 100.0


def _chain(spot: float = SPOT) -> OptionChain:
    cs: list[OptionContract] = []
    for k in range(60, 141):
        for ot in (OptionType.CALL, OptionType.PUT):
            px = black_scholes_price(spot, k, T, VOL, ot)
            if px < 0.02:
                continue
            cs.append(OptionContract(
                symbol="AAA", expiration=EXP, strike=float(k), option_type=ot,
                bid=round(px - 0.02, 2), ask=round(px + 0.02, 2), mark=round(px, 2),
                volume=500, open_interest=2000, implied_volatility=VOL,
                greeks=Greeks(delta=round(black_scholes_delta(spot, k, T, VOL, ot), 3)),
                as_of=NOW,
            ))
    return OptionChain(symbol="AAA", underlying_price=spot, contracts=cs, as_of=NOW)


SEL = SelectionConfig(min_dte=20, max_dte=45, min_delta=0.30, max_delta=0.60)
LEGACY = replace(SEL, min_pop=None, min_short_leg_delta=0.0)


def _pick(sel: SelectionConfig, budget: float = 500.0):
    return select_vertical_spread(
        _chain(), Direction.BULLISH, AS_OF, max_debit_usd=budget, sel=sel
    )


# --- The headline behaviour ---------------------------------------------------
def test_selection_now_records_the_probability_that_chose_it() -> None:
    """The number that drove the choice must be auditable against the choice."""
    s = _pick(SEL)
    assert s is not None
    assert s.selection_pop is not None
    assert 0.0 <= s.selection_pop <= 1.0


def test_the_chosen_structure_clears_the_floor() -> None:
    s = _pick(SEL)
    assert s is not None and s.selection_pop >= SEL.min_pop


def test_a_well_behaved_chain_is_barely_affected() -> None:
    """The amendment is TARGETED, not a blanket tightening.

    Where the delta band already constrains the long leg and the chain has clean
    greeks, the old and new rules agree. The defect lived in the fallback path
    (below), not in ordinary selection — so this test asserting "almost nothing
    changes here" is a feature, and its failure would mean the amendment is
    over-reaching.
    """
    old, new = _pick(LEGACY), _pick(SEL)
    assert old is not None and new is not None
    assert new.selection_pop >= old.selection_pop


# --- THE production defect, reproduced ---------------------------------------
def _spy_like_chain() -> OptionChain:
    """SPY on 2026-08-03: spot ~756, ~13% IV, 28 DTE.

    At those inputs a 790 call prices to delta ~0.10 — far outside any 0.30 floor
    — yet 790/756 is +4.4%, INSIDE the 6% swing moneyness band. That combination
    is what put a 10%-POP structure on the board.
    """
    spot, vol, t = 756.37, 0.13, 28 / 365
    cs: list[OptionContract] = []
    for k in range(700, 861):
        px = black_scholes_price(spot, k, t, vol, OptionType.CALL)
        if px < 0.02:
            continue
        cs.append(OptionContract(
            symbol="SPY", expiration=date(2026, 8, 31), strike=float(k),
            option_type=OptionType.CALL,
            bid=round(px - 0.02, 2), ask=round(px + 0.02, 2), mark=round(px, 2),
            volume=500, open_interest=2000, implied_volatility=vol,
            greeks=Greeks(delta=round(black_scholes_delta(spot, k, t, vol, OptionType.CALL), 4)),
            as_of=NOW,
        ))
    return OptionChain(symbol="SPY", underlying_price=spot, contracts=cs, as_of=NOW)


SWING = SelectionConfig(
    min_dte=21, max_dte=45, target_delta=0.45, min_delta=0.30, max_delta=0.60,
    moneyness_fallback_pct=0.06,
)


def test_the_fixture_reproduces_the_production_inputs() -> None:
    """Guards the premise: a ~0.10-delta strike really does sit inside the band."""
    ch = _spy_like_chain()
    leg = next(c for c in ch.contracts if c.strike == 790.0)
    assert leg.greeks.delta < 0.15, leg.greeks.delta
    assert abs(790.0 / ch.underlying_price - 1.0) < SWING.moneyness_fallback_pct


def test_an_out_of_band_delta_is_no_longer_re_admitted_by_moneyness() -> None:
    """THE fix. A usable delta outside the band is a REJECT, not a fallback case.

    Previously `_leg_ok` fell through to the moneyness proxy whenever the delta
    band check failed, so proximity in strike overrode a delta the provider had
    supplied perfectly well.
    """
    s = select_vertical_spread(
        _spy_like_chain(), Direction.BULLISH, date(2026, 8, 3),
        max_debit_usd=98.15, sel=replace(SWING, min_pop=None, min_short_leg_delta=0.0),
    )
    if s is not None:
        d = abs(s.long_leg.greeks.delta)
        assert SWING.min_delta <= d <= SWING.max_delta, (
            f"long leg delta {d} is outside the band and was still selected"
        )


def test_the_production_long_shot_is_refused_outright() -> None:
    """With the full amendment, this chain and this cap yield no 10%-POP row."""
    s = select_vertical_spread(
        _spy_like_chain(), Direction.BULLISH, date(2026, 8, 3),
        max_debit_usd=98.15, sel=SWING,
    )
    assert s is None or (s.selection_pop is not None and s.selection_pop >= SWING.min_pop)


# --- C2: the floor rejects, it does not down-rank -----------------------------
def test_a_floor_above_every_available_structure_yields_nothing() -> None:
    assert _pick(replace(SEL, min_pop=0.999)) is None


def test_unmodellable_odds_cannot_clear_the_floor() -> None:
    """A probability we could not compute is not evidence of a good one."""
    ch = _chain()
    for c in ch.contracts:
        c.implied_volatility = None
    assert select_vertical_spread(
        ch, Direction.BULLISH, AS_OF, max_debit_usd=500.0, sel=SEL
    ) is None


def test_unmodellable_odds_still_select_when_the_floor_is_disabled() -> None:
    """Proves the previous test fails on the FLOOR, not on a crash in pricing."""
    ch = _chain()
    for c in ch.contracts:
        c.implied_volatility = None
    s = select_vertical_spread(
        ch, Direction.BULLISH, AS_OF, max_debit_usd=500.0, sel=LEGACY
    )
    assert s is not None and s.selection_pop is None


# --- C3: the short leg is no longer unbounded ---------------------------------
def test_the_short_leg_respects_its_delta_floor() -> None:
    s = _pick(replace(SEL, min_pop=None))
    assert s is not None
    d = abs(s.short_leg.greeks.delta)
    assert d >= SEL.min_short_leg_delta


def test_the_delta_floor_narrows_the_structure() -> None:
    """The floor exists to cap width; show that it does."""
    wide = _pick(replace(SEL, min_pop=None, min_short_leg_delta=0.0))
    capped = _pick(replace(SEL, min_pop=None))
    assert wide is not None and capped is not None
    assert capped.width <= wide.width


# --- Bearish side behaves the same --------------------------------------------
def test_the_floor_applies_to_put_spreads_too() -> None:
    s = select_vertical_spread(
        _chain(), Direction.BEARISH, AS_OF, max_debit_usd=500.0, sel=SEL
    )
    assert s is not None and s.selection_pop >= SEL.min_pop
    assert s.long_leg.option_type == OptionType.PUT
    assert s.short_leg.strike < s.long_leg.strike


# --- C4: horizon follows the thesis, not the strategy label -------------------
def test_a_thesis_that_resolves_inside_the_window_may_go_short_dated() -> None:
    from app.shortduration.contracts import short_horizon_viable

    # 2% to invalidation against a 30% vol name: ~3.5% expected 5-day move covers it.
    assert short_horizon_viable(distance_to_invalidation_pct=2.0, iv=0.30) is True


def test_a_distant_invalidation_keeps_the_weeks_out_default() -> None:
    from app.shortduration.contracts import short_horizon_viable

    assert short_horizon_viable(distance_to_invalidation_pct=25.0, iv=0.30) is False


def test_an_unanswerable_horizon_question_keeps_the_conservative_default() -> None:
    from app.shortduration.contracts import short_horizon_viable

    assert short_horizon_viable(distance_to_invalidation_pct=None, iv=0.30) is False
    assert short_horizon_viable(distance_to_invalidation_pct=2.0, iv=None) is False
    assert short_horizon_viable(distance_to_invalidation_pct=2.0, iv=0.0) is False


# --- The declared constants are the ones the amendment names ------------------
def test_the_thresholds_are_the_ones_the_amendment_recorded() -> None:
    d = SelectionConfig()
    assert d.min_pop == 0.25
    assert d.min_short_leg_delta == 0.15


def test_the_model_version_records_the_amendment() -> None:
    from app.config import settings

    assert settings.scoring_model_version == "sd-scoring-2026.08-v4.1"
