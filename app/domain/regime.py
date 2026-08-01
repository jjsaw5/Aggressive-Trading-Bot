"""Market regime as a property of the MARKET, not of any one signal.

Reviewer Ruling 2 rejected substituting a per-signal vol×tape tag for this. The
reasons are worth keeping next to the code, because the per-signal tag is the
more obvious thing to build and it is wrong for this job:

1. A per-signal tag conflates symbol-level with market-level conditions. Two
   signals fired in the same minute could carry different "regimes" while
   experiencing the same market — which makes the label useless as a grouping.
2. The conviction gate's `per_regime` criterion needs classes that are stable
   across the whole corpus, not recomputed per row.
3. `CAPTURE_WINDOW_PREREGISTRATION.md` §5 pre-registers per-regime cuts that
   must be computable independently of any signal.

The per-signal tag survives as a supplementary column on `MarketContext`. This
is the one the gate and the window-close analysis use.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

# VIX percentile thresholds splitting the volatility axis. Fixed here once, so
# there is exactly one definition of "high vol" at market level.
VIX_PCTL_LOW = 0.33
VIX_PCTL_HIGH = 0.67


class DailyRegime(BaseModel):
    """One trading day's market-level regime reading.

    Immutable once written: a regime row is an observation about a closed
    session, and rewriting it later would silently re-cut every analysis that
    already grouped by it (`docs/OUTCOMES.md`).
    """

    session: date

    # --- The four measurements Ruling 2 specifies ---
    vix_close: float | None = None
    # Percentile of `vix_close` within the trailing 20 sessions, inclusive.
    # NOTE: a 20-session window gives 5% granularity — coarse by construction.
    # It is what the ruling specifies and is applied literally rather than
    # silently "improved" to a 252-day window.
    vix_percentile_20d: float | None = None
    # Annualised stdev of the last 20 daily log returns on the S&P 500.
    spx_realized_vol_20d: float | None = None
    # (close - SMA50) / SMA50. Positive = above trend.
    spx_vs_50d_sma: float | None = None

    # --- Derived label ---
    # `{vol}_{trend}`, e.g. "lowvol_above". "unknown" when either axis is
    # unavailable — never defaulted to a middle bucket, which would quietly
    # inflate the count of whichever class absorbed the missing data.
    regime_class: str = "unknown"
    vol_state: str = "unknown"   # lowvol | midvol | highvol
    trend_state: str = "unknown"  # above | below

    # Provenance: which vendor supplied the two series.
    source: str = ""

    @property
    def is_complete(self) -> bool:
        """Both axes measured. A row failing this must not be used as a cut."""
        return self.vol_state != "unknown" and self.trend_state != "unknown"
