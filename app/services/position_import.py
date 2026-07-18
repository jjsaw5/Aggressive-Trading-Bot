"""Import real broker option positions as tracked positions Tier 4 can monitor.

The app's own Robinhood client can't run in every environment, so positions are
ingested through a normalized shape (symbol + legs) — whoever has broker access
(the agent via the connector, or a future working broker provider) maps the raw
account data to `ImportedLeg`s and calls `build_tracked_trade`. The result is a
`PaperTrade` with the REAL entry fill and a mechanical exit plan, stored like any
other tracked position so Tier 4 marks it from the live chain each pass.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.config import settings
from app.domain.enums import (
    Direction,
    OptionAction,
    OptionType,
    PaperTradeStatus,
    StrategyType,
)
from app.domain.trades import ContractLeg, PaperTrade, RiskPlan, TradePlan
from app.risk.exit_plan import for_trade_plan


@dataclass
class ImportedLeg:
    strike: float
    option_type: OptionType
    is_long: bool
    quantity: int
    entry_price_per_share: float  # positive premium per share
    expiration: date


def _infer(legs: list[ImportedLeg]) -> tuple[StrategyType, Direction]:
    calls = [leg for leg in legs if leg.option_type == OptionType.CALL]
    puts = [leg for leg in legs if leg.option_type == OptionType.PUT]
    if len(legs) == 1:
        leg = legs[0]
        if leg.option_type == OptionType.CALL:
            return StrategyType.LONG_CALL, Direction.BULLISH
        return StrategyType.LONG_PUT, Direction.BEARISH
    if len(calls) == 2:  # vertical on calls
        longs = [c for c in calls if c.is_long]
        shorts = [c for c in calls if not c.is_long]
        if longs and shorts and longs[0].strike < shorts[0].strike:
            return StrategyType.BULL_CALL_SPREAD, Direction.BULLISH
    if len(puts) == 2:
        longs = [p for p in puts if p.is_long]
        shorts = [p for p in puts if not p.is_long]
        if longs and shorts and longs[0].strike > shorts[0].strike:
            return StrategyType.BEAR_PUT_SPREAD, Direction.BEARISH
    return StrategyType.LONG_CALL, Direction.NEUTRAL  # fallback


def build_tracked_trade(
    symbol: str,
    legs: list[ImportedLeg],
    *,
    opened_at: datetime | None = None,
    source: str = "broker_import",
) -> PaperTrade:
    if not legs:
        raise ValueError("a position needs at least one leg")
    strategy, direction = _infer(legs)
    contracts = min(leg.quantity for leg in legs)

    # Signed net per share (debit > 0, credit < 0) and dollar economics.
    net_per_share = round(
        sum((1 if leg.is_long else -1) * leg.entry_price_per_share for leg in legs), 4
    )
    is_vertical = len(legs) == 2
    width = (
        abs(legs[0].strike - legs[1].strike) if is_vertical else 0.0
    )
    max_loss_usd = round(abs(net_per_share) * 100 * contracts, 2)
    max_profit_usd = (
        round((width - abs(net_per_share)) * 100 * contracts, 2) if is_vertical else None
    )

    plan_legs = [
        ContractLeg(
            symbol=symbol.upper(),
            action=OptionAction.BUY_TO_OPEN if leg.is_long else OptionAction.SELL_TO_OPEN,
            option_type=leg.option_type,
            strike=leg.strike,
            expiration=leg.expiration,
            quantity=1,
            entry_price=leg.entry_price_per_share,
        )
        for leg in legs
    ]
    risk = RiskPlan(
        max_loss_usd=max_loss_usd,
        max_profit_usd=max_profit_usd,
        account_risk_pct=round(max_loss_usd / settings.account_equity_usd, 4),
        profit_target_pct=0.5,
        stop_loss_pct=0.5,
        time_stop_dte=7,
        invalidation_note="Imported broker position.",
    )
    plan = TradePlan(
        symbol=symbol.upper(),
        direction=direction,
        strategy=strategy,
        legs=plan_legs,
        net_debit=round(net_per_share * 100, 2),
        contracts=contracts,
        risk=risk,
        rationale=f"Imported from broker ({source}).",
    )
    plan.exit_plan = for_trade_plan(plan)

    return PaperTrade(
        id=uuid.uuid4().hex[:12],
        scan_id=source,
        symbol=symbol.upper(),
        trade_plan=plan,
        status=PaperTradeStatus.OPEN,
        opened_at=opened_at or datetime.now(UTC),
        entry_fill=net_per_share,
        entry_slippage=0.0,
    )


async def sync_broker_positions() -> tuple[int, str]:
    """Pull open option positions from the configured brokerage and store them as
    tracked positions Tier 4 monitors. Raises a clear error if the broker can't
    read positions (e.g. Robinhood library not installed, or creds/MFA missing)."""
    import asyncio

    from app.db import repository
    from app.providers import registry

    broker = registry.brokerage_provider()
    getter = getattr(broker, "get_option_positions", None)
    if getter is None:
        raise RuntimeError(
            "The configured brokerage can't read positions. Set PROVIDER_BROKERAGE=robinhood."
        )
    groups = await getter()
    if not groups:
        return 0, "No open option positions returned by the broker."
    n = 0
    for symbol, legs in groups:
        if not legs:
            continue
        trade = build_tracked_trade(symbol, legs, source="rh_sync")
        await asyncio.to_thread(repository.save_paper_trade, trade)
        n += 1
    return n, f"Synced {n} position(s) from the broker."
