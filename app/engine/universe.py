"""Trading universe definition and configuration.

The default universe is the liquid mega-cap / index-ETF set requested for the
initial build. It is fully configurable — override via `UniverseConfig`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

DEFAULT_UNIVERSE: list[str] = [
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "NVDA", "AMD", "META", "AMZN", "GOOGL", "TSLA", "NFLX",
]

# Opt-in lower-priced, liquid, actively-optioned names. Useful when running
# tighter per-trade caps where mega-cap spreads are unaffordable (see
# docs/RISK_POLICY.md). Not enabled by default — the primary universe above is
# the specified target set.
AFFORDABLE_UNIVERSE: list[str] = [
    "F", "SOFI", "PLTR", "INTC", "T", "PFE", "BAC", "CCL", "NIO", "RIVN",
]

# --- Short-duration universes ------------------------------------------------
# 0DTE is restricted to names that reliably list SAME-DAY expirations: the index
# ETFs and the mega-caps that now carry dailies. Trading "0DTE" on a name without
# a daily listing is impossible, so a broad list would only add rejected rows.
ZERO_DTE_UNIVERSE: list[str] = [
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "AMD", "META", "AMZN", "GOOGL", "TSLA", "NFLX",
]

# 1-5DTE casts a wide net: broadly liquid, actively-optioned names across a range
# of prices so defined-risk structures fit tight per-trade caps. The cheaper
# names are deliberately included so more setups clear the risk cap instead of
# rejecting as `risk_unmanageable`.
SHORT_DURATION_UNIVERSE: list[str] = [
    # index / sector ETFs
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "SMH", "GLD",
    # mega-cap tech / semis
    "AAPL", "MSFT", "NVDA", "AMD", "META", "AMZN", "GOOGL", "TSLA", "NFLX", "AVGO", "MU",
    # liquid large-caps
    "JPM", "BAC", "WFC", "C", "DIS", "UBER", "PYPL", "BABA", "COIN", "PLTR",
    # lower-priced, high-volume names (fit tighter per-trade caps)
    "F", "SOFI", "INTC", "T", "PFE", "CCL", "AAL", "NIO", "RIVN", "MARA", "RIOT", "SNAP",
]


def short_duration_universe(is_zero_dte: bool) -> list[str]:
    """The default scan universe for a short-duration DTE category."""
    return list(ZERO_DTE_UNIVERSE if is_zero_dte else SHORT_DURATION_UNIVERSE)


class UniverseConfig(BaseModel):
    """Configurable universe + hard gating toggles for excluded categories.

    The excluded categories default to disabled, matching the requirement to
    exclude/heavily penalize illiquid, penny, low-float, and binary-event names
    unless specifically enabled.
    """

    symbols: list[str] = Field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    allow_low_float: bool = False
    allow_binary_biotech: bool = False
    allow_penny_stocks: bool = False

    # Minimum quality thresholds (underlying-level).
    min_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000.0  # $20M/day
    min_market_cap: float = 2_000_000_000.0  # $2B

    def normalized_symbols(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for s in self.symbols:
            u = s.strip().upper()
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out
