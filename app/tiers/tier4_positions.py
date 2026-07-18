"""Tier 4 — open-position monitoring (highest priority).

The most responsive tier: capital is at risk, so it gets first claim on API
budget (POSITIONS priority). For each open position it marks the structure from
the live chain, computes P&L, checks the mechanical exit rules, and flags
expiry risk — reusing the paper engine's exit logic and the position's own
P&L math. It never places orders; it produces risk assessments the funnel turns
into events.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import ExitReason as _ExitReason
from app.domain.enums import OptionAction
from app.domain.options import OptionChain
from app.domain.trades import PaperTrade, TradePlan
from app.logging_config import get_logger
from app.providers.base import OptionsChainProvider
from app.providers.ratelimit import Priority, use_priority
from app.quant.analytics import structure_breakevens
from app.services.paper_engine import check_exit
from app.tiers.concurrency import bounded_gather
from app.tiers.models import PositionRisk

log = get_logger(__name__)

_BUY = {OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE}


def _chain_index(chain: OptionChain) -> dict:
    return {(c.expiration, round(c.strike, 4), c.option_type): c for c in chain.contracts}


def mark_net_per_share(plan: TradePlan, chain: OptionChain) -> float | None:
    """Current signed net (debit>0/credit<0) per share from live chain marks."""
    by_key = _chain_index(chain)
    net = 0.0
    for leg in plan.legs:
        c = by_key.get((leg.expiration, round(leg.strike, 4), leg.option_type))
        if c is None or c.mark is None:
            return None
        net += (1.0 if leg.action in _BUY else -1.0) * c.mark
    return round(net, 4)


def position_greeks(
    plan: TradePlan, chain: OptionChain
) -> tuple[float | None, float | None]:
    """Live net (delta, theta) for the whole position from the current chain.

    Delta is shares-equivalent (x100 x contracts); theta is $/day. Returns
    (None, None) if any leg is missing a live delta/theta — a partial greek would
    misstate the position's exposure, so it's all-or-nothing.
    """
    by_key = _chain_index(chain)
    net_delta = net_theta = 0.0
    for leg in plan.legs:
        c = by_key.get((leg.expiration, round(leg.strike, 4), leg.option_type))
        if c is None or c.greeks.delta is None or c.greeks.theta is None:
            return None, None
        sign = 1.0 if leg.action in _BUY else -1.0
        scale = sign * plan.contracts * 100.0
        net_delta += c.greeks.delta * scale
        net_theta += c.greeks.theta * scale
    return round(net_delta, 2), round(net_theta, 2)


class Tier4PositionMonitor:
    def __init__(self, *, chain: OptionsChainProvider, concurrency: int = 8) -> None:
        self.chain = chain
        self.concurrency = concurrency

    async def evaluate(self, trade: PaperTrade) -> PositionRisk | None:
        today = datetime.now(UTC).date()
        exps = [leg.expiration for leg in trade.trade_plan.legs]
        dte = (min(exps) - today).days if exps else None

        # Fetch the position's OWN expirations, not the default ~30-DTE window,
        # so held positions with any expiry can be marked.
        chain = await self.chain.get_option_chain_for_expirations(
            trade.symbol, sorted(set(exps))
        )
        net = mark_net_per_share(trade.trade_plan, chain)
        if net is None:
            # A risk monitor must never SILENTLY drop a position it can't price.
            # Surface it as unmarked (e.g. expiration outside the chain window)
            # so it stays visible and can be flagged, not ignored.
            return PositionRisk(
                symbol=trade.symbol,
                trade_id=trade.id,
                pnl_usd=0.0,
                pnl_pct=0.0,
                current_net=0.0,
                dte=dte,
                action="unmarked",
                note="No live mark (expiration outside chain window).",
            )

        pnl_usd = trade.mark_pnl_usd(net)
        entry = trade.entry_fill
        pnl_pct = round((net - entry) / abs(entry), 4) if abs(entry) > 1e-6 else 0.0
        time_stop = trade.trade_plan.risk.time_stop_dte

        reason = check_exit(trade, net)
        if reason == _ExitReason.PROFIT_TARGET:
            action, note = "take_profit", "Profit target reached — close."
        elif reason == _ExitReason.STOP_LOSS:
            action, note = "stop", "Stop hit — close."
        elif dte is not None and time_stop is not None and dte <= time_stop:
            action, note = "time_stop", f"Within {time_stop} DTE time stop."
        else:
            action, note = "hold", ""

        # Live risk profile: net greeks from the chain + how far spot is from the
        # nearest breakeven. All best-effort — missing greeks/spot leave None.
        net_delta, net_theta = position_greeks(trade.trade_plan, chain)
        spot = chain.underlying_price
        be_dist = None
        if spot and spot > 0:
            bes = structure_breakevens(trade.trade_plan)
            if bes:
                nearest = min(bes, key=lambda b: abs(b - spot))
                be_dist = round((nearest - spot) / spot, 4)

        return PositionRisk(
            symbol=trade.symbol,
            trade_id=trade.id,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            current_net=net,
            dte=dte,
            action=action,
            note=note,
            underlying_price=round(spot, 4) if spot else None,
            net_delta=net_delta,
            net_theta=net_theta,
            breakeven_distance_pct=be_dist,
        )

    async def run(self, trades: list[PaperTrade]) -> list[PositionRisk]:
        from app.domain.enums import PaperTradeStatus

        open_trades = [t for t in trades if t.status == PaperTradeStatus.OPEN]
        if not open_trades:
            return []
        with use_priority(Priority.POSITIONS):
            results = await bounded_gather(
                [self.evaluate(t) for t in open_trades], limit=self.concurrency
            )
        out = [r for r in results if r is not None]
        log.info("tier4_complete", monitored=len(out))
        return out
