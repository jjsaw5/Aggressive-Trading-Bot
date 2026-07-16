"""Strategy selection: choose the structure that best expresses the thesis
given direction and the implied-volatility regime.

Core idea — let IV rank pick debit vs credit:
  * Directional + LOW IV rank  -> buy a debit vertical (cheap premium, convex).
  * Directional + HIGH IV rank -> sell a credit vertical (rich premium, high POP).
  * Neutral + HIGH IV rank     -> iron condor (defined-risk short vol, range-bound).
  * Neutral + LOW IV rank + catalyst -> long straddle/strangle (long vol into event).

Each candidate structure is attempted in priority order; the first that sizes
within the risk policy wins. Returns the sized `TradePlan` (or None).
"""

from __future__ import annotations

from datetime import date

from app.domain.enums import Direction
from app.domain.options import OptionChain
from app.domain.trades import TradePlan
from app.engine.contract_selection import (
    select_credit_vertical,
    select_iron_condor,
    select_long_contract,
    select_straddle,
    select_strangle,
    select_vertical_spread,
)
from app.risk.policy import RiskPolicy
from app.risk.trade_plan import (
    build_long_option_plan,
    build_structure_plan,
    build_vertical_spread_plan,
)

IV_HIGH = 0.50
IV_LOW = 0.35


def build_best_plan(
    chain: OptionChain,
    direction: Direction,
    iv_rank: float | None,
    has_catalyst: bool,
    policy: RiskPolicy,
    as_of: date,
    *,
    open_risk_usd: float = 0.0,
) -> TradePlan | None:
    remaining = max(0.0, policy.max_account_risk_usd - open_risk_usd)
    max_risk = min(policy.max_trade_risk_usd, remaining)
    if max_risk <= 0:
        return None

    def debit_vertical() -> TradePlan | None:
        c = select_vertical_spread(chain, direction, as_of, max_debit_usd=max_risk)
        return build_vertical_spread_plan(
            c, direction, policy, as_of, open_risk_usd=open_risk_usd
        ) if c else None

    def long_option() -> TradePlan | None:
        c = select_long_contract(chain, direction, as_of)
        return build_long_option_plan(
            c.contract, direction, policy, as_of, open_risk_usd=open_risk_usd
        ) if c else None

    def credit_vertical() -> TradePlan | None:
        c = select_credit_vertical(chain, direction, as_of, max_risk_usd=max_risk)
        return build_structure_plan(c, policy, as_of, open_risk_usd=open_risk_usd) if c else None

    def condor() -> TradePlan | None:
        c = select_iron_condor(chain, as_of, max_risk_usd=max_risk)
        return build_structure_plan(c, policy, as_of, open_risk_usd=open_risk_usd) if c else None

    def straddle() -> TradePlan | None:
        c = select_straddle(chain, as_of, max_debit_usd=max_risk)
        return build_structure_plan(c, policy, as_of, open_risk_usd=open_risk_usd) if c else None

    def strangle() -> TradePlan | None:
        c = select_strangle(chain, as_of, max_debit_usd=max_risk)
        return build_structure_plan(c, policy, as_of, open_risk_usd=open_risk_usd) if c else None

    high_iv = iv_rank is not None and iv_rank >= IV_HIGH
    low_iv = iv_rank is not None and iv_rank <= IV_LOW

    attempts: list = []
    if direction in (Direction.BULLISH, Direction.BEARISH):
        if high_iv:
            attempts = [credit_vertical, debit_vertical, long_option]
        else:
            attempts = [debit_vertical, long_option, credit_vertical]
    elif direction == Direction.NEUTRAL:
        if high_iv:
            attempts = [condor]
        elif low_iv and has_catalyst:
            attempts = [strangle, straddle]

    for attempt in attempts:
        plan = attempt()
        if plan is not None:
            return plan
    return None
