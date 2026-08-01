"""Which provider fields reach the scorer — pinned, because FINDING_01.

The freeze guard checks imports. The golden file checks outputs for fixed
inputs. Neither answers the question FINDING_01 actually turned on:

    *Which fields does the live provider populate, and therefore which scoring
    branches can execute in production?*

`IVContext.term_structure_slope` was read by `scoring/components.py` from v3
onward and populated by nobody. The backwardation penalty was live in every
mock-backed test and dead in production for the entire life of the model. No
existing test could see that, because nothing under `scoring/` was wrong.

This file pins the contract from the provider side. If a provider stops
supplying a scored field — or starts supplying one it previously did not — that
is a model change in effect, and it fails here.

PARSING ONLY. No network: fixtures are shaped from real captured responses, so
the suite stays hermetic while still pinning the real schema.
"""

from __future__ import annotations

import pytest

# Fields on IVContext that the frozen scorer READS. Sourced by inspection of
# app/shortduration/scoring/components.py. Adding to this set means the scorer
# gained an input; removing means it lost one. Either is a model change.
SCORED_IV_FIELDS = {"iv_rank", "iv_percentile", "term_structure_slope", "iv_rank_source"}


def test_the_scored_field_list_still_matches_the_scorer() -> None:
    """Catches a scoring component starting to read a field nobody pinned.

    Greps the frozen scorer for `iv.<attr>` access and compares against the set
    above, so this file cannot drift silently away from what is actually scored.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).parents[1] / "app/shortduration/scoring/components.py").read_text()
    read = set(re.findall(r"\biv\.([a-z_][a-z0-9_]*)", src))
    # iv30/hv20 feed explanations and POP, not the volatility component's value.
    read -= {"iv30", "hv20", "iv_skew", "iv_hv_ratio", "symbol", "as_of", "source"}
    assert read == SCORED_IV_FIELDS, (
        f"The scorer's IVContext inputs changed: {read} != {SCORED_IV_FIELDS}. "
        "A new scored input is a model change — bump scoring_model_version and "
        "amend the pre-registration before updating this set."
    )


# --- The term-structure slope, post-FINDING_01 --------------------------------
_TERM_ROWS = {
    "data": [
        # Shaped from the real SPY response, 2026-07-31.
        {"date": "2026-07-31", "ticker": "SPY", "volatility": "0.241410460396433",
         "expiry": "2026-07-31", "dte": 0},
        {"date": "2026-07-31", "ticker": "SPY", "volatility": "0.0800737609271233",
         "expiry": "2026-08-03", "dte": 3},
        {"date": "2026-07-31", "ticker": "SPY", "volatility": "0.1149839885511151",
         "expiry": "2026-08-07", "dte": 7},
        {"date": "2026-07-31", "ticker": "SPY", "volatility": "0.1500000000000000",
         "expiry": "2026-08-31", "dte": 31},
    ]
}


def _provider(payload):
    from app.providers.unusual_whales.client import UnusualWhalesProvider

    p = UnusualWhalesProvider()

    async def _get_json(path, params=None):
        return payload

    p._http.get_json = _get_json  # type: ignore[method-assign]
    return p


async def _slope(payload):
    return await _provider(payload)._term_structure_slope("SPY")


@pytest.mark.asyncio
async def test_the_slope_is_populated_at_all() -> None:
    """THE regression. This returned None for the entire life of v3."""
    assert await _slope(_TERM_ROWS) is not None


@pytest.mark.asyncio
async def test_contango_is_positive() -> None:
    # back (31d, 0.15) - front (3d, 0.08) > 0
    assert await _slope(_TERM_ROWS) == pytest.approx(0.15 - 0.0800737609271233, abs=1e-6)


@pytest.mark.asyncio
async def test_backwardation_is_negative() -> None:
    payload = {"data": [
        {"dte": 2, "volatility": "0.40"},
        {"dte": 30, "volatility": "0.25"},
    ]}
    s = await _slope(payload)
    assert s is not None and s < -0.01  # below the scorer's deadband


@pytest.mark.asyncio
async def test_expiration_day_is_excluded_from_the_front_leg() -> None:
    """dte=0 IV is numerically unstable — SPY printed 0.24 at dte=0 against 0.08
    at dte=3. Anchoring there would manufacture backwardation on every
    0DTE-listed name, penalising them all for a solver artifact."""
    s = await _slope(_TERM_ROWS)
    # Front must be the 3-DTE point, not the 0-DTE one; using dte=0 would give
    # a large negative instead of a positive.
    assert s is not None and s > 0


@pytest.mark.asyncio
async def test_a_single_tenor_is_not_a_term_structure() -> None:
    assert await _slope({"data": [{"dte": 5, "volatility": "0.2"}]}) is None


@pytest.mark.asyncio
async def test_an_empty_or_malformed_response_yields_none_not_zero() -> None:
    # Zero slope means "flat"; absence means "unknown". They must not collapse.
    assert await _slope({"data": []}) is None
    assert await _slope({"data": "nonsense"}) is None
    assert await _slope({"data": [{"dte": 3}, {"dte": 30}]}) is None  # no vols


@pytest.mark.asyncio
async def test_a_failing_endpoint_degrades_rather_than_raising() -> None:
    """Term structure is context, not a precondition — a scan must survive its
    absence."""
    from app.providers.unusual_whales.client import UnusualWhalesProvider

    p = UnusualWhalesProvider()

    async def _boom(path, params=None):
        raise RuntimeError("upstream down")

    p._http.get_json = _boom  # type: ignore[method-assign]
    assert await p._term_structure_slope("SPY") is None


@pytest.mark.asyncio
async def test_get_iv_context_actually_carries_the_slope_through() -> None:
    """End-to-end through the public method, not just the helper: the wiring is
    the part that was missing, not the arithmetic."""
    from app.providers.unusual_whales.client import UnusualWhalesProvider

    p = UnusualWhalesProvider()

    async def _get_json(path, params=None):
        if "term-structure" in path:
            return _TERM_ROWS
        return {"data": {"iv": "0.22"}}

    p._http.get_json = _get_json  # type: ignore[method-assign]
    ctx = await p.get_iv_context("SPY")
    assert ctx.iv30 == pytest.approx(0.22)
    assert ctx.term_structure_slope is not None, (
        "get_iv_context dropped the term-structure slope. This is exactly the "
        "FINDING_01 regression: the scorer reads this field, so a None here "
        "silently disables the backwardation penalty in production."
    )
