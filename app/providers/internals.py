"""Composite market-internals provider.

Real market internals span two backends, so this composes them: sector breadth
from FMP, options-flow tide + sector-flow breadth from Unusual Whales. Each source
is best-effort — a miss is recorded and the rest still returns. Fields no
connected key can supply (A/D issues, TICK, up/down volume, new highs/lows) are
left None and named in `unavailable_fields`, never faked.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.internals import MarketInternals
from app.logging_config import get_logger
from app.providers.base import MarketInternalsProvider, ProviderMeta

log = get_logger(__name__)

_UNAVAILABLE = ["advance_decline_issues", "tick", "up_down_volume_ratio", "new_highs_minus_lows"]


class CompositeMarketInternals(MarketInternalsProvider):
    """FMP sector breadth + UW market tide + UW sector flow → one internals read."""

    meta = ProviderMeta(
        name="composite_internals",
        requires_auth=True,
        typical_delay="near-real-time (FMP EOD sector snapshot + UW intraday tide)",
        rate_limit="inherits FMP + Unusual Whales limits",
        licensing="FMP + Unusual Whales",
        docs_url=None,
        verified=True,  # live-confirmed 2026-07: FMP sector snapshot + UW tide/sector-etfs
    )

    async def get_market_internals(self, *, now: datetime | None = None) -> MarketInternals:
        from app.providers import registry

        now = now or datetime.now(UTC)
        mi = MarketInternals(as_of=now, source="fmp+unusual_whales", unavailable_fields=list(_UNAVAILABLE))

        # Sector breadth (FMP) — best-effort.
        try:
            fmp = registry.market_data_provider()
            if hasattr(fmp, "get_sector_breadth"):
                sb = await fmp.get_sector_breadth(as_of=now.date())
                if sb:
                    mi.sectors_total = int(sb["sectors_total"])
                    mi.sectors_advancing = int(sb["sectors_advancing"])
                    mi.sector_breadth_pct = round(mi.sectors_advancing / mi.sectors_total, 3)
                    mi.avg_sector_change_pct = sb.get("avg_sector_change_pct")
        except Exception as exc:  # noqa: BLE001
            mi.errors["sector_breadth"] = str(exc)[:160]
            log.warning("internals_sector_breadth_failed", error=str(exc))

        # Options-flow tide + sector flow (UW) — best-effort.
        try:
            uw = registry.options_flow_provider()
            if hasattr(uw, "get_market_tide"):
                tide = await uw.get_market_tide()
                if tide:
                    ncp, npp = tide.get("net_call_premium", 0.0), tide.get("net_put_premium", 0.0)
                    mi.net_call_premium, mi.net_put_premium = ncp, npp
                    mi.net_volume = tide.get("net_volume")
                    denom = abs(ncp) + abs(npp)
                    # tide_direction in [-1,1]: + when call premium dominates.
                    mi.tide_direction = round((ncp - abs(npp)) / denom, 3) if denom else None
            if hasattr(uw, "get_sector_flow"):
                sf = await uw.get_sector_flow()
                if sf:
                    mi.sectors_call_heavy = sf["sectors_call_heavy"]
                    mi.sector_flow_total = sf["sector_flow_total"]
                    mi.sector_flow_pct = round(sf["sectors_call_heavy"] / sf["sector_flow_total"], 3)
        except Exception as exc:  # noqa: BLE001
            mi.errors["flow_tide"] = str(exc)[:160]
            log.warning("internals_flow_tide_failed", error=str(exc))

        mi.breadth_score = _composite_breadth(mi)
        mi.is_authoritative = mi.breadth_score is not None
        return mi


def _composite_breadth(mi: MarketInternals) -> float | None:
    """Blend the available real signals into a [0,1] breadth score (0.5 = neutral).
    Averages whichever of price-breadth / flow-tide / sector-flow are present;
    returns None if none are — so the regime never invents an internals reading."""
    parts: list[float] = []
    if mi.sector_breadth_pct is not None:
        parts.append(mi.sector_breadth_pct)                 # price breadth [0,1]
    if mi.tide_direction is not None:
        parts.append((mi.tide_direction + 1.0) / 2.0)       # flow tide [-1,1] -> [0,1]
    if mi.sector_flow_pct is not None:
        parts.append(mi.sector_flow_pct)                    # flow breadth [0,1]
    return round(sum(parts) / len(parts), 3) if parts else None
