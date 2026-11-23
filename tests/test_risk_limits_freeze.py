"""The risk limits are a SCORING INPUT, and nothing was guarding them.

This control exists because Amendment 4 exposed a hole. The freeze guard gates on
PATH — `scoring/`, `strategies/`, `contracts.py`, two providers, `iv_context.py`,
`contract_selection.py`. The risk limits live in `app/config.py`, which is not in
that list. So a one-line budget change altered the shipped model's output while
CI stayed green.

The chain is short and entirely mechanical:

    settings.max_defined_risk_per_trade_usd
      -> RiskPolicy.max_trade_risk_usd          (min of pct cap and absolute cap)
      -> shortduration/contracts.py:192,221     (`max_debit_usd=...` into selection)
      -> a DIFFERENT structure is selected
      -> scoring/components.py:184 reads `reward_to_risk` off that plan
      -> the composite moves

Measured on a 2-DTE fixture when the cap went 100 -> 500: the selected spread
moved 252/255 x1 -> 250/256 x2, and a long call appeared alongside it that could
not previously be sized at one contract.

This is the THIRD instance of the same pattern — FINDING_01 (a provider field),
Amendment 2 (contract selection), Amendment 4 (the budget). Each time the
behavioural controls were blind: `test_scoring_golden.py` scores hand-built
`IVContext` fixtures and passes NO trade plan, so a selection change cannot move
its numbers. The golden diff for Amendment 4 was the version stamp and nothing
else.

So this file pins the limits themselves. It is deliberately dumb: if the numbers
move, it fails, and the only legitimate way past it is the same declaration §2
demands — a `scoring_model_version` bump plus a dated §8 amendment.
"""

from __future__ import annotations

from app.config import settings
from app.risk.policy import RiskPolicy

# Amendment 4 (2026-08-12). Moving any of these is a MODEL CHANGE.
FROZEN_LIMITS = {
    "account_equity_usd": 25_000.0,
    "max_defined_risk_per_trade_usd": 500.0,
    "max_account_risk_pct": 0.15,
    "max_trade_risk_pct": 0.05,
    "max_concurrent_positions": 4,
    "max_contracts_per_trade": 20,
}
# What those settings RESOLVE to. Pinned separately because the resolution rule
# (min of the percentage cap and the absolute cap) is itself load-bearing: at
# 25_000 the 5% cap is $1,250, so the $500 absolute cap is what actually binds.
# A change that moved the binding constraint from one to the other would leave
# the raw settings looking untouched.
FROZEN_RESOLVED = {
    "max_trade_risk_usd": 500.0,
    "max_account_risk_usd": 3_750.0,
}


def test_the_configured_risk_limits_are_the_declared_ones() -> None:
    actual = {k: getattr(settings, k) for k in FROZEN_LIMITS}
    assert actual == FROZEN_LIMITS, (
        f"Risk limits changed: {actual} != {FROZEN_LIMITS}.\n"
        "These are a SCORING INPUT — they set `max_debit_usd` for contract "
        "selection, and the scorer reads `reward_to_risk` off the selected plan. "
        "Per §2 the model is frozen; per §8 any deviation needs a dated amendment "
        "in the same commit as a scoring_model_version bump."
    )


def test_the_resolved_caps_are_the_declared_ones() -> None:
    """Pins the RESOLUTION, not just the inputs."""
    p = RiskPolicy.from_settings()
    actual = {"max_trade_risk_usd": p.max_trade_risk_usd,
              "max_account_risk_usd": p.max_account_risk_usd}
    assert actual == FROZEN_RESOLVED, f"{actual} != {FROZEN_RESOLVED}"


def test_the_absolute_cap_is_what_binds() -> None:
    """The $500 cap must be the binding constraint, not the 5% one.

    If equity rose without the absolute cap keeping pace, the percentage would
    silently take over and the per-trade risk would drift with the account
    balance — which is not what "a $500 cap" means.
    """
    pct_cap = settings.account_equity_usd * settings.max_trade_risk_pct
    assert settings.max_defined_risk_per_trade_usd < pct_cap, (
        f"the ${settings.max_defined_risk_per_trade_usd:g} absolute cap no longer binds "
        f"(5% of equity is ${pct_cap:g}); per-trade risk would now float with equity"
    )


def test_the_limits_declaration_matches_the_frozen_model_version() -> None:
    """Ties this file to the version it was declared under, so the two cannot
    drift apart silently."""
    from tests.test_scoring_freeze import FROZEN_MODEL_VERSION

    assert settings.scoring_model_version == FROZEN_MODEL_VERSION


def test_aggregate_heat_still_bounds_the_position_count() -> None:
    """Max theoretical deployment must stay inside aggregate heat.

    4 concurrent x $500 = $2,000 against $3,750 of heat. If the position count
    ever rose past 7, heat would start rejecting trades the per-trade cap allowed
    — a silent second limit the board would not explain.
    """
    p = RiskPolicy.from_settings()
    max_deployed = p.max_concurrent_positions * p.max_trade_risk_usd
    assert max_deployed <= p.max_account_risk_usd, (
        f"{p.max_concurrent_positions} x ${p.max_trade_risk_usd:g} = ${max_deployed:g} "
        f"exceeds ${p.max_account_risk_usd:g} of aggregate heat"
    )
