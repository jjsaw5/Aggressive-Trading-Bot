"""Unusual Whales (UW) options-flow provider.

Grounded against the CURRENT official API (verified 2026-07):

  * Base URL: https://api.unusualwhales.com  (NO /v1 or /v2 version segment)
  * Auth:     Authorization: Bearer <API_KEY>   (header only; no query key)
  * Confirmed endpoints used here:
        GET /api/option-trades/flow-alerts          (market-wide flow alerts)
        GET /api/stock/{ticker}/flow-alerts         (per-ticker flow alerts)
  * Rate limits are surfaced per-response via x-uw-* headers; 429 on exceed.

WARNING — endpoint PATHS and AUTH are confirmed, but exact JSON RESPONSE FIELD
names are NOT hardcoded as guaranteed. Mapping below is defensive (tries known
aliases, falls back to None) and MUST be validated against the live OpenAPI
spec (https://api.unusualwhales.com/api/openapi) before production use. Do not
add fields you have not confirmed.

LICENSING: UW data is licensed for personal/internal use only. Do NOT
redistribute or expose raw UW data to third parties without an enterprise
redistribution license. See docs/providers/UNUSUAL_WHALES.md.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.config import settings
from app.domain.enums import OptionType
from app.domain.options import FlowAlert
from app.providers._http import AsyncHTTP
from app.providers.base import OptionsFlowProvider, ProviderMeta

_META = ProviderMeta(
    name="unusual_whales",
    requires_auth=True,
    typical_delay="marketed real-time; REST polling delay unconfirmed. WS/Kafka for live.",
    rate_limit="per-token; see x-uw-req-per-minute-remaining / x-uw-token-req-limit headers.",
    licensing="Personal/internal use only; redistribution prohibited without enterprise license.",
    docs_url="https://api.unusualwhales.com/docs",
    verified=True,
)


def _f(row: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        if row.get(k) is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return None


def _i(row: dict[str, Any], *keys: str) -> int | None:
    v = _f(row, *keys)
    return int(v) if v is not None else None


def _parse_dt(v: Any) -> datetime:
    if v is None:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _parse_date(v: Any) -> date | None:
    if v is None:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


class UnusualWhalesProvider(OptionsFlowProvider):
    meta = _META

    def __init__(self) -> None:
        self._http = AsyncHTTP(
            provider="unusual_whales",
            base_url=settings.unusual_whales_base_url,
            headers={"Authorization": f"Bearer {settings.unusual_whales_api_key or ''}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_flow_alerts(
        self, symbol: str | None = None, unusual_only: bool = True, limit: int = 100
    ) -> list[FlowAlert]:
        if symbol:
            path = f"/api/stock/{symbol.upper()}/flow-alerts"
            params: dict[str, Any] = {"limit": limit}
        else:
            path = "/api/option-trades/flow-alerts"
            params = {"limit": limit}
        if unusual_only:
            params["unusual"] = "true"

        payload = await self._http.get_json(path, params)
        # UW responses commonly wrap rows under a "data" key; tolerate both.
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []

        alerts: list[FlowAlert] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            raw_type = str(r.get("type") or r.get("option_type") or "").lower()
            otype = (
                OptionType.CALL
                if raw_type.startswith("c")
                else OptionType.PUT
                if raw_type.startswith("p")
                else None
            )
            at_ask = r.get("at_ask")
            sentiment = _f(r, "sentiment")
            if sentiment is None and otype is not None:
                # Derive a coarse sentiment when UW doesn't supply one.
                sign = 1.0 if otype == OptionType.CALL else -1.0
                sentiment = sign * (0.6 if at_ask else 0.3)
            alerts.append(
                FlowAlert(
                    symbol=str(r.get("ticker") or r.get("symbol") or symbol or "").upper(),
                    option_type=otype,
                    strike=_f(r, "strike"),
                    expiration=_parse_date(r.get("expiry") or r.get("expiration")),
                    premium=_f(r, "total_premium", "premium"),
                    size=_i(r, "size", "volume"),
                    open_interest=_i(r, "open_interest", "oi"),
                    is_sweep=bool(r.get("is_sweep") or r.get("sweep") or False),
                    is_opening=r.get("is_opening"),
                    at_ask=at_ask,
                    sentiment=sentiment,
                    ts=_parse_dt(r.get("executed_at") or r.get("created_at") or r.get("timestamp")),
                    source="unusual_whales",
                )
            )
        return alerts[:limit]
