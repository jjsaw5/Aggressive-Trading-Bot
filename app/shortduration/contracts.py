"""Short-duration contract selection.

Turns a confirmed setup into a sized, DEFINED-RISK option expression, reusing the
core selection + sizing + exit-plan machinery with short-DTE-tuned configs. The
policy is the small-account guardrail: try a near-the-money single leg first, and
if its debit exceeds the per-trade risk cap, fall back to a defined-risk debit
vertical that fits. If nothing liquid fits the cap, the setup is REJECTED with a
reason — never forced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.domain.enums import Direction, DTECategory, RejectReason
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
_SEL = {
    DTECategory.ZERO_DTE: SelectionConfig(min_dte=0, max_dte=1, target_delta=0.5, min_delta=0.35, max_delta=0.68),
    DTECategory.SHORT_DTE: SelectionConfig(min_dte=1, max_dte=5, target_delta=0.5, min_delta=0.35, max_delta=0.65),
}
_LIQ = {
    DTECategory.ZERO_DTE: OptionLiquidityConfig(
        min_open_interest=100, min_volume=100, max_spread_pct=0.15, min_mid_price=0.05, max_mid_price=25.0
    ),
    DTECategory.SHORT_DTE: OptionLiquidityConfig(
        min_open_interest=250, min_volume=50, max_spread_pct=0.12, min_mid_price=0.10, max_mid_price=25.0
    ),
}


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
        description=f"{plan.strategy.value.replace('_', ' ')} x{plan.contracts}",
        legs=legs,
        max_loss_usd=plan.risk.max_loss_usd,
        max_profit_usd=plan.risk.max_profit_usd,
        breakevens=structure_breakevens(plan),
        est_fill_net=round(plan.net_debit / 100.0, 4),
        liquidity_note=note,
    )


def _any_liquid(chain: OptionChain, direction: Direction, dte: DTECategory, as_of: date) -> bool:
    from app.domain.enums import OptionType

    want = OptionType.CALL if direction == Direction.BULLISH else OptionType.PUT
    sel, liq = _SEL[dte], _LIQ[dte]
    return any(
        c.option_type == want and sel.min_dte <= c.dte(as_of) <= sel.max_dte and not gate_option(c, liq)
        for c in chain.contracts
    )


def _long_expression(
    chain: OptionChain, direction: Direction, dte: DTECategory,
    policy: RiskPolicy, as_of: date, open_risk_usd: float,
) -> ContractResult | None:
    """Near-the-money single leg (defined risk = debit), or None if none fits."""
    sel, liq = _SEL[dte], _LIQ[dte]
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
    policy: RiskPolicy, as_of: date, open_risk_usd: float,
) -> ContractResult | None:
    """Defined-risk debit vertical sized to the cap, or None if none fits."""
    sel, liq = _SEL[dte], _LIQ[dte]
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
) -> list[ContractResult]:
    """EVERY viable defined-risk expression for the setup — the near-ATM single leg
    AND the defined-risk debit vertical, whichever are available — so the board
    offers a mix (long + spread) to pick from, each ranked on its own merits. Falls
    back to a single REJECTED result with a reason when nothing fits."""
    if direction not in (Direction.BULLISH, Direction.BEARISH):
        return [ContractResult(
            None,
            ContractRecommendation(description="Non-directional setups are not sized in Phase 4."),
            [RejectReason.NO_VALID_CONTRACT],
        )]
    out: list[ContractResult] = []
    long_res = _long_expression(chain, direction, dte, policy, as_of, open_risk_usd)
    spread_res = _spread_expression(chain, direction, dte, policy, as_of, open_risk_usd)
    if long_res is not None:
        out.append(long_res)
    if spread_res is not None:
        out.append(spread_res)
    if out:
        return out

    # Nothing fits — say why (traceable, never silently dropped).
    if not _any_liquid(chain, direction, dte, as_of):
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
) -> ContractResult:
    """Single best expression (single leg preferred, then spread, then reject).
    Kept for callers wanting one structure; the board uses the plural variant."""
    return select_short_duration_contracts(
        chain, direction, dte, policy=policy, as_of=as_of, open_risk_usd=open_risk_usd
    )[0]
