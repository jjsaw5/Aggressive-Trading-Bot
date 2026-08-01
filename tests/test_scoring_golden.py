"""Golden-file regression: fixed inputs, asserted composite outputs.

REQUIRED, not optional — reclassified by reviewer Ruling 1 on the strength of
FINDING_01, which demonstrated the failure mode this test exists to catch.

`test_scoring_freeze.py` blocks scoring modules from IMPORTING capture-only
data. That is necessary and insufficient. FINDING_01 found a scoring input
(`IVContext.term_structure_slope`) that was permanently `None` in production
because the provider never populated it, while the frozen scorer read it and
applied a 0.85 backwardation penalty. Populating that field — an edit entirely
outside `app/shortduration/scoring/` — would have changed every composite score
with no import to detect and no diff in any scoring file.

This test closes that hole from the other side. It asserts what the scorer
OUTPUTS for pinned inputs, so any change to what the scorer receives shows up as
a failing expectation regardless of which module caused it.

## How to work with this file

If a change here fails, that is the test doing its job. Ask, in order:

1. Was the change to scoring behaviour INTENTIONAL and APPROVED? During the
   capture window, `CAPTURE_WINDOW_PREREGISTRATION.md` §2 says the answer is
   almost always no.
2. If yes: bump `scoring_model_version`, amend the pre-registration under §8
   with a date and reason, and regenerate `tests/golden/scoring_v3.json` in the
   SAME commit, documenting the delta.
3. If no: you have found an accidental scoring change. Do not regenerate.

Regenerate with:  python -m tests.test_scoring_golden --regenerate
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.domain.enums import Direction, DTECategory, OptionType, ShortDurationStrategy
from app.domain.market import Candle, PriceHistory, Quote
from app.domain.options import Greeks, IVContext, OptionChain, OptionContract
from app.domain.shortduration import ShortDurationRegime, ShortDurationRegimeState
from app.shortduration.scoring.engine import score_candidate
from app.shortduration.strategies.base import SetupContext, StrategyDetection

GOLDEN = Path(__file__).parent / "golden" / "scoring_v3.json"

_NOW = datetime(2026, 8, 3, 15, 0, tzinfo=UTC)
_EXP = date(2026, 8, 21)


def _chain() -> OptionChain:
    return OptionChain(
        symbol="SPY", underlying_price=500.0, as_of=_NOW, source="fixture",
        contracts=[
            OptionContract(
                symbol="SPY", option_symbol=f"SPY{k}", expiration=_EXP, strike=float(k),
                option_type=OptionType.CALL, bid=2.0, ask=2.1, mark=2.05, last=2.05,
                volume=1500, open_interest=6000, implied_volatility=0.24,
                greeks=Greeks(), as_of=_NOW, source="fixture",
            )
            for k in (495, 500, 505, 510)
        ],
    )


def _iv(**kw) -> IVContext:
    base = {
        "symbol": "SPY", "iv30": 0.24, "iv_rank": 0.35, "iv_percentile": 0.38,
        "hv20": 0.20, "term_structure_slope": None, "iv_skew": None,
        "iv_rank_source": "iv_history", "as_of": _NOW, "source": "fixture",
    }
    base.update(kw)
    return IVContext(**base)


def _ctx() -> SetupContext:
    closes = [480.0 + i * 0.7 for i in range(60)]
    return SetupContext(
        symbol="SPY", now=_NOW,
        regime=ShortDurationRegimeState(
            regime=ShortDurationRegime.BULL_TREND, confidence=0.7, as_of=_NOW,
        ),
        change_pct=0.8,
        daily=PriceHistory(
            symbol="SPY", source="fixture",
            candles=[
                Candle(ts=datetime(2026, 5, 1, tzinfo=UTC), open=c, high=c + 1,
                       low=c - 1, close=c, volume=1_000_000)
                for c in closes
            ],
        ),
        quote=Quote(symbol="SPY", price=500.0, bid=499.9, ask=500.1, volume=1_000_000,
                    prev_close=496.0, as_of=_NOW, delayed_minutes=0, source="fixture"),
    )


def _det(direction: Direction = Direction.BULLISH) -> StrategyDetection:
    return StrategyDetection(
        strategy=ShortDurationStrategy.TREND_CONTINUATION,
        dte_category=DTECategory.SHORT_DTE, direction=direction,
        setup_score=0.7, entry_trigger="fixture trigger", invalidation="fixture invalidation",
        reasons=["fixture"], targets=[505.0],
    )


# Each case pins one axis of scorer behaviour.
#
# NOTE on the `term_structure_*` cases: they pin what the SCORER does with a
# given slope. They do NOT detect the provider starting or stopping supplying
# one — the inputs here are fixed by construction, so a provider change cannot
# move these numbers. That boundary is covered by
# tests/test_provider_scoring_contract.py. FINDING_01 lived exactly between the
# two, which is why both exist.
CASES: dict[str, dict] = {
    "baseline_bullish_no_term_slope": {"iv": {}, "direction": "bullish"},
    "term_structure_backwardated": {"iv": {"term_structure_slope": -0.05}, "direction": "bullish"},
    "term_structure_contango": {"iv": {"term_structure_slope": 0.04}, "direction": "bullish"},
    "term_structure_flat_within_deadband": {"iv": {"term_structure_slope": -0.005}, "direction": "bullish"},
    "hv_proxy_rank_is_discounted": {"iv": {"iv_rank_source": "hv_proxy"}, "direction": "bullish"},
    "rich_iv_crush_risk": {"iv": {"iv_rank": 0.85, "iv_percentile": 0.88}, "direction": "bullish"},
    "very_low_iv": {"iv": {"iv_rank": 0.10, "iv_percentile": 0.12}, "direction": "bullish"},
    "bearish_direction": {"iv": {}, "direction": "bearish"},
}


def _score(case: dict):
    d = Direction.BULLISH if case["direction"] == "bullish" else Direction.BEARISH
    return score_candidate(_ctx(), _det(d), chain=_chain(), iv=_iv(**case["iv"]))


def _observed() -> dict:
    out = {}
    for name, case in CASES.items():
        card = _score(case)
        out[name] = {
            "total": round(card.total, 4),
            "overall_confidence": round(card.overall_confidence, 4),
            "data_quality": round(card.data_quality, 4),
            "model_version": card.model_version,
            # A component may legitimately be None (unavailable input). None and
            # 0.0 are different facts, so None is preserved rather than coerced.
            "components": {
                k: (round(v.value, 4) if v.value is not None else None)
                for k, v in sorted(card.components.items())
            },
        }
    return out


def _load() -> dict:
    if not GOLDEN.exists():
        pytest.fail(
            f"{GOLDEN} is missing. Generate it with:\n"
            "  python -m tests.test_scoring_golden --regenerate"
        )
    return json.loads(GOLDEN.read_text())


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_composite_score_matches_the_golden_reference(case_name: str) -> None:
    """A changed number here is a changed model. See this file's docstring."""
    expected = _load()["cases"][case_name]
    actual = _observed()[case_name]
    assert actual["total"] == pytest.approx(expected["total"], abs=1e-4), (
        f"composite score for {case_name!r} moved "
        f"{expected['total']} -> {actual['total']}. If intentional: bump "
        "scoring_model_version, amend the pre-registration (§8), and regenerate "
        "this file in the same commit."
    )


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_every_component_matches_the_golden_reference(case_name: str) -> None:
    """Component-level, so a change is localised rather than merely detected."""
    expected = _load()["cases"][case_name]["components"]
    actual = _observed()[case_name]["components"]
    assert actual == pytest.approx(expected, abs=1e-4)


