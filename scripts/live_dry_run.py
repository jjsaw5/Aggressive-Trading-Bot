"""Live dry run of the short-duration module.

Exercises the full pipeline end-to-end: build_market_regime -> run_detection for
both 0DTE and 1-5DTE. Which feeds are live vs mock is decided by the provider env
vars (printed below). No orders are ever placed; entries show BLOCKED off-hours.

Usage:
    python -m scripts.live_dry_run
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime


def _p(k: str) -> str:
    return os.environ.get(k) or "<default:mock>"


async def main() -> None:
    from app.domain.enums import DTECategory
    from app.shortduration.detection import run_detection
    from app.shortduration.service import build_market_regime

    now = datetime.now(UTC)
    print("=" * 78)
    print(f"SHORT-DURATION LIVE DRY RUN  @ {now:%Y-%m-%d %H:%M:%S} UTC")
    print("=" * 78)
    print("Provider wiring:")
    for k in (
        "PROVIDER_MARKET_DATA", "PROVIDER_INTRADAY", "PROVIDER_OPTIONS_FLOW",
        "PROVIDER_OPTIONS_CHAIN", "PROVIDER_NEWS", "PROVIDER_ECON_CALENDAR",
    ):
        print(f"  {k:<24} = {_p(k)}")
    print("-" * 78)

    # --- Market regime -------------------------------------------------------
    regime, levels, breadth = await build_market_regime(now=now)
    print("MARKET REGIME")
    print(f"  regime            : {regime.regime.value}  (confidence {regime.confidence:.2f})")
    print(f"  allow_new_trades  : {regime.allow_new_trades}")
    print(f"  reduce_size       : {regime.reduce_size}")
    print(f"  spy/qqq/iwm trend : {regime.spy_trend_pct} / {regime.qqq_trend_pct} / {regime.iwm_trend_pct}")
    print(f"  vol_reading       : {regime.vol_reading}")
    print(f"  next event        : {regime.next_event_name} (in {regime.next_event_minutes} min)")
    _notes = regime.notes if isinstance(regime.notes, list) else [regime.notes]
    for n in [x for x in _notes if x][:4]:
        print(f"     note: {n}")
    print(f"  breadth (%>VWAP)  : {breadth.above_vwap_pct}  "
          f"(n={breadth.symbols_considered}, proxy={breadth.is_proxy})")
    print(f"  levels computed   : {len(levels)} symbols")
    for sym in sorted(levels)[:5]:
        lv = levels[sym]
        print(f"     {sym:<5} vwap={lv.vwap} last={lv.last} rel_vol={lv.relative_volume}")
    print("-" * 78)

    # --- Detection (both DTE categories) ------------------------------------
    # Trim the universe to a few liquid names so the live UW chain sweep stays
    # under the rate budget for a one-shot dry run.
    dry_universe = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]
    print(f"Detection universe (trimmed for rate budget): {dry_universe}")
    print("-" * 78)
    for dte in (DTECategory.ZERO_DTE, DTECategory.SHORT_DTE):
        cands = await run_detection(dte, now=now, universe=dry_universe)
        print(f"DETECTION  [{dte.value}]  -> {len(cands)} candidate(s)")
        for c in cands[:8]:
            rr = f"{c.reward_to_risk:.2f}" if c.reward_to_risk else "—"
            risk = f"${c.max_risk_usd:.0f}" if c.max_risk_usd else "—"
            print(
                f"  {c.symbol:<5} {c.state.value:<10} score={c.score:.2f} "
                f"{(c.strategy.value if c.strategy else '—'):<22} "
                f"dir={c.direction.value:<4} R:R={rr:<5} risk={risk:<6} "
                f"entry={'ALLOWED' if c.entry_allowed else 'BLOCKED'}"
            )
            if c.contract and c.contract.description:
                print(f"        contract : {c.contract.description}")
            if c.entry_notes:
                print(f"        gates    : {'; '.join(c.entry_notes[:3])}")
            if c.reject_reasons:
                print(f"        rejects  : {'; '.join(c.reject_reasons[:3])}")
        print("-" * 78)

    print("Dry run complete. No orders placed (execution guard remains gated OFF).")


if __name__ == "__main__":
    asyncio.run(main())
