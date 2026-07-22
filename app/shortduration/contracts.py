"""Short-duration contract selection.

Turns a confirmed setup into sized, DEFINED-RISK option expressions, reusing the
core selection + sizing + exit-plan machinery with short-DTE-tuned configs. The
plural selector (`select_short_duration_contracts`) returns EVERY viable
defined-risk expression — the near-ATM single leg AND the defined-risk debit
vertical, whichever fit the cap — so the board offers a mix to rank. The singular
`select_short_duration_contract` keeps the older one-best behaviour (single leg
preferred, then spread, then reject). If nothing liquid fits the per-trade cap,
the setup is REJECTED with a reason — never forced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.config import settings
from app.domain.enums import Direction, DTECategory, RejectReason, ShortDurationStrategy
from app.domain.options import OptionChain
from app.domain.shortduration import ContractRecommendation
from app.domain.trades import TradePlan
from app.engine.contract_selection import (
    SelectionConfig,
    select_long_contract,
    select_vertical_spread,
)
from app.engine.liquidity import OptionLiquidityConfig, gate_option
from app.quant.analytics import structure_breakevens
from app.risk.exit_plan import for_trade_plan
from app.risk.policy import RiskPolicy
from app.risk.trade_plan import build_long_option_plan, build_vertical_spread_plan

# Near-the-money delta band; DTE windows differ by category. 0DTE tolerates a
# slightly wider spread and cheaper contracts (ATM 0DTE premium can be small).
# moneyness_fallback_pct picks the near-ATM strike when provider greeks are
# missing/degenerate (common for single-stock 0DTE), so those names aren't
# silently rejected. 0DTE deltas roll off fast, so keep its band tighter.
_SEL = {
    DTECategory.ZERO_DTE: SelectionConfig(
        min_dte=0, max_dte=1, target_delta=0.5, min_delta=0.35, max_delta=0.68,
        moneyness_fallback_pct=0.03),
    DTECategory.SHORT_DTE: SelectionConfig(
        min_dte=1, max_dte=5, target_delta=0.5, min_delta=0.35, max_delta=0.65,
        moneyness_fallback_pct=0.05),
}
_LIQ = {
    DTECategory.ZERO_DTE: OptionLiquidityConfig(
        min_open_interest=100, min_volume=100, max_spread_pct=0.15, min_mid_price=0.05, max_mid_price=25.0
    ),
    DTECategory.SHORT_DTE: OptionLiquidityConfig(
        min_open_interest=250, min_volume=50, max_spread_pct=0.12, min_mid_price=0.10, max_mid_price=25.0
    ),
}
# A swing (daily-trend) thesis is expressed at a WEEKS-out expiry so the instrument
# matches the horizon — the fix for a daily-trend signal landing in a ~4-DTE spread.
# Longer-dated contracts carry wider spreads and higher premiums, so the liquidity
# window is relaxed accordingly. Strikes are picked a touch further OTM (lower delta).
_SWING_STRATEGIES = {ShortDurationStrategy.TREND_CONTINUATION}
_SWING_LIQ = OptionLiquidityConfig(
    min_open_interest=250, min_volume=20, max_spread_pct=0.15, min_mid_price=0.15, max_mid_price=40.0
)


def is_swing(strategy) -> bool:
    """Does this strategy's thesis need a swing (weeks) horizon rather than 0-5DTE?"""
    return strategy in _SWING_STRATEGIES


def _configs(dte: DTECategory, swing: bool) -> tuple[SelectionConfig, OptionLiquidityConfig]:
    if swing:
        return (
            SelectionConfig(
                min_dte=settings.swing_min_dte, max_dte=settings.swing_max_dte,
                target_delta=0.45, min_delta=0.30, max_delta=0.60, moneyness_fallback_pct=0.06,
            ),
            _SWING_LIQ,
        )
    return _SEL[dte], _LIQ[dte]


@dataclass
class ContractResult:
    plan: TradePlan | None
    recommendation: ContractRecommendation
    reject_reasons: list[RejectReason] = field(default_factory=list)

    @property
    def is_tradeable(self) -> bool:
        return self.plan is not None


def _recommendation(plan: TradePlan, note: str) -> ContractRecommendation:
    legs = [
        {
            "action": lg.action.value, "option_type": lg.option_type.value,
            "strike": lg.strike, "expiration": str(lg.expiration), "quantity": lg.quantity,
            "entry_price": lg.entry_price,
        }
        for lg in plan.legs
    ]
    return ContractRecommendation(
        description=f"{plan.strategy.display_name} x{plan.contracts}",
        legs=legs,
        max_loss_usd=plan.risk.max_loss_usd,
        max_profit_usd=plan.risk.max_profit_usd,
        breakevens=structure_breakevens(plan),
        est_fill_net=round(plan.net_debit / 100.0, 4),
        liquidity_note=note,
    )


