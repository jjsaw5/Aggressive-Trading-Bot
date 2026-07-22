"""Build real-mark backtest populations from live UW per-contract history.

Two population builders share the same machinery (contract discovery, put-call
parity spot reconstruction, real-IV vol-regime tagging):

- **fixed** — a systematic grid of verticals (call-debit / put-credit / put-debit)
  around the money. Broad coverage of structure economics.
- **engine** — lets the *engine's own selection rule* choose the trade: direction
  from the price-action trend on the reconstructed spot path, then the same
  debit-vs-credit decision the live scanner uses (`strategy_selector.build_best_plan`:
  directional + IV rank >= IV_HIGH -> credit vertical, else debit), with ATM
  strikes off the real ladder. This backtests the scanner's *logic*, honestly
  labeled: historical flow/IV-context signals aren't available, so direction is
  price-action-only and IV rank is a proxy (percentile of the ATM contract's own
  trailing IV). Both caveats are surfaced, never hidden.

Everything downstream reprices from recorded NBBO via the real-mark evaluator.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from app.backtest.historical import as_of_direction
from app.backtest.real_mark import ExitRule, RealMarkTrade
from app.domain.enums import Direction
from app.domain.historic import HistoricOptionBar
from app.engine.strategy_selector import IV_HIGH

_EXIT = ExitRule(profit_target_pct=0.5, stop_loss_pct=0.5, time_stop_days=21)


def third_friday(year: int, month: int) -> date:
    """The monthly standard-expiration date (3rd Friday)."""
    c = calendar.monthcalendar(year, month)
    fridays = [week[calendar.FRIDAY] for week in c if week[calendar.FRIDAY]]
    return date(year, month, fridays[2])


def monthly_expiries(start: date, end: date) -> list[date]:
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        exp = third_friday(y, m)
        if start <= exp <= end:
            out.append(exp)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def occ(root: str, exp: date, cp: str, strike: int) -> str:
    return f"{root}{exp:%y%m%d}{cp}{int(strike * 1000):08d}"


def _bar_on(bars: list[HistoricOptionBar], d: date) -> HistoricOptionBar | None:
    return next((b for b in bars if b.date == d), None)


def parity_spot(call_bar: HistoricOptionBar | None, put_bar: HistoricOptionBar | None,
                strike: int) -> float | None:
    """Underlying ≈ call_mid − put_mid + strike (put-call parity, rates/divs ignored)."""
    if call_bar is None or put_bar is None:
        return None
    cm, pm = call_bar.mid, put_bar.mid
    if cm is None or pm is None:
        return None
    return cm - pm + strike


def reconstruct_spot_path(
    calls: dict[int, list[HistoricOptionBar]],
    puts: dict[int, list[HistoricOptionBar]],
    dates: list[date],
) -> dict[date, float]:
    """Spot per date, using the strike whose call/put mids are closest (most ATM)."""
    path: dict[date, float] = {}
    strikes = sorted(set(calls) & set(puts))
    for d in dates:
        best = None
        for k in strikes:
            cb, pb = _bar_on(calls[k], d), _bar_on(puts[k], d)
            if cb is None or pb is None or cb.mid is None or pb.mid is None:
                continue
            gap = abs(cb.mid - pb.mid)
            s = parity_spot(cb, pb, k)
            if s is not None and (best is None or gap < best[0]):
                best = (gap, s)
        if best is not None:
            path[d] = best[1]
    return path


def _iv_rank_proxy(bars: list[HistoricOptionBar], d: date, lookback: int = 60) -> float | None:
    """Percentile of the entry-day IV within the contract's trailing IV window —
    a self-contained proxy for the underlying IV rank the live engine would read."""
    hist = [b.iv for b in bars if b.date <= d and b.iv is not None]
    if len(hist) < 10:
        return None
    cur = hist[-1]
    below = sum(1 for v in hist[-lookback:] if v <= cur)
    return round(below / len(hist[-lookback:]), 4)


def vol_regime(iv_rank: float | None) -> str:
    if iv_rank is None:
        return "unknown"
    if iv_rank >= IV_HIGH:
        return "high"
    if iv_rank <= 0.35:
        return "low"
    return "mid"


@dataclass(frozen=True)
class LadderSpec:
    root: str
    strikes: list[int]  # a ladder spanning the underlying's range over the window


def near_atm(strikes: list[int], spot: float) -> int:
    return min(strikes, key=lambda k: abs(k - spot))


def _entry_dates(dates: list[date], offsets: tuple[int, ...]) -> list[date]:
    return [dates[-(o + 1)] for o in offsets if o + 1 <= len(dates)]


def build_fixed_verticals(
    root: str, exp: date, width: int,
    calls: dict[int, list[HistoricOptionBar]],
    puts: dict[int, list[HistoricOptionBar]],
    spot_path: dict[date, float],
    entry_offsets: tuple[int, ...] = (60, 40, 25),
) -> list[tuple[RealMarkTrade, ExitRule]]:
    """Systematic call-debit / put-credit / put-debit verticals at the money."""
    strikes = sorted(set(calls) & set(puts))
    dates = sorted({b.date for bars in calls.values() for b in bars})
    trades: list[tuple[RealMarkTrade, ExitRule]] = []
    for entry in _entry_dates(dates, entry_offsets):
        spot = spot_path.get(entry)
        if spot is None:
            continue
        k = near_atm([s for s in strikes if abs(s - spot) < 10 * width] or strikes, spot)
        if k + width not in calls or k - width not in puts:
            continue
        regime = vol_regime(_iv_rank_proxy(calls.get(k, []), entry))
        dte = (exp - entry).days
        specs = [
            ("call_debit_spread", "bullish", occ(root, exp, "C", k), occ(root, exp, "C", k + width)),
            ("put_credit_spread", "bullish", occ(root, exp, "P", k - width), occ(root, exp, "P", k)),
            ("put_debit_spread", "bearish", occ(root, exp, "P", k), occ(root, exp, "P", k - width)),
        ]
        for name, direction, long_id, short_id in specs:
            trades.append((
                RealMarkTrade(
                    trade_id=f"{root}:{exp:%y%m%d}:{name}:{dte}dte:{entry}",
                    long_id=long_id, short_id=short_id, entry_date=entry,
                    dte_at_entry=dte, strategy=name, direction=direction, vol_regime=regime,
                ),
                _EXIT,
            ))
    return trades


def build_engine_verticals(
    root: str, exp: date, width: int,
    calls: dict[int, list[HistoricOptionBar]],
    puts: dict[int, list[HistoricOptionBar]],
    spot_path: dict[date, float],
    entry_offsets: tuple[int, ...] = (60, 40, 25),
) -> list[tuple[RealMarkTrade, ExitRule]]:
    """Let the engine's rule choose: price-action trend picks direction; IV rank
    (proxy) picks debit vs credit exactly as strategy_selector.build_best_plan does."""
    strikes = sorted(set(calls) & set(puts))
    dates = sorted(spot_path)
    closes = [spot_path[d] for d in dates]
    trades: list[tuple[RealMarkTrade, ExitRule]] = []
    for entry in _entry_dates(dates, entry_offsets):
        i = dates.index(entry)
        direction = as_of_direction(closes, i, fast=20, slow=50)
        if direction == Direction.NEUTRAL:
            continue
        spot = spot_path[entry]
        k = near_atm(strikes, spot)
        iv_rank = _iv_rank_proxy(calls.get(k, []), entry)
        regime = vol_regime(iv_rank)
        high_iv = iv_rank is not None and iv_rank >= IV_HIGH
        dte = (exp - entry).days

        # The engine's structure decision (mirrors build_best_plan's attempt order).
        if direction == Direction.BULLISH:
            if high_iv:  # sell a bull put credit spread
                name, long_id, short_id = "put_credit_spread", occ(root, exp, "P", k - width), occ(root, exp, "P", k)
            else:  # buy a bull call debit spread
                name, long_id, short_id = "call_debit_spread", occ(root, exp, "C", k), occ(root, exp, "C", k + width)
        else:  # BEARISH
            if high_iv:  # sell a bear call credit spread
                name, long_id, short_id = "call_credit_spread", occ(root, exp, "C", k + width), occ(root, exp, "C", k)
            else:  # buy a bear put debit spread
                name, long_id, short_id = "put_debit_spread", occ(root, exp, "P", k), occ(root, exp, "P", k - width)

        need = {long_id, short_id}
        have = {occ(root, exp, cp, s) for cp in "CP" for s in strikes}
        if not need <= have:
            continue
        trades.append((
            RealMarkTrade(
                trade_id=f"{root}:{exp:%y%m%d}:{name}:{dte}dte:{entry}",
                long_id=long_id, short_id=short_id, entry_date=entry,
                dte_at_entry=dte, strategy=name, direction=direction.value, vol_regime=regime,
            ),
            _EXIT,
        ))
    return trades
