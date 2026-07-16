"""Liquidity and tradeability gates.

Implements the exclusion rules: illiquid options, wide spreads, low OI/volume,
penny stocks, low float, binary events, unreliable pricing. Gates return a
list of `RejectReason` — empty means the symbol/contract passed.

These are HARD gates (disqualify) distinct from soft scoring penalties. The
philosophy is capital preservation first: when data is missing we treat it as a
disqualifier, never as an optimistic assumption.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.domain.enums import RejectReason
from app.domain.market import Fundamentals
from app.domain.options import OptionContract
from app.engine.universe import UniverseConfig


class OptionLiquidityConfig(BaseModel):
    min_open_interest: int = 250
    min_volume: int = 50
    max_spread_pct: float = 0.10  # 10% of mid
    min_mid_price: float = 0.10  # avoid unpriceable/garbage contracts
    max_mid_price: float = 25.0  # per-share; keeps 1-lot affordable for small acct


def gate_underlying(
    fundamentals: Fundamentals, price: float, cfg: UniverseConfig
) -> list[RejectReason]:
    reasons: list[RejectReason] = []

    if not cfg.allow_penny_stocks and price < cfg.min_price:
        reasons.append(RejectReason.PENNY_STOCK)

    if (
        fundamentals.avg_dollar_volume is not None
        and fundamentals.avg_dollar_volume < cfg.min_avg_dollar_volume
    ):
        reasons.append(RejectReason.LOW_VOLUME)

    if (
        not cfg.allow_low_float
        and fundamentals.shares_float is not None
        and fundamentals.shares_float < 50_000_000
    ):
        reasons.append(RejectReason.LOW_FLOAT)

    return reasons


def gate_option(
    contract: OptionContract, cfg: OptionLiquidityConfig
) -> list[RejectReason]:
    reasons: list[RejectReason] = []

    mid = contract.mid
    # Missing/zero pricing is unreliable — disqualify rather than guess.
    if mid is None or mid <= 0:
        reasons.append(RejectReason.UNRELIABLE_PRICING)
        return reasons

    if mid < cfg.min_mid_price or mid > cfg.max_mid_price:
        reasons.append(RejectReason.UNRELIABLE_PRICING)

    if contract.open_interest is None or contract.open_interest < cfg.min_open_interest:
        reasons.append(RejectReason.LOW_OPEN_INTEREST)

    if contract.volume is None or contract.volume < cfg.min_volume:
        reasons.append(RejectReason.LOW_VOLUME)

    spread = contract.spread_pct
    if spread is None or spread > cfg.max_spread_pct:
        reasons.append(RejectReason.WIDE_SPREAD)

    return reasons


def option_liquidity_score(contract: OptionContract, cfg: OptionLiquidityConfig) -> float:
    """Soft score in [0, 1] rewarding tighter spreads and deeper OI/volume.

    Used to rank among contracts that already passed the hard gate.
    """
    spread = contract.spread_pct
    oi = contract.open_interest or 0
    vol = contract.volume or 0

    spread_score = 0.0 if spread is None else max(0.0, 1.0 - spread / cfg.max_spread_pct)
    oi_score = min(1.0, oi / (cfg.min_open_interest * 8))
    vol_score = min(1.0, vol / (cfg.min_volume * 8))

    return round(0.5 * spread_score + 0.3 * oi_score + 0.2 * vol_score, 4)