def _any_liquid(chain: OptionChain, direction: Direction, dte: DTECategory, as_of: date, swing: bool) -> bool:
    from app.domain.enums import OptionType

    want = OptionType.CALL if direction == Direction.BULLISH else OptionType.PUT
    sel, liq = _configs(dte, swing)
    return any(
        c.option_type == want and sel.min_dte <= c.dte(as_of) <= sel.max_dte and not gate_option(c, liq)
        for c in chain.contracts
    )


def _long_expression(
    chain: OptionChain, direction: Direction, dte: DTECategory,
    policy: RiskPolicy, as_of: date, open_risk_usd: float, swing: bool,
) -> ContractResult | None:
    """Near-the-money single leg (defined risk = debit), or None if none fits."""
    sel, liq = _configs(dte, swing)
    choice = select_long_contract(chain, direction, as_of, sel, liq)
    plan = (
        build_long_option_plan(choice.contract, direction, policy, as_of, open_risk_usd=open_risk_usd)
        if choice else None
    )
    if plan is None:
        return None
    plan.exit_plan = for_trade_plan(plan)
    return ContractResult(plan, _recommendation(plan, "Near-ATM single leg (max loss = debit)."), [])


def _spread_expression(
    chain: OptionChain, direction: Direction, dte: DTECategory,
    policy: RiskPolicy, as_of: date, open_risk_usd: float, swing: bool,
) -> ContractResult | None:
    """Defined-risk debit vertical sized to the cap, or None if none fits."""
    sel, liq = _configs(dte, swing)
    spread = select_vertical_spread(
        chain, direction, as_of, max_debit_usd=policy.max_trade_risk_usd, sel=sel, liq=liq
    )
    plan = (
        build_vertical_spread_plan(spread, direction, policy, as_of, open_risk_usd=open_risk_usd)
        if spread else None
    )
    if plan is None:
        return None
    plan.exit_plan = for_trade_plan(plan)
    return ContractResult(plan, _recommendation(plan, "Defined-risk debit vertical."), [])


def select_short_duration_contracts(
    chain: OptionChain,
    direction: Direction,
    dte: DTECategory,
    *,
    policy: RiskPolicy,
    as_of: date,
    open_risk_usd: float = 0.0,
    swing: bool = False,
) -> list[ContractResult]:
    """EVERY viable defined-risk expression for the setup — the near-ATM single leg
    AND the defined-risk debit vertical, whichever are available — so the board
    offers a mix (long + spread) to pick from, each ranked on its own merits. Falls
    back to a single REJECTED result with a reason when nothing fits. `swing` selects
    a weeks-out expiry so a daily-trend thesis isn't forced into a 0-5DTE contract."""
    if direction not in (Direction.BULLISH, Direction.BEARISH):
        return [ContractResult(
            None,
            ContractRecommendation(description="Non-directional setups are not sized in Phase 4."),
            [RejectReason.NO_VALID_CONTRACT],
        )]
    out: list[ContractResult] = []
    long_res = _long_expression(chain, direction, dte, policy, as_of, open_risk_usd, swing)
    spread_res = _spread_expression(chain, direction, dte, policy, as_of, open_risk_usd, swing)
    if long_res is not None:
        out.append(long_res)
    if spread_res is not None:
        out.append(spread_res)
    if out:
        return out

    # Nothing fits — say why (traceable, never silently dropped).
    if not _any_liquid(chain, direction, dte, as_of, swing):
        reasons = [RejectReason.ILLIQUID_OPTION]
        why = "No liquid contract in the DTE/delta window (spread/OI/volume gates)."
    else:
        reasons = [RejectReason.RISK_UNMANAGEABLE]
        why = f"No defined-risk structure fits the ${policy.max_trade_risk_usd:g} per-trade cap."
    return [ContractResult(None, ContractRecommendation(description=why, liquidity_note=why), reasons)]


def select_short_duration_contract(
    chain: OptionChain,
    direction: Direction,
    dte: DTECategory,
    *,
    policy: RiskPolicy,
    as_of: date,
    open_risk_usd: float = 0.0,
    swing: bool = False,
) -> ContractResult:
    """Single best expression (single leg preferred, then spread, then reject).
    Kept for callers wanting one structure; the board uses the plural variant."""
    return select_short_duration_contracts(
        chain, direction, dte, policy=policy, as_of=as_of, open_risk_usd=open_risk_usd, swing=swing
    )[0]
