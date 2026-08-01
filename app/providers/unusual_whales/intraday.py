"""Unusual Whales per-contract INTRADAY option bars (IntradayOptionsProvider).

    GET /api/option-contract/{id}/intraday?date=YYYY-MM-DD
    Auth: Authorization: Bearer <UW_TOKEN>
    {id} is an OCC option symbol, e.g. SPY260728C00730000
    Response: {"data": [ {one row per traded minute}, ... ]}

Verified live against `SPY260728C00730000` on 2026-07-28: HTTP 200, 123 minute
bars spanning 13:30Z-20:11Z, session high 12.25 / low 6.50.

This endpoint was in the UW OpenAPI spec the whole time. The historical provider
next door implements `/historic`, which is EOD daily, and its docstring's "there
is no intraday" refers to that endpoint — not to UW. Reading it as a statement
about the vendor is what left the managed policy unmeasured, 0DTE suspended, and
a paid data feed looking necessary.

TWO PROPERTIES OF THIS DATA THAT MUST NOT BE FORGOTTEN DOWNSTREAM:

1. **Bars are sparse.** Only minutes with prints produce one — roughly a third of
   a session for a liquid contract, far fewer for an illiquid one. A gap means
   "nothing traded", not "price unchanged". Never interpolate.
2. **These are trades, not quotes.** open/high/low/close are executions. The
   bid/ask fields classify which side of the book each trade hit; they are not
   an NBBO. Cost figures derived here are `effective_*` and are labeled to keep
   them distinguishable from a quoted spread forever.

Numeric fields arrive as strings (or occasionally as numbers) and are parsed
defensively: a bad value becomes None, never 0.0.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.config import settings
from app.domain.options import OptionMinuteBar
from app.logging_config import get_logger
from app.providers._http import AsyncHTTP, ProviderHTTPError
from app.providers.base import IntradayOptionsProvider, ProviderMeta

log = get_logger(__name__)

_META = ProviderMeta(
    name="unusual_whales_intraday",
    requires_auth=True,
    typical_delay="1-minute bars; current session available intraday.",
    rate_limit="per-token; x-uw-req-per-minute-remaining / x-uw-token-req-limit.",
    licensing="Personal/internal use only; redistribution prohibited.",
    docs_url="https://api.unusualwhales.com/api/openapi",
    verified=True,  # live 200 on SPY260728C00730000, 2026-07-28
)


class IntradayDataUnentitledError(RuntimeError):
    """The token lacks the intraday entitlement (HTTP 401/403).

    A hard stop, not a transient error — retrying an unlicensed endpoint just
    burns rate limit."""

    def __init__(self, contract_id: str, status: int) -> None:
        super().__init__(
            f"UW intraday endpoint returned HTTP {status} for {contract_id!r}: the "
            "token lacks intraday entitlement. Verify the plan before enabling."
        )
        self.contract_id = contract_id
        self.status = status


def _f(row: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        v = row.get(k)
        if v in (None, ""):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _i(row: dict[str, Any], *keys: str) -> int:
    """Volume/premium counters. Absent means zero traded — which, unlike a price,
    genuinely is zero."""
    v = _f(row, *keys)
    return int(v) if v is not None else 0


def parse_minute_bar(row: dict[str, Any], option_symbol: str) -> OptionMinuteBar | None:
    """One minute bar, or None when the row cannot be trusted.

    A bar without all four OHLC values is dropped rather than back-filled from
    whichever fields did arrive.
    """
    ts_raw = row.get("start_time")
    o, h, low, c = (_f(row, "open"), _f(row, "high"), _f(row, "low"), _f(row, "close"))
    if not ts_raw or None in (o, h, low, c):
        return None
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    # A crossed bar (high < low) is corrupt, not a tight range.
    if h < low:
        return None
    return OptionMinuteBar(
        option_symbol=option_symbol.upper(),
        start_time=ts,
        open=o, high=h, low=low, close=c,
        avg_price=_f(row, "avg_price"),
        iv_high=_f(row, "iv_high"),
        iv_low=_f(row, "iv_low"),
        volume_bid_side=_i(row, "volume_bid_side"),
        volume_ask_side=_i(row, "volume_ask_side"),
        volume_mid_side=_i(row, "volume_mid_side"),
        premium_bid_side=_f(row, "premium_bid_side") or 0.0,
        premium_ask_side=_f(row, "premium_ask_side") or 0.0,
        premium_mid_side=_f(row, "premium_mid_side") or 0.0,
        source="unusual_whales",
    )


class UnusualWhalesIntradayProvider(IntradayOptionsProvider):
    """Minute bars per option contract, one session per call."""

    meta = _META

    def __init__(self) -> None:
        self._http = AsyncHTTP(
            provider="unusual_whales_intraday",
            base_url=settings.unusual_whales_base_url,
            headers={
                "Authorization": f"Bearer {settings.unusual_whales_api_key or ''}",
                "Accept": "application/json",
            },
            rate_limit_headers={
                "remaining": "x-uw-req-per-minute-remaining",
                "limit": "x-uw-token-req-limit",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_option_minute_bars(
        self, option_symbol: str, session: date
    ) -> list[OptionMinuteBar]:
        """One session of minute bars, ascending by time.

        The API returns newest-first; callers depend on chronological order to
        replay a path, so sorting here is not cosmetic.
        """
        sym = option_symbol.upper()
        try:
            payload = await self._http.get_json(
                f"/api/option-contract/{sym}/intraday", {"date": session.isoformat()}
            )
        except ProviderHTTPError as exc:
            if getattr(exc, "status", None) in (401, 403):
                raise IntradayDataUnentitledError(sym, exc.status) from exc
            raise

        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []

        bars, dropped = [], 0
        for r in rows:
            if not isinstance(r, dict):
                dropped += 1
                continue
            bar = parse_minute_bar(r, sym)
            if bar is None:
                dropped += 1
                continue
            bars.append(bar)
        if dropped:
            log.warning("uw_intraday_rows_dropped", option_symbol=sym, dropped=dropped)
        bars.sort(key=lambda b: b.start_time)
        return bars
