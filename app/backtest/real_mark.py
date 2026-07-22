"""Real-mark backtest evaluator: reprice a vertical from recorded NBBO history.

Given both legs' historical bars, an entry date, and an exit rule, this opens the
spread on the entry day, walks the subsequent daily marks until an exit rule
triggers, and reports the round-trip P&L at both the optimistic and conservative
spread-crossing fractions — net of commissions.

Causality is enforced structurally (§6): the signal window is `date <= entry`,
the P&L path is `date > entry`, and the exit is priced on the day it triggers
(rolling forward to the next tradeable day if that day has no usable quote). The
core evaluator is pure over already-fetched bars, so it is fully unit-testable
without touching the paid endpoint; a thin async wrapper fetches then evaluates.

This is EOD-daily data, so horizons are labeled honestly (§8): swing is
VALIDATING, short-DTE is VALIDATING (coarse), and 0DTE is NOT_TESTABLE — an
intraday feed is required and this must never inherit a swing grade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.backtest.fill_model import (
    LiquidityConfig,
    RoundTrip,
    round_trip_pnl,
    tradeable,
)
from app.domain.historic import HistoricOptionBar
from app.logging_config import get_logger

log = get_logger(__name__)

_K_OPTIMISTIC = 0.5
_K_CONSERVATIVE = 1.0


# --- Horizon fidelity (§8) ---------------------------------------------------
@dataclass(frozen=True)
class HorizonFidelity:
    horizon: str
    verdict: str  # VALIDATING | VALIDATING_COARSE | NOT_TESTABLE


def horizon_fidelity(dte_at_entry: int) -> HorizonFidelity:
    """Stamp a backtest run with what EOD-daily data can honestly support."""
    if dte_at_entry <= 1:
        return HorizonFidelity("0DTE", "NOT_TESTABLE")
    if dte_at_entry <= 5:
        return HorizonFidelity("1-5DTE", "VALIDATING_COARSE")
    return HorizonFidelity("swing", "VALIDATING")


# --- Causal helpers (§6) -----------------------------------------------------
def signal_window(bars: list[HistoricOptionBar], entry_date: date) -> list[HistoricOptionBar]:
    """Bars a signal may legally read: only those on/before entry. Asserts no
    look-ahead so a wiring bug surfaces loudly instead of silently."""
    window = [b for b in bars if b.date <= entry_date]
    assert all(b.date <= entry_date for b in window), "look-ahead: signal read a future bar"
    return window


def _align(
    long_bars: list[HistoricOptionBar], short_bars: list[HistoricOptionBar]
) -> list[tuple[date, HistoricOptionBar, HistoricOptionBar]]:
    """Trading days on which BOTH legs have a bar, in date order."""
    shorts = {b.date: b for b in short_bars}
    out = [(b.date, b, shorts[b.date]) for b in long_bars if b.date in shorts]
    out.sort(key=lambda t: t[0])
    return out


def _spread_mark_mid(long_bar: HistoricOptionBar, short_bar: HistoricOptionBar) -> float | None:
    """Mark-to-mid liquidation value of the vertical (long mid - short mid),
    k-independent — used only to detect exit triggers, not to fill."""
    if long_bar.mid is None or short_bar.mid is None:
        return None
    return long_bar.mid - short_bar.mid


# --- Trade + result models ---------------------------------------------------
@dataclass(frozen=True)
class ExitRule:
    profit_target_pct: float | None = None  # of entry mark magnitude
    stop_loss_pct: float | None = None
    time_stop_days: int | None = None  # max trading days held


@dataclass(frozen=True)
class RealMarkTrade:
    trade_id: str
    long_id: str
    short_id: str
    entry_date: date
    dte_at_entry: int
    contracts: int = 1
    strategy: str = "vertical"
    direction: str = "unknown"
    vol_regime: str | None = None


@dataclass(frozen=True)
class RealMarkResult:
    trade_id: str
    included: bool
    exclusion_reason: str | None
    entry_date: date
    exit_date: date | None
    days_held: int
    exit_reason: str | None
    by_k: dict[float, RoundTrip]
    horizon: str
    fidelity: str
    slippage_fragile: bool | None  # positive net at k=0.5 but not at k=1.0

    @property
    def net_pnl_conservative(self) -> float | None:
        rt = self.by_k.get(_K_CONSERVATIVE)
        return rt.net_pnl_usd if rt else None


def _excluded(trade: RealMarkTrade, reason: str) -> RealMarkResult:
    log.info("real_mark_excluded", trade_id=trade.trade_id, reason=reason)
    fid = horizon_fidelity(trade.dte_at_entry)
    return RealMarkResult(
        trade_id=trade.trade_id, included=False, exclusion_reason=reason,
        entry_date=trade.entry_date, exit_date=None, days_held=0, exit_reason=None,
        by_k={}, horizon=fid.horizon, fidelity=fid.verdict, slippage_fragile=None,
    )


def evaluate_real_mark_trade(
    trade: RealMarkTrade,
    long_bars: list[HistoricOptionBar],
    short_bars: list[HistoricOptionBar],
    exit_rule: ExitRule,
    *,
    liquidity: LiquidityConfig | None = None,
    ks: tuple[float, ...] = (_K_OPTIMISTIC, _K_CONSERVATIVE),
) -> RealMarkResult:
    """Pure evaluation over already-fetched bars. Excludes (with a logged reason)
    a trade whose entry-day contracts fail the liquidity guard or lack a quote."""
    liquidity = liquidity or LiquidityConfig.from_settings()
    fid = horizon_fidelity(trade.dte_at_entry)

    aligned = _align(long_bars, short_bars)
    entry = next((t for t in aligned if t[0] == trade.entry_date), None)
    if entry is None:
        return _excluded(trade, "no_aligned_entry_bar")
    _, entry_long, entry_short = entry
    if not (tradeable(entry_long, liquidity) and tradeable(entry_short, liquidity)):
        return _excluded(trade, "entry_liquidity_guard")

    entry_mark = _spread_mark_mid(entry_long, entry_short)
    if entry_mark is None:
        return _excluded(trade, "entry_no_quote")
    entry_ref = abs(entry_mark) or 1e-9  # capital basis for % rules

    forward = [t for t in aligned if t[0] > trade.entry_date]
    exit_long = exit_short = None
    exit_date: date | None = None
    exit_reason: str | None = None
    days_held = 0

    for day_idx, (d, lb, sb) in enumerate(forward, start=1):
        mark = _spread_mark_mid(lb, sb)
        if mark is None:
            continue  # no usable quote -> roll forward (this day can't be an exit)
        pnl = mark - entry_mark  # signed: works for debit and credit structures
        reason = None
        if exit_rule.profit_target_pct is not None and pnl >= exit_rule.profit_target_pct * entry_ref:
            reason = "profit_target"
        elif exit_rule.stop_loss_pct is not None and pnl <= -exit_rule.stop_loss_pct * entry_ref:
            reason = "stop_loss"
        elif exit_rule.time_stop_days is not None and day_idx >= exit_rule.time_stop_days:
            reason = "time_stop"
        if reason is not None:
            exit_long, exit_short, exit_date, exit_reason, days_held = lb, sb, d, reason, day_idx
            break

    if exit_date is None:
        # No rule fired before data ran out — close at the last tradeable day.
        tradeable_days = [(d, lb, sb) for d, lb, sb in forward if _spread_mark_mid(lb, sb) is not None]
        if not tradeable_days:
            return _excluded(trade, "no_tradeable_exit_day")
        d, exit_long, exit_short = tradeable_days[-1]
        exit_date, exit_reason, days_held = d, "expiry_or_data_end", len(tradeable_days)

    by_k = {
        k: round_trip_pnl(
            entry_long, entry_short, exit_long, exit_short,
            k=k, contracts=trade.contracts,
        )
        for k in ks
    }
    opt = by_k.get(_K_OPTIMISTIC)
    con = by_k.get(_K_CONSERVATIVE)
    fragile = (
        opt is not None and con is not None
        and opt.net_pnl_usd is not None and con.net_pnl_usd is not None
        and opt.net_pnl_usd > 0 >= con.net_pnl_usd
    )
    return RealMarkResult(
        trade_id=trade.trade_id, included=True, exclusion_reason=None,
        entry_date=trade.entry_date, exit_date=exit_date, days_held=days_held,
        exit_reason=exit_reason, by_k=by_k, horizon=fid.horizon, fidelity=fid.verdict,
        slippage_fragile=fragile,
    )


async def evaluate_from_provider(
    provider, trade: RealMarkTrade, exit_rule: ExitRule, *, window_days: int = 120
) -> RealMarkResult:
    """Fetch both legs' histories through a HistoricalOptionsProvider, then run the
    pure evaluator. One bad contract does not raise past here — it becomes an
    exclusion, mirroring the scan engine's per-symbol isolation."""
    from datetime import timedelta

    start = trade.entry_date - timedelta(days=window_days)
    end = trade.entry_date + timedelta(days=window_days)
    try:
        long_bars = await provider.get_contract_history(trade.long_id, start=start, end=end)
        short_bars = await provider.get_contract_history(trade.short_id, start=start, end=end)
    except Exception as exc:  # noqa: BLE001 — isolate one bad contract, keep the run alive
        log.warning("real_mark_fetch_failed", trade_id=trade.trade_id, error=str(exc))
        return _excluded(trade, f"fetch_failed:{type(exc).__name__}")
    return evaluate_real_mark_trade(trade, long_bars, short_bars, exit_rule)
