"""First real-mark backtest against live UW per-contract history.

Builds a small, real, multi-regime population of verticals on liquid SPY/AAPL
contracts spanning the 2021 calm and the 2022 selloff, reprices every leg from
recorded NBBO (net of round-trip commissions, at both k=0.5 and k=1.0), and
prints the aggregate. This is a validation of *structure economics* on real
option marks — the thing the Black-Scholes backtest structurally cannot do.

Run (requires the UW historic entitlement):
    UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=... \
        python -m scripts.real_mark_backtest
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

from app.backtest.real_mark import ExitRule, RealMarkTrade, evaluate_real_mark_trade
from app.backtest.real_mark_runner import aggregate
from app.providers import registry

# (root, expiry ymd, expiry date, near-money strike K, width)
_UNDERLYINGS = [
    ("SPY", "211217", date(2021, 12, 17), 450, 10),
    ("AAPL", "211217", date(2021, 12, 17), 160, 10),
    ("SPY", "220617", date(2022, 6, 17), 400, 20),
    ("AAPL", "220617", date(2022, 6, 17), 150, 10),
]
# structure -> (option type, long strike offset, short strike offset, direction)
# offsets are in multiples of `width` from K.
_STRUCTURES = {
    "call_debit_spread": ("C", 0, +1, "bullish"),      # long K, short K+w
    "put_credit_spread": ("P", -1, 0, "bullish"),       # long K-w, short K (bull put credit)
    "put_debit_spread": ("P", 0, -1, "bearish"),        # long K, short K-w
}
_ENTRY_OFFSETS = [60, 40, 25]  # trading days before expiry
_EXIT = ExitRule(profit_target_pct=0.5, stop_loss_pct=0.5, time_stop_days=21)


def _occ(root: str, ymd: str, cp: str, strike: int) -> str:
    return f"{root}{ymd}{cp}{int(strike * 1000):08d}"


def _regime(iv: float | None) -> str:
    if iv is None:
        return "unknown"
    if iv < 0.30:
        return "low"
    if iv < 0.50:
        return "mid"
    return "high"


async def main() -> None:
    provider = registry.historical_options_provider()

    # 1) Collect every contract we need and fetch its history once.
    need: set[str] = set()
    specs = []
    for root, ymd, exp, k, w in _UNDERLYINGS:
        for name, (cp, lo, so, direction) in _STRUCTURES.items():
            long_id = _occ(root, ymd, cp, k + lo * w)
            short_id = _occ(root, ymd, cp, k + so * w)
            need.add(long_id)
            need.add(short_id)
            specs.append((root, ymd, exp, name, direction, long_id, short_id))

    hist = {}
    for cid in sorted(need):
        hist[cid] = await provider.get_contract_history(cid)
    await provider.aclose()

    # 2) Build trades: pick real entry dates from each expiry's trading calendar.
    pairs = []
    for root, ymd, exp, name, direction, long_id, short_id in specs:
        long_bars = hist.get(long_id, [])
        short_bars = hist.get(short_id, [])
        if not long_bars or not short_bars:
            continue
        cal = [b.date for b in long_bars]  # ascending; expiry is last
        for off in _ENTRY_OFFSETS:
            if off + 1 > len(cal):
                continue
            entry_date = cal[-(off + 1)]
            entry_bar = next((b for b in long_bars if b.date == entry_date), None)
            regime = _regime(entry_bar.iv if entry_bar else None)
            trade = RealMarkTrade(
                trade_id=f"{root}:{ymd}:{name}:{off}dte",
                long_id=long_id, short_id=short_id, entry_date=entry_date,
                dte_at_entry=(exp - entry_date).days, contracts=1,
                strategy=name, direction=direction, vol_regime=regime,
            )
            res = evaluate_real_mark_trade(trade, long_bars, short_bars, _EXIT)
            pairs.append((trade, res))

    report = aggregate(pairs)
    print(json.dumps(report.as_dict(), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