def test_the_golden_file_records_the_model_version_it_was_generated_under() -> None:
    """A golden file from a different model version proves nothing about this one."""
    from app.config import settings

    assert _load()["model_version"] == settings.scoring_model_version, (
        "The golden reference was generated under a different scoring_model_version. "
        "Regenerate it, and treat the version change as a pre-registration amendment."
    )


def test_the_backwardation_penalty_is_actually_wired() -> None:
    """The specific behaviour FINDING_01 showed had never executed in production.

    Guards against the branch being deleted rather than fixed: if backwardation
    stops mattering, this fails loudly instead of silently scoring the same.
    """
    flat = _score(CASES["term_structure_flat_within_deadband"]).total
    back = _score(CASES["term_structure_backwardated"]).total
    assert back < flat, (
        "A backwardated term structure no longer penalises the score. The "
        "IV-crush guard at scoring/components.py:137 is the intended behaviour."
    )


def test_a_null_term_slope_scores_the_same_as_a_flat_one() -> None:
    """Absence must not be treated as a signal in either direction."""
    none_case = _score(CASES["baseline_bullish_no_term_slope"]).total
    flat = _score(CASES["term_structure_flat_within_deadband"]).total
    assert none_case == pytest.approx(flat, abs=1e-4)


def test_time_of_day_is_not_an_input_to_the_scorer() -> None:
    """Ruling 5: time_of_day_bucket is LOGGED as a feature and must not be SCORED.

    Same clock-independence check the golden cases rely on: scoring the identical
    setup at two different times of day must produce the identical score.
    """
    import dataclasses

    morning = dataclasses.replace(_ctx(), now=datetime(2026, 8, 3, 14, 0, tzinfo=UTC))
    power_hour = dataclasses.replace(_ctx(), now=datetime(2026, 8, 3, 19, 45, tzinfo=UTC))
    a = score_candidate(morning, _det(), chain=_chain(), iv=_iv())
    b = score_candidate(power_hour, _det(), chain=_chain(), iv=_iv())
    assert a.total == pytest.approx(b.total, abs=1e-4), (
        "The composite score changed with time of day. time_of_day_bucket is a "
        "logged feature only; it must not enter scoring."
    )


def _regenerate() -> None:
    from app.config import settings

    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            "Golden scoring reference. Regenerating this file is a MODEL CHANGE. "
            "Bump scoring_model_version and amend CAPTURE_WINDOW_PREREGISTRATION.md "
            "§8 in the same commit. See tests/test_scoring_golden.py."
        ),
        "model_version": settings.scoring_model_version,
        "cases": _observed(),
    }
    GOLDEN.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN} under model_version={settings.scoring_model_version}")


if __name__ == "__main__":
    import sys

    if "--regenerate" in sys.argv:
        _regenerate()
    else:
        print(__doc__)
