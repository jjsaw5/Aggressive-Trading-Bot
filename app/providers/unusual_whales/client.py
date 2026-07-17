"""Unusual Whales (UW) provider: options flow, IV-rank history, option chains.

Grounded against the CURRENT official API (verified/live-validated 2026-07):

  * Base URL: https://api.unusualwhales.com  (NO /v1 or /v2 version segment)
  * Auth:     Authorization: Bearer <API_KEY>   (header only; no query key)
  * Confirmed endpoints used here:
        GET /api/option-trades/flow-alerts          (market-wide flow alerts)
        GET /api/stock/{ticker}/flow-alerts         (per-ticker flow alerts)
        GET /api/stock/{ticker}/iv-rank             (daily IV + iv_rank_1y series)
        GET /api/stock/{ticker}/stock-state         (underlying close = spot)
        GET /api/stock/{ticker}/expiry-breakdown    (expiration list)
        GET /api/stock/{ticker}/option-contracts    (per-contract IV/OI/NBBO)
        GET /api/stock/{ticker}/volatility/stats    (current ATM IV)
  * Option chains carry no greeks; delta is computed via Black-Scholes from the
    underlying spot + per-contract IV. Contract strike/type/expiry are parsed
    from the OCC option_symbol.
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
from app.domain.options import (
    FlowAlert,
    Greeks,
    IVContext,
    IVHistory,
    IVHistoryPoint,
    OptionChain,
    OptionContract,
)
from app.providers._http import AsyncHTTP
from app.providers.base import (
    IVHistoryProvider,
    OptionsChainProvider,
    OptionsFlowProvider,
    ProviderMeta,
)
from app.quant.pricing import black_scholes_delta

_META = ProviderMeta(
    name="unusual_whales",
    requires_auth=True,
    typical_delay="marketed real-time; REST polling delay unconfirmed. WS/Kafka for live.",
    rate_limit="per-token; see x-uw-req-per-minute-remaining / x-uw-token-req-limit headers.",
    licensing="Personal/internal use only; redistribution prohibited without enterprise license.",
    docs_url="https://api.unusualwhales.com/docs",
    verified=True,
)


def parse_occ_symbol(occ: str) -> tuple[date, float, OptionType] | None:
    """Parse an OCC option symbol into (expiration, strike, type).

    Format: <root><yymmdd><C|P><strike*1000, 8 digits>, e.g.
    ``AAPL260717C00335000`` -> (2026-07-17, 335.0, CALL). The root/ticker is
    variable length, so parse from the right.
    """
    if not occ or len(occ) < 16:
        return None
    tail = occ[-15:]  # yymmdd(6) + C/P(1) + strike(8)
    try:
        exp = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
        cp = tail[6].upper()
        strike = int(tail[7:15]) / 1000.0
    except (ValueError, IndexError):
        return None
    otype = OptionType.CALL if cp == "C" else OptionType.PUT if cp == "P" else None
    if otype is None or strike <= 0:
        return None
    return exp, strike, otype


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


class UnusualWhalesProvider(OptionsFlowProvider, IVHistoryProvider, OptionsChainProvider):
    meta = _META

    def __init__(self) -> None:
        self._http = AsyncHTTP(
            provider="unusual_whales",
            base_url=settings.unusual_whales_base_url,
            headers={"Authorization": f"Bearer {settings.unusual_whales_api_key or ''}"},
            # UW returns remaining/limit budget on every response; capture it so
            # exhaustion is visible before a 429 (feeds the Phase-2 budgeter).
            rate_limit_headers={
                "remaining": "x-uw-req-per-minute-remaining",
                "limit": "x-uw-token-req-limit",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_iv_history(self, symbol: str, lookback_days: int = 365) -> IVHistory:
        """Daily implied-volatility series from UW's confirmed
        GET /api/stock/{ticker}/iv-rank (fields: date, close, volatility,
        iv_rank_1y, updated_at). We keep the raw `volatility` series and let the
        engine compute IV rank/percentile consistently across providers.

        Field names are validated defensively; verify against the OpenAPI spec
        before production. Alternative source: /api/stock/{ticker}/volatility/stats
        for a point-in-time iv_rank + iv_high/iv_low."""
        # timespan=1Y returns a full year (~251 rows); the default returns only
        # the last few days (validated against the live endpoint 2026-07-16).
        payload = await self._http.get_json(
            f"/api/stock/{symbol.upper()}/iv-rank", {"timespan": "1Y"}
        )
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return IVHistory(symbol=symbol.upper(), points=[], source="unusual_whales")

        points: list[IVHistoryPoint] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            iv = _f(r, "volatility", "implied_volatility", "iv")
            ts = _parse_dt(r.get("date"))
            if iv is not None and iv > 0:
                points.append(IVHistoryPoint(ts=ts, iv=iv))
        points.sort(key=lambda p: p.ts)
        return IVHistory(symbol=symbol.upper(), points=points, source="unusual_whales")

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
            # Aggression: which side of the book the premium hit (validated
            # against live fields total_ask_side_prem / total_bid_side_prem).
            ask_prem = _f(r, "total_ask_side_prem") or 0.0
            bid_prem = _f(r, "total_bid_side_prem") or 0.0
            at_ask: bool | None = None
            ask_frac = 0.5
            if ask_prem + bid_prem > 0:
                at_ask = ask_prem > bid_prem
                ask_frac = ask_prem / (ask_prem + bid_prem)

            sentiment: float | None = None
            if otype is not None:
                sign = 1.0 if otype == OptionType.CALL else -1.0
                # Ask-side buying is more conviction; bid-side less. [-1, 1].
                sentiment = round(sign * (0.25 + 0.55 * ask_frac), 3)

            alerts.append(
                FlowAlert(
                    symbol=str(r.get("ticker") or r.get("symbol") or symbol or "").upper(),
                    option_type=otype,
                    strike=_f(r, "strike"),
                    expiration=_parse_date(r.get("expiry") or r.get("expiration")),
                    premium=_f(r, "total_premium", "premium"),
                    size=_i(r, "total_size", "size", "volume"),
                    open_interest=_i(r, "open_interest", "oi"),
                    is_sweep=bool(r.get("has_sweep") or r.get("is_sweep") or False),
                    is_opening=r.get("all_opening_trades", r.get("is_opening")),
                    at_ask=at_ask,
                    sentiment=sentiment,
                    ts=_parse_dt(r.get("created_at") or r.get("executed_at") or r.get("timestamp")),
                    source="unusual_whales",
                )
            )
        return alerts[:limit]

    # --- Options chain (OptionsChainProvider) ---
    async def _stock_close(self, symbol: str) -> float | None:
        """Current underlying price from /api/stock/{ticker}/stock-state."""
        r = await self._http.get_json(f"/api/stock/{symbol.upper()}/stock-state")
        d = r.get("data", r) if isinstance(r, dict) else r
        return _f(d, "close", "prev_close") if isinstance(d, dict) else None

    async def _chain_expirations(self, symbol: str, count: int, center_dte: int = 30) -> list[str]:
        """Pick `count` expirations nearest the target DTE from expiry-breakdown."""
        r = await self._http.get_json(f"/api/stock/{symbol.upper()}/expiry-breakdown")
        rows = r.get("data", r) if isinstance(r, dict) else r
        if not isinstance(rows, list):
            return []
        today = datetime.now(UTC).date()
        exps = [str(x.get("expires")) for x in rows if isinstance(x, dict) and x.get("expires")]
        future = [e for e in exps if (_parse_date(e) or today) >= today]
        future.sort(key=lambda e: abs(((_parse_date(e) or today) - today).days - center_dte))
        return future[:count]

    async def _contracts_for_expiration(
        self, symbol: str, exp: str, spot: float | None
    ) -> list[OptionContract]:
        payload = await self._http.get_json(
            f"/api/stock/{symbol.upper()}/option-contracts", {"expiry": exp, "limit": 500}
        )
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        now = datetime.now(UTC)
        out: list[OptionContract] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            parsed = parse_occ_symbol(str(r.get("option_symbol", "")))
            if parsed is None:
                continue
            expd, strike, otype = parsed
            iv = _f(r, "implied_volatility")
            bid = _f(r, "nbbo_bid", "bid")
            ask = _f(r, "nbbo_ask", "ask")
            mid = (bid + ask) / 2 if bid is not None and ask is not None else _f(r, "last_price")
            # UW does not supply greeks; compute delta via Black-Scholes.
            delta = None
            if spot and iv and iv > 0:
                dte = max(0, (expd - now.date()).days)
                delta = round(black_scholes_delta(spot, strike, dte / 365.0, iv, otype), 3)
            out.append(
                OptionContract(
                    symbol=symbol.upper(),
                    option_symbol=str(r.get("option_symbol")),
                    expiration=expd,
                    strike=strike,
                    option_type=otype,
                    bid=bid,
                    ask=ask,
                    mark=round(mid, 4) if mid is not None else None,
                    last=_f(r, "last_price"),
                    volume=_i(r, "volume"),
                    open_interest=_i(r, "open_interest"),
                    implied_volatility=iv,
                    greeks=Greeks(delta=delta),
                    as_of=now,
                    source="unusual_whales",
                )
            )
        return out

    async def get_option_chain(self, symbol: str, expirations: int = 4) -> OptionChain:
        spot = await self._stock_close(symbol)
        exps = await self._chain_expirations(symbol, expirations)
        contracts: list[OptionContract] = []
        for exp in exps:
            contracts.extend(await self._contracts_for_expiration(symbol, exp, spot))
        return OptionChain(
            symbol=symbol.upper(),
            underlying_price=spot,
            contracts=contracts,
            as_of=datetime.now(UTC),
            source="unusual_whales",
        )

    async def get_iv_context(self, symbol: str) -> IVContext:
        """Current ATM IV from /api/stock/{ticker}/volatility/stats (iv field).
        IV rank/percentile are computed elsewhere from the IV history."""
        iv30 = None
        try:
            r = await self._http.get_json(f"/api/stock/{symbol.upper()}/volatility/stats")
            d = r.get("data", r) if isinstance(r, dict) else r
            if isinstance(d, dict):
                iv30 = _f(d, "iv", "implied_volatility")
        except Exception:
            iv30 = None
        return IVContext(
            symbol=symbol.upper(),
            iv30=iv30,
            as_of=datetime.now(UTC),
            source="unusual_whales",
        )
