"""The trade evaluator's rubric, pinned against fixtures.

The evaluator grades a trade the HUMAN proposes, with the account out of scope.
That makes two things worth testing hard, because both are claims the product
stance depends on:

  1. **The account really is out of scope.** The scanner's affordability limits
     must not reach the evaluator through a side door — and one of them wears a
     liquidity costume (`OptionLiquidityConfig.max_mid_price`, commented "keeps
     1-lot affordable for small acct").
  2. **A dimension with no data scores nothing, not zero.** A missing feed that
     silently contributed 0.0 would be indistinguishable from a measured
     failure, which is the whole "absent stays absent" rule (CLAUDE.md §4).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.config import settings
from app.domain.enums import OptionType
from app.domain.evaluation import (
    NA_NO_DATA,
    NA_NOT_IMPLEMENTED,
    StructureType,
    Verdict,
)
from app.domain.market import EarningsEvent
from app.domain.options import Greeks, IVContext, OptionChain, OptionContract
from app.engine.trade_evaluator import (
    EvaluationInputs,
    dim_alternative,
    dim_cost_structure,
    dim_iv_context,
    dim_liquidity,
    dim_probability,
    dim_timing,
    evaluate,
    grade_from,
    parse_horizon,
    price_structure,
    resolve_expiration,
)
from app.quant.pricing import black_scholes_delta, black_scholes_price
from app.risk.policy import RiskPolicy

NOW = datetime(2026, 8, 7, 15, 0, tzinfo=UTC)
AS_OF = date(2026, 8, 7)
SPOT = 100.0
VOL = 0.30
# Three listed expiries so horizon snapping has something to choose between.
EXPIRIES = [date(2026, 8, 7), date(2026, 8, 14), date(2026, 9, 18)]


def _chain(
    *, spot: float = SPOT, vol: float = VOL, spread: float = 0.02,
    oi: int = 2000, volume: int = 500,
) -> OptionChain:
    cs: list[OptionContract] = []
    for exp in EXPIRIES:
        t = max((exp - AS_OF).days, 0) / 365 or 1 / 365
        # Reaches down to 60 so the chain genuinely contains legs priced above
        # the scanner's $25/share affordability cap. Without those strikes the
        # budget-blindness test would pass without exercising anything.
        for k in range(60, 121):
            for ot in (OptionType.CALL, OptionType.PUT):
                px = black_scholes_price(spot, k, t, vol, ot)
                if px < 0.05:
                    continue
                cs.append(OptionContract(
                    symbol="AAA", expiration=exp, strike=float(k), option_type=ot,
                    bid=round(px - spread, 2), ask=round(px + spread, 2),
                    mark=round(px, 2), volume=volume, open_interest=oi,
                    implied_volatility=vol,
                    greeks=Greeks(delta=round(black_scholes_delta(spot, k, t, vol, ot), 4)),
                    as_of=NOW,
                ))
    return OptionChain(symbol="AAA", underlying_price=spot, contracts=cs, as_of=NOW)


def _inputs(**kw) -> EvaluationInputs:
    base = {
        "chain": _chain(),
        "iv": IVContext(symbol="AAA", iv30=0.30, iv_rank=0.20, hv20=0.28,
                        iv_rank_source="iv_history", as_of=NOW),
        "earnings": EarningsEvent(symbol="AAA", report_date=date(2026, 12, 1)),
        "spot": SPOT,
    }
    base.update(kw)
    return EvaluationInputs(**base)


# --- Horizon resolution -------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "days"),
    [("0d", 0), ("3d", 3), ("2w", 14), ("45d", 45), ("1m", 30), (" 5D ", 5)],
)
def test_horizon_shorthand_parses(raw: str, days: int) -> None:
    assert parse_horizon(raw) == (days, None)


def test_an_iso_date_is_taken_literally() -> None:
    assert parse_horizon("2026-09-18") == (None, date(2026, 9, 18))


@pytest.mark.parametrize("raw", ["", "soon", "3 fortnights", "-2d"])
def test_an_unreadable_horizon_is_a_gap_not_a_default(raw: str) -> None:
    """Guessing a default would silently evaluate a DIFFERENT trade."""
    assert parse_horizon(raw) == (None, None)


def test_the_resolved_expiry_is_reported_not_implied() -> None:
    """'3d' on one weekday and '3d' on another are different contracts."""
    exp, note = resolve_expiration(_chain(), "3d", as_of=AS_OF)
    assert exp == date(2026, 8, 7)  # nearest listed to 2026-08-10
    assert "nearest listed expiry" in note and str(exp) in note


def test_an_exact_listed_expiry_says_so() -> None:
    exp, note = resolve_expiration(_chain(), "2026-09-18", as_of=AS_OF)
    assert exp == date(2026, 9, 18) and "exact listed expiry" in note


def test_an_unreadable_horizon_resolves_to_nothing_with_a_reason() -> None:
    exp, note = resolve_expiration(_chain(), "whenever", as_of=AS_OF)
    assert exp is None and "could not read a horizon" in note


# --- Pricing ------------------------------------------------------------------
def test_a_call_debit_spread_prices_to_the_arithmetic() -> None:
    s, err = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=100.0, short_strike=105.0, as_of=AS_OF,
    )
    assert err == "" and s is not None
    assert s.width == 5.0
    assert s.max_loss_usd == pytest.approx(s.net_debit_per_share * 100, abs=0.01)
    assert s.max_profit_usd == pytest.approx((5.0 - s.net_debit_per_share) * 100, abs=0.01)
    assert s.breakeven == pytest.approx(100.0 + s.net_debit_per_share, abs=1e-4)
    assert 0.0 < s.probability_of_profit < 1.0


def test_an_inverted_spread_is_refused_with_a_reason() -> None:
    """Short below long on a CALL debit spread is a credit structure, not a typo
    to be silently corrected."""
    s, err = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=105.0, short_strike=100.0, as_of=AS_OF,
    )
    assert s is None and "must be ABOVE" in err


def test_an_unlisted_strike_is_a_reason_not_a_crash() -> None:
    s, err = price_structure(
        chain=_chain(), structure=StructureType.LONG_CALL,
        expiration=date(2026, 9, 18), long_strike=999.0, short_strike=None, as_of=AS_OF,
    )
    assert s is None and "no listed" in err


def test_the_marketable_cost_is_worse_than_the_mid() -> None:
    """Buy the ask, sell the bid. This gap is what the liquidity dimension taxes."""
    s, _ = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=100.0, short_strike=105.0, as_of=AS_OF,
    )
    assert s.marketable_debit_per_share > s.net_debit_per_share


def test_a_missing_book_side_yields_no_marketable_cost() -> None:
    """A fill cost we cannot observe is not the same as a cheap one."""
    ch = _chain()
    for c in ch.contracts:
        c.ask = None
    s, _ = price_structure(
        chain=ch, structure=StructureType.LONG_CALL, expiration=date(2026, 9, 18),
        long_strike=100.0, short_strike=None, as_of=AS_OF,
    )
    assert s is not None and s.marketable_debit_per_share is None


# --- The account is genuinely out of scope ------------------------------------
def test_an_expensive_structure_is_evaluated_not_rejected() -> None:
    """THE point of the feature. A deep-ITM call costs far more than the $100
    per-trade cap and the scanner would never surface it; the evaluator must
    still price and grade it."""
    ev = evaluate(
        symbol="AAA", structure=StructureType.LONG_CALL, horizon="2026-09-18",
        inputs=_inputs(), long_strike=65.0, now=NOW,
    )
    assert ev.proposed is not None
    cap = RiskPolicy.from_settings().max_trade_risk_usd
    assert ev.proposed.max_loss_usd > cap, (
        f"fixture no longer exceeds the ${cap:.0f} per-trade cap"
    )
    assert ev.grade != ""


def test_the_alternative_is_selected_without_a_budget_cap() -> None:
    """`max_mid_price=25.0` is an affordability limit wearing a liquidity
    costume. If it leaked in, the selector could not return a leg priced above
    $25/share on a chain where those are the sensible ones."""
    ev = evaluate(
        symbol="AAA", structure=StructureType.LONG_CALL, horizon="2026-09-18",
        inputs=_inputs(), now=NOW,
    )
    assert ev.alternative is not None
    # Sanity: the chain really does contain legs the scanner's cap would exclude.
    dear = [c for c in _chain().contracts
            if c.expiration == date(2026, 9, 18) and c.mid and c.mid > 25.0]
    assert dear, "fixture no longer exercises the affordability cap"


# --- Absent stays absent ------------------------------------------------------
def test_a_dimension_without_data_scores_nothing_not_zero() -> None:
    """A missing feed must not be indistinguishable from a measured failure."""
    d = dim_iv_context(None)
    assert d.verdict == Verdict.NOT_ASSESSED
    assert d.score is None
    assert d.unavailable == NA_NO_DATA


def test_unassessed_dimensions_are_excluded_from_the_composite() -> None:
    """Two evaluations differing only in whether IV was available must not
    differ in grade because of a phantom zero."""
    with_iv = evaluate(symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD,
                       horizon="2026-09-18", inputs=_inputs(), now=NOW)
    no_iv = evaluate(symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD,
                     horizon="2026-09-18", inputs=_inputs(iv=None), now=NOW)
    assert no_iv.dimensions_assessed == with_iv.dimensions_assessed - 1
    # The IV dimension scored well here, so dropping it moves the composite a
    # little; a phantom 0.0 would have moved it a lot further down.
    assert no_iv.composite is not None
    assert abs(no_iv.composite - with_iv.composite) < 0.15


def test_the_report_always_states_how_many_dimensions_it_saw() -> None:
    """A B over four of six is not the same claim as a B over six."""
    ev = evaluate(symbol="AAA", structure=StructureType.LONG_CALL,
                  horizon="2026-09-18", inputs=_inputs(iv=None, earnings=None), now=NOW)
    assert ev.dimensions_total == 6
    assert 0 < ev.dimensions_assessed < 6


def test_unmodellable_odds_are_not_evidence_of_good_odds() -> None:
    ch = _chain()
    for c in ch.contracts:
        c.implied_volatility = None
    ev = evaluate(symbol="AAA", structure=StructureType.LONG_CALL,
                  horizon="2026-09-18", inputs=_inputs(chain=ch), long_strike=100.0, now=NOW)
    prob = next(d for d in ev.dimensions if d.key == "probability")
    assert prob.verdict == Verdict.NOT_ASSESSED and prob.score is None


# --- Dimension behaviour ------------------------------------------------------
def test_cost_drag_penalises_paying_most_of_the_width() -> None:
    cheap, _ = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=100.0, short_strike=120.0, as_of=AS_OF,
    )
    dear, _ = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=85.0, short_strike=86.0, as_of=AS_OF,
    )
    d_cheap = dim_cost_structure(cheap, SPOT)
    d_dear = dim_cost_structure(dear, SPOT)
    assert cheap.net_debit_per_share / cheap.width < dear.net_debit_per_share / dear.width
    assert d_cheap.score > d_dear.score


def test_a_single_leg_is_judged_on_extrinsic_not_width() -> None:
    """There is no width on a single long leg, so cost drag is meaningless; the
    real cost question is how much of the premium decays to zero."""
    s, _ = price_structure(
        chain=_chain(), structure=StructureType.LONG_CALL, expiration=date(2026, 9, 18),
        long_strike=100.0, short_strike=None, as_of=AS_OF,
    )
    d = dim_cost_structure(s, SPOT)
    assert d.verdict != Verdict.NOT_ASSESSED
    assert any("Extrinsic" in f.label for f in d.facts)
    assert any(f.value == "uncapped" for f in d.facts)


def test_a_nearer_strike_models_better_odds() -> None:
    """Probability is the heaviest single dimension; pin its direction directly
    rather than only through the composite."""
    near, _ = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=95.0, short_strike=100.0, as_of=AS_OF,
    )
    far, _ = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=115.0, short_strike=120.0, as_of=AS_OF,
    )
    d_near = dim_probability(near, "AAA", SPOT)
    d_far = dim_probability(far, "AAA", SPOT)
    assert near.probability_of_profit > far.probability_of_profit
    assert d_near.score > d_far.score
    assert d_far.verdict == Verdict.FAIL
    # The plain-English ask must be present and name the actual break-even.
    assert any("must rise" in f.value for f in d_far.facts)


def test_the_probability_dimension_states_its_model() -> None:
    """'Modelled is labeled' — the number must carry how it was produced."""
    s, _ = price_structure(
        chain=_chain(), structure=StructureType.LONG_CALL, expiration=date(2026, 9, 18),
        long_strike=100.0, short_strike=None, as_of=AS_OF,
    )
    d = dim_probability(s, "AAA", SPOT)
    assert any("Black-Scholes" in f.note for f in d.facts)


def test_a_wide_market_fails_liquidity() -> None:
    wide, _ = price_structure(
        chain=_chain(spread=1.50), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=100.0, short_strike=105.0, as_of=AS_OF,
    )
    tight, _ = price_structure(
        chain=_chain(spread=0.01), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=100.0, short_strike=105.0, as_of=AS_OF,
    )
    assert dim_liquidity(wide).score < dim_liquidity(tight).score
    assert dim_liquidity(wide).verdict == Verdict.FAIL


def test_high_iv_rank_counts_against_a_debit_structure() -> None:
    """Polarity is easy to get backwards: these are LONG vol."""
    cheap = dim_iv_context(IVContext(symbol="AAA", iv30=0.3, iv_rank=0.10, as_of=NOW))
    rich = dim_iv_context(IVContext(symbol="AAA", iv30=0.3, iv_rank=0.90, as_of=NOW))
    assert cheap.score > rich.score


def test_earnings_inside_the_window_is_flagged() -> None:
    s, _ = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=100.0, short_strike=105.0, as_of=AS_OF,
    )
    inside = dim_timing(s, EarningsEvent(symbol="AAA", report_date=date(2026, 9, 1)), AS_OF)
    outside = dim_timing(s, EarningsEvent(symbol="AAA", report_date=date(2026, 12, 1)), AS_OF)
    assert inside.score < outside.score
    assert "INSIDE" in " ".join(f.note for f in inside.facts)


def test_an_unknown_earnings_date_is_not_treated_as_no_event() -> None:
    """Absence of a date is not evidence of absence of an event."""
    s, _ = price_structure(
        chain=_chain(), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=100.0, short_strike=105.0, as_of=AS_OF,
    )
    d = dim_timing(s, None, AS_OF)
    assert d.verdict == Verdict.ACCEPTABLE  # not STRONG
    assert any(f.value == NA_NO_DATA for f in d.facts)


def test_a_zero_dte_structure_is_marked_observation_only() -> None:
    s, _ = price_structure(
        chain=_chain(), structure=StructureType.LONG_CALL, expiration=date(2026, 8, 7),
        long_strike=100.0, short_strike=None, as_of=AS_OF,
    )
    d = dim_timing(s, None, AS_OF)
    assert any("OBSERVATION ONLY" in f.note for f in d.facts)


# --- The contrast dimension ---------------------------------------------------
def test_no_supplied_strikes_means_there_is_nothing_to_contrast() -> None:
    """'No gap' and 'no comparison' are different statements."""
    ev = evaluate(symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD,
                  horizon="2026-09-18", inputs=_inputs(), now=NOW)
    d = next(x for x in ev.dimensions if x.key == "alternative")
    assert d.verdict == Verdict.NOT_ASSESSED
    assert d.unavailable == NA_NOT_IMPLEMENTED
    assert ev.graded == "alternative"


def test_matching_the_selector_scores_full_marks() -> None:
    ev = evaluate(symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD,
                  horizon="2026-09-18", inputs=_inputs(), now=NOW)
    alt = ev.alternative
    assert alt is not None
    same = evaluate(
        symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD, horizon="2026-09-18",
        inputs=_inputs(), long_strike=alt.legs[0].strike,
        short_strike=alt.legs[1].strike, now=NOW,
    )
    d = next(x for x in same.dimensions if x.key == "alternative")
    assert d.verdict == Verdict.STRONG and d.score == 1.0


def test_a_far_otm_long_shot_is_told_what_to_use_instead() -> None:
    """The headline behaviour: 'here is what you can do to make it better' must
    name a concrete structure, not offer advice."""
    ev = evaluate(
        symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD, horizon="2026-09-18",
        inputs=_inputs(), long_strike=115.0, short_strike=120.0, now=NOW,
    )
    d = next(x for x in ev.dimensions if x.key == "alternative")
    assert d.verdict in (Verdict.WEAK, Verdict.FAIL)
    swap = [i for i in ev.improvements if i.dimension == "alternative"]
    assert swap and "at the same" in swap[0].suggestion
    assert "POP" in swap[0].impact


def test_the_contrast_compares_at_the_same_expiry() -> None:
    """Suggesting a different expiry answers a question that was not asked."""
    ev = evaluate(
        symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD, horizon="2026-09-18",
        inputs=_inputs(), long_strike=115.0, short_strike=120.0, now=NOW,
    )
    assert ev.alternative.expiration == ev.proposed.expiration == date(2026, 9, 18)


# --- Grading ------------------------------------------------------------------
def test_nothing_assessed_yields_no_grade_rather_than_an_f() -> None:
    """An F is a measurement. No grade is the honest output when nothing could
    be measured, and the two must not be conflated."""
    grade, composite, n = grade_from([
        dim_iv_context(None), dim_alternative(None, None),
    ])
    assert grade == "" and composite is None and n == 0


def test_one_failing_dimension_caps_the_grade() -> None:
    """A structure that fails a hard bar is not rescued by averaging."""
    wide = dim_liquidity(price_structure(
        chain=_chain(spread=1.50), structure=StructureType.CALL_DEBIT_SPREAD,
        expiration=date(2026, 9, 18), long_strike=100.0, short_strike=105.0, as_of=AS_OF,
    )[0])
    strong = dim_iv_context(IVContext(symbol="AAA", iv30=0.3, iv_rank=0.05, as_of=NOW))
    assert wide.verdict == Verdict.FAIL
    grade, _, _ = grade_from([wide, strong, strong, strong])
    assert grade in ("D", "F")


def test_the_grade_always_carries_its_disclaimer() -> None:
    ev = evaluate(symbol="AAA", structure=StructureType.LONG_CALL,
                  horizon="2026-09-18", inputs=_inputs(), long_strike=100.0, now=NOW)
    assert "UNCALIBRATED" in ev.grade_claim
    assert "not a prediction" in ev.grade_claim
    assert ev.evaluator_version == settings.trade_eval_version


def test_the_evaluator_version_is_not_the_frozen_scoring_version() -> None:
    """Borrowing `scoring_model_version` would make an evaluator change look like
    a change to the shipped, frozen scoring model."""
    assert settings.trade_eval_version != settings.scoring_model_version
    assert settings.trade_eval_version.startswith("trade-eval-")


# --- Degradation --------------------------------------------------------------
def test_no_chain_reports_a_gap_rather_than_a_grade() -> None:
    ev = evaluate(symbol="AAA", structure=StructureType.LONG_CALL, horizon="3d",
                  inputs=EvaluationInputs(), now=NOW)
    assert ev.grade == "" and ev.composite is None
    assert "chain" in ev.errors
    assert all(d.verdict == Verdict.NOT_ASSESSED for d in ev.dimensions)


def test_a_bad_proposed_structure_still_returns_the_alternative() -> None:
    """A typo in the strikes should teach you what WAS available, not 500."""
    ev = evaluate(
        symbol="AAA", structure=StructureType.CALL_DEBIT_SPREAD, horizon="2026-09-18",
        inputs=_inputs(), long_strike=999.0, short_strike=1000.0, now=NOW,
    )
    assert ev.proposed is None
    assert "no listed" in ev.errors["proposed"]
    assert ev.alternative is not None and ev.graded == "alternative"
