"""Amendment 4: the board offers single legs again.

`select_short_duration_contracts` has ALWAYS appended both expressions — its own
docstring promises "the near-ATM single leg AND the defined-risk debit vertical".
The single leg was never excluded by logic. It was excluded by ARITHMETIC:
`build_long_option_plan` returns None when "even one contract exceeds the
per-trade cap", and at $100 a near-ATM leg on any liquid name could not be sized
at one contract (a 2-DTE $250 underlying prices its ATM call near $261).

So the board showed spreads only, and the cause looked like a structure
preference that did not exist. These tests pin the real mechanism, so a future
reader does not go looking for the preference either.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.domain.enums import Direction, DTECategory, OptionType, StrategyType
from app.domain.options import Greeks, OptionChain, OptionContract
from app.quant.pricing import black_scholes_delta, black_scholes_price
from app.risk.policy import RiskPolicy
from app.shortduration.contracts import select_short_duration_contracts

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
AS_OF = date(2026, 8, 12)
EXP = date(2026, 8, 14)  # 2 DTE -> SHORT_DTE
SPOT, VOL, T = 250.0, 0.35, 2 / 365


def _chain() -> OptionChain:
    cs: list[OptionContract] = []
    for k in range(200, 301):
        for ot in (OptionType.CALL, OptionType.PUT):
            px = black_scholes_price(SPOT, k, T, VOL, ot)
            if px < 0.10:
                continue
            cs.append(OptionContract(
                symbol="AAA", expiration=EXP, strike=float(k), option_type=ot,
                bid=round(px - 0.03, 2), ask=round(px + 0.03, 2), mark=round(px, 2),
                volume=800, open_interest=3000, implied_volatility=VOL,
                greeks=Greeks(delta=round(black_scholes_delta(SPOT, k, T, VOL, ot), 4)),
                as_of=NOW))
    return OptionChain(symbol="AAA", underlying_price=SPOT, contracts=cs, as_of=NOW)


def _policy(cap: float) -> RiskPolicy:
    return RiskPolicy(
        account_equity_usd=25_000, max_account_risk_pct=0.15, max_trade_risk_pct=0.05,
        max_concurrent_positions=4, max_defined_risk_per_trade_usd=cap,
        max_contracts_per_trade=20)


def _expressions(cap: float):
    return select_short_duration_contracts(
        _chain(), Direction.BULLISH, DTECategory.SHORT_DTE,
        policy=_policy(cap), as_of=AS_OF, open_risk_usd=0.0)


def _strategies(cap: float) -> set[StrategyType]:
    return {r.plan.strategy for r in _expressions(cap) if r.plan is not None}


def test_the_fixture_prices_an_atm_leg_above_the_old_cap() -> None:
    """Guards the premise. If the ATM call ever cost under $100 here, every test
    below would pass without exercising anything."""
    atm = black_scholes_price(SPOT, 250, T, VOL, OptionType.CALL) * 100
    assert atm > 100.0, f"ATM call is ${atm:.0f} — fixture no longer exercises the cap"


def test_the_old_cap_suppressed_the_single_leg() -> None:
    """The defect, reproduced: at $100 only the spread survives."""
    assert _strategies(100.0) == {StrategyType.BULL_CALL_SPREAD}


def test_the_new_cap_surfaces_the_single_leg_alongside_the_spread() -> None:
    """THE change. Both expressions, so the board offers a genuine choice."""
    assert _strategies(500.0) == {StrategyType.LONG_CALL, StrategyType.BULL_CALL_SPREAD}


def test_no_structure_preference_was_changed_to_achieve_this() -> None:
    """The selector appends both unconditionally; only sizing gated the leg.

    Pins the diagnosis so nobody 'fixes' a preference that never existed.
    """
    import inspect

    from app.shortduration import contracts as mod

    src = inspect.getsource(mod.select_short_duration_contracts)
    assert "long_res" in src and "spread_res" in src
    assert src.index("long_res =") < src.index("spread_res ="), "long is attempted first"
    # Neither is conditional on the other — no preference, no fallback.
    assert "if long_res is not None:" in src and "if spread_res is not None:" in src


def test_the_single_leg_respects_the_cap() -> None:
    for r in _expressions(500.0):
        if r.plan is not None and r.plan.strategy == StrategyType.LONG_CALL:
            assert r.plan.risk.max_loss_usd <= 500.0
            return
    pytest.fail("no long call was offered")


def test_bearish_setups_get_the_same_pair() -> None:
    res = select_short_duration_contracts(
        _chain(), Direction.BEARISH, DTECategory.SHORT_DTE,
        policy=_policy(500.0), as_of=AS_OF, open_risk_usd=0.0)
    assert {r.plan.strategy for r in res if r.plan is not None} == {
        StrategyType.LONG_PUT, StrategyType.BEAR_PUT_SPREAD}


def test_the_selected_spread_itself_moved_with_the_budget() -> None:
    """The part that reaches the FROZEN scorer.

    `scoring/components.py:184` reads `reward_to_risk` off the selected plan, so a
    different spread is a different composite. This is why Amendment 4 is a model
    change and not a config tweak.
    """
    def spread(cap):
        for r in _expressions(cap):
            if r.plan is not None and r.plan.strategy == StrategyType.BULL_CALL_SPREAD:
                return r.plan
        return None

    old, new = spread(100.0), spread(500.0)
    assert old is not None and new is not None
    old_strikes = tuple(sorted(leg.strike for leg in old.legs))
    new_strikes = tuple(sorted(leg.strike for leg in new.legs))
    assert old_strikes != new_strikes or old.contracts != new.contracts, (
        "the budget change did not move the selected spread — if this ever "
        "becomes true, Amendment 4's premise needs re-deriving"
    )
