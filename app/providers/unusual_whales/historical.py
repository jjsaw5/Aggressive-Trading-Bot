"""Unusual Whales per-contract historical option data (HistoricalOptionsProvider).

Sources real historical marks — including true NBBO bid/ask and implied
volatility — from UW's per-contract history endpoint, so the backtest can reprice
legs from recorded quotes instead of Black-Scholes-at-realized-vol.

    GET /api/option-contract/{id}/historic
    Auth: Authorization: Bearer <UW_TOKEN>
    {id} is an ISO/OCC option symbol, e.g. AAPL260821C00342500
    Response: {"chains": [ {one row per trading day}, ... ]}

ENTITLEMENT: the historic endpoint is part of UW's paid **API tier**, distinct
from the website subscription. A token without it returns 401/403 — surfaced here
as a loud `HistoricalDataUnentitledError`, never a silent retry, so the
precondition failure is unmissable. Verify with one live call before trusting a
backtest built on this feed.

Granularity is EOD daily; there is no intraday. All numeric fields arrive as
strings and are parsed defensively (bad value -> None, never 0.0, never now()).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.config import settings
from app.domain.historic import HistoricOptionBar, parse_float, parse_int
from app.domain.options import OptionMarkPoint
from app.logging_config import get_logger
from app.providers._http import AsyncHTTP, ProviderHTTPError
from app.providers.base import HistoricalOptionsProvider, ProviderMeta

log = get_logger(__name__)

_META = ProviderMeta(
    name="unusual_whales_historic",
    requires_auth=True,
    typical_delay="EOD daily; immutable once the trading day closes.",
    rate_limit="per-token; x-uw-req-per-minute-remaining / x-uw-token-req-limit.",
    licensing="Personal/internal use only; redistribution prohibited.",
    docs_url="https://api.unusualwhales.com/docs",
    verified=False,  # endpoint schema confirmed from docs; live entitlement per-token
)


class HistoricalDataUnentitledError(RuntimeError):
    """The UW token lacks the historic-endpoint entitlement (HTTP 401/403).

    A hard stop, not a transient error: the data is not licensed on this token,
    so no amount of retry will help. Confirm the API-tier add-on."""

    def __init__(self, contract_id: str, status: int) -> None:
        super().__init__(
            f"UW historic endpoint returned HTTP {status} for {contract_id!r}: the "
            "token lacks historic-data entitlement (paid API tier, separate from the "
            "website subscription). Verify UW_TOKEN's add-on before enabling."
        )
        self.contract_id = contract_id
        self.status = status


def _bar_from_row(contract_id: str, row: dict) -> HistoricOptionBar | None:
    """Normalize one raw history row. Returns None (dropped + logged by caller) on
    an unparseable trading day — never coerces a bad date to now()."""
    d = row.get("date")
    try:
        trading_day = date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None
    return HistoricOptionBar(
        contract_id=contract_id,
        date=trading_day,
        nbbo_bid=parse_float(row.get("nbbo_bid")),
        nbbo_ask=parse_float(row.get("nbbo_ask")),
        last_fill=parse_float(row.get("last_price")),
        iv=parse_float(row.get("implied_volatility")),
        iv_high=parse_float(row.get("iv_high")),
        iv_low=parse_float(row.get("iv_low")),
        open_interest=parse_int(row.get("open_interest")),
        volume=parse_int(row.get("volume")),
        trades=parse_int(row.get("trades")),
    )


class UWHistoricalOptionsProvider(HistoricalOptionsProvider):
    meta = _META

    def __init__(self) -> None:
        self._http = AsyncHTTP(
            provider="unusual_whales_historic",
            base_url=settings.unusual_whales_base_url,
            headers={"Authorization": f"Bearer {settings.unusual_whales_api_key or ''}"},
            rate_limit_headers={
                "remaining": "x-uw-req-per-minute-remaining",
                "limit": "x-uw-token-req-limit",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_contract_history(
        self, contract_id: str, *, start: date | None = None, end: date | None = None
    ) -> list[HistoricOptionBar]:
        """Full per-day history for one contract, optionally sliced to [start, end].

        The endpoint returns the whole history; we filter client-side. A 401/403
        raises `HistoricalDataUnentitledError`; a malformed row is dropped + logged,
        never coerced."""
        path = f"/api/option-contract/{contract_id}/historic"
        try:
            payload = await self._http.get_json(path)
        except ProviderHTTPError as exc:
            if exc.status in (401, 403):
                raise HistoricalDataUnentitledError(contract_id, exc.status) from exc
            raise

        rows = payload.get("chains", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []

        bars: list[HistoricOptionBar] = []
        dropped = 0
        for r in rows:
            if not isinstance(r, dict):
                dropped += 1
                continue
            bar = _bar_from_row(contract_id, r)
            if bar is None:
                dropped += 1
                continue
            if start is not None and bar.date < start:
                continue
            if end is not None and bar.date > end:
                continue
            bars.append(bar)
        if dropped:
            log.warning("uw_historic_rows_dropped", contract_id=contract_id, dropped=dropped)
        bars.sort(key=lambda b: b.date)
        return bars

    async def get_option_mark_series(
        self, option_symbol: str, start: date, end: date
    ) -> list[OptionMarkPoint]:
        """Adapt the historic bars to the generic mark series the interface
        promises: the NBBO mid is the mark; days without a usable quote are
        omitted (no fabricated mark)."""
        bars = await self.get_contract_history(option_symbol, start=start, end=end)
        points: list[OptionMarkPoint] = []
        for b in bars:
            mid = b.mid
            if mid is None:
                continue
            points.append(
                OptionMarkPoint(
                    option_symbol=option_symbol,
                    ts=datetime(b.date.year, b.date.month, b.date.day, tzinfo=UTC),
                    mark=round(mid, 4),
                    implied_volatility=b.iv,
                    source="unusual_whales",
                )
            )
        return points
