"""Real-NBBO fill model + liquidity guard for historical backtesting.

This is the piece that replaces `spread = 0.0` in the current backtest. You never
fill at mid by assumption — you cross each leg's *own* real bid/ask, controlled by
`k`:

    k = 0.5  -> fill at mid (optimistic; you captured the whole spread)
    k = 1.0  -> pay the full half-spread (you crossed to the far touch)

Every backtest is meant to be run at BOTH and reported side by side: a strategy
whose edge exists at k=0.5 but evaporates at k=1.0 is *slippage-fragile* and must
not be certified — the "edge" was living inside the spread. All defaults here err
toward understating edge, on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import settings
from app.domain.historic import HistoricOptionBar

Side = Literal["buy", "sell"]


def leg_fill(bar: HistoricOptionBar, side: Side, k: float) -> float | None:
    """Fill price for one leg, crossing `k` of its half-spread. None when the day
    has no usable quote (missing or crossed NBBO) — the leg is not fillable."""
    if bar.mid is None or bar.half_spread is None:
        return None
    if side == "buy":
        return bar.mid + k * bar.half_spread  # pay up toward the ask
    return bar.mid - k * bar.half_spread  # get hit down toward the bid


def spread_net_debit(
    long_bar: HistoricOptionBar,
    short_bar: HistoricOptionBar,
    k: float,
    *,
    close: bool = False,
) -> float | None:
    """Net per-share price of a vertical, aligning the two legs already on the
    same date.

    Open (`close=False`): buy the long leg, sell the short leg -> a debit
    structure returns > 0 (you paid), a credit structure returns < 0 (you
    received). Close (`close=True`): the crossing direction reverses per leg (sell
    the long, buy back the short) — the spread's liquidation value. Returns None
    if either leg is unfillable that day.
    """
    if close:
        long_leg = leg_fill(long_bar, "sell", k)
        short_leg = leg_fill(short_bar, "buy", k)
    else:
        long_leg = leg_fill(long_bar, "buy", k)
        short_leg = leg_fill(short_bar, "sell", k)
    if long_leg is None or short_leg is None:
        return None
    return long_leg - short_leg


def round_trip_commission(
    *, contracts: int, num_legs: int = 2, per_contract: float | None = None
) -> float:
    """Commission for a full round trip: charged per leg per contract on BOTH the
    open and the close. A 2-leg vertical => 4 charges. UW gives no commissions;
    this is the cost the platform must add itself."""
    rate = settings.bt_commission_per_contract if per_contract is None else per_contract
    return rate * contracts * num_legs * 2


@dataclass(frozen=True)
class RoundTrip:
    fillable: bool
    entry_net_debit: float | None  # >0 debit paid, <0 credit received
    exit_net: float | None  # liquidation value at close
    gross_pnl_usd: float | None
    commissions_usd: float
    net_pnl_usd: float | None  # gross minus every commission leg — the number that matters
    k: float


def round_trip_pnl(
    entry_long: HistoricOptionBar,
    entry_short: HistoricOptionBar,
    exit_long: HistoricOptionBar,
    exit_short: HistoricOptionBar,
    *,
    k: float,
    contracts: int,
    per_contract_commission: float | None = None,
) -> RoundTrip:
    """P&L of opening a vertical on the entry day and closing it on the exit day,
    at spread-crossing fraction `k`, net of round-trip commissions. If either day
    is unfillable, `fillable=False` and P&L is None (never fabricated)."""
    entry = spread_net_debit(entry_long, entry_short, k, close=False)
    exit_ = spread_net_debit(exit_long, exit_short, k, close=True)
    commissions = round_trip_commission(contracts=contracts, per_contract=per_contract_commission)
    if entry is None or exit_ is None:
        return RoundTrip(False, entry, exit_, None, commissions, None, k)
    gross = (exit_ - entry) * 100 * contracts
    return RoundTrip(True, entry, exit_, gross, commissions, gross - commissions, k)


@dataclass(frozen=True)
class LiquidityConfig:
    min_oi: int
    min_vol: int
    max_spread_pct: float

    @classmethod
    def from_settings(cls) -> LiquidityConfig:
        return cls(
            min_oi=settings.bt_min_oi,
            min_vol=settings.bt_min_vol,
            max_spread_pct=settings.bt_max_spread_pct,
        )


def tradeable(bar: HistoricOptionBar, cfg: LiquidityConfig | None = None) -> bool:
    """Could this contract realistically have been filled on this day? A modeled
    fill on a contract nobody traded is fiction — gate it out of the backtest.
    Excluded entries should be LOGGED with a reason by the caller, not silently
    dropped: a strategy that only works on contracts it couldn't fill is a
    finding."""
    cfg = cfg or LiquidityConfig.from_settings()
    mid = bar.mid
    return (
        mid is not None
        and mid > 0
        and bar.open_interest is not None
        and bar.open_interest >= cfg.min_oi
        and bar.volume is not None
        and bar.volume >= cfg.min_vol
        and bar.half_spread is not None
        and (2 * bar.half_spread) / mid <= cfg.max_spread_pct
    )
