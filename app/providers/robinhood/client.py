"""Robinhood provider — quotes, historicals, option chains, and account state.

Grounded against robin_stocks 3.4.0 (module `robin_stocks.robinhood`). Robinhood
has no official public API; this uses the unofficial library and carries ToS and
account risk — see docs/providers/ROBINHOOD.md. `meta.verified` is False: the
function/field mapping is source-grounded but not yet exercised against a live
account, and greeks/IV can be null for illiquid strikes.

Implements market-data, options-chain, and (read-only) brokerage capabilities.
Order placement is intentionally absent; execution is gated by the execution
guard (automation disabled by default).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.market import PriceHistory, Quote
from app.domain.options import IVContext, OptionChain, OptionContract
from app.logging_config import get_logger
from app.providers.base import (
    BrokerageProvider,
    MarketDataProvider,
    OptionsChainProvider,
    ProviderMeta,
)
from app.providers.robinhood.mapping import parse_candles, parse_option_contract, parse_quote
from app.providers.robinhood.session import RobinhoodSession
from app.quant.iv import atm_iv_from_chain

log = get_logger(__name__)

_META = ProviderMeta(
    name="robinhood",
    requires_auth=True,
    typical_delay="account data real-time; market data freshness unconfirmed — verify.",
    rate_limit="unpublished; library does no throttling. Batch + back off; avoid per-contract loops.",
    licensing="Unofficial API; Robinhood ToS prohibits automated access. Personal use, at your risk.",
    docs_url="https://robin-stocks.readthedocs.io",
    verified=False,  # Grounded on robin_stocks 3.4.0 source; needs a live smoke test.
)


def _span_for(lookback_days: int) -> str:
    if lookback_days <= 7:
        return "week"
    if lookback_days <= 31:
        return "month"
    if lookback_days <= 93:
        return "3month"
    if lookback_days <= 366:
        return "year"
    return "5year"


class RobinhoodProvider(MarketDataProvider, OptionsChainProvider, BrokerageProvider):
    meta = _META

    def __init__(self, session: RobinhoodSession | None = None) -> None:
        self._session = session or RobinhoodSession()

    async def aclose(self) -> None:  # symmetry with other clients
        return None

    # --- Market data ---
    async def get_quote(self, symbol: str) -> Quote:
        rows = await self._session.call("stocks.get_quotes", [symbol.upper()])
        row = rows[0] if isinstance(rows, list) and rows else (rows or {})
        if not isinstance(row, dict):
            row = {}
        return parse_quote(row, symbol, datetime.now(UTC))

    async def get_price_history(self, symbol: str, lookback_days: int = 90) -> PriceHistory:
        rows = await self._session.call(
            "stocks.get_stock_historicals",
            symbol.upper(),
            interval="day",
            span=_span_for(lookback_days),
            bounds="regular",
        )
        rows = rows if isinstance(rows, list) else []
        candles = parse_candles(rows)[-lookback_days:]
        return PriceHistory(symbol=symbol.upper(), candles=candles, source="robinhood")

    # --- Options chain / IV ---
    async def _expirations(self, symbol: str, count: int, center_dte: int = 30) -> list[str]:
        chains = await self._session.call("options.get_chains", symbol.upper())
        exps = chains.get("expiration_dates", []) if isinstance(chains, dict) else []
        today = datetime.now(UTC).date()
        future = [e for e in exps if _to_date(e) and _to_date(e) >= today]  # type: ignore[arg-type]
        # Center on the target DTE so the standard 20-45 DTE window is covered.
        future.sort(key=lambda e: abs((_to_date(e) - today).days - center_dte))  # type: ignore[operator]
        return future[:count]

    async def _contracts_for_expiration(self, symbol: str, exp: str) -> list[OptionContract]:
        rows = await self._session.call(
            "options.find_options_by_expiration", symbol.upper(), expirationDate=exp
        )
        rows = rows if isinstance(rows, list) else []
        now = datetime.now(UTC)
        out = []
        for r in rows:
            if isinstance(r, dict):
                c = parse_option_contract(r, symbol, now)
                if c is not None:
                    out.append(c)
        return out

    async def get_option_chain(self, symbol: str, expirations: int = 4) -> OptionChain:
        exps = await self._expirations(symbol, expirations)
        contracts: list[OptionContract] = []
        for exp in exps:
            contracts.extend(await self._contracts_for_expiration(symbol, exp))
        underlying = None
        try:
            underlying = (await self.get_quote(symbol)).price
        except Exception as exc:  # underlying price is best-effort
            log.warning("rh_underlying_price_failed", symbol=symbol, error=str(exc))
        return OptionChain(
            symbol=symbol.upper(),
            underlying_price=underlying,
            contracts=contracts,
            as_of=datetime.now(UTC),
            source="robinhood",
        )

    async def get_iv_context(self, symbol: str) -> IVContext:
        # Compute current ATM IV straight from the ~30-DTE expiration's contracts.
        exps = await self._expirations(symbol, 1, center_dte=30)
        contracts: list[OptionContract] = []
        if exps:
            contracts = await self._contracts_for_expiration(symbol, exps[0])
        underlying = None
        try:
            underlying = (await self.get_quote(symbol)).price
        except Exception:
            pass
        chain = OptionChain(
            symbol=symbol.upper(),
            underlying_price=underlying,
            contracts=contracts,
            as_of=datetime.now(UTC),
            source="robinhood",
        )
        return IVContext(
            symbol=symbol.upper(),
            iv30=atm_iv_from_chain(chain, dte_target=30),
            as_of=datetime.now(UTC),
            source="robinhood",
        )

    # --- Brokerage (read-only) ---
    async def get_account_equity(self) -> float:
        prof = await self._session.call("profiles.load_portfolio_profile")
        if isinstance(prof, dict):
            for key in ("equity", "extended_hours_equity", "market_value"):
                v = prof.get(key)
                if v not in (None, ""):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
        return 0.0

    async def get_open_option_symbols(self) -> list[str]:
        positions = await self._session.call("options.get_open_option_positions")
        positions = positions if isinstance(positions, list) else []
        symbols = {
            str(p.get("chain_symbol")).upper()
            for p in positions
            if isinstance(p, dict) and p.get("chain_symbol")
        }
        return sorted(symbols)

    async def get_option_positions(self) -> list:
        """Full open option positions grouped by (symbol, expiration), each as a
        list of `ImportedLeg` (strike, type, long/short, qty, per-share cost).
        Mirrors what the connector pulls: positions + per-contract instrument
        data for strikes/type. Best-effort; unofficial API."""
        from app.domain.enums import OptionType
        from app.services.position_import import ImportedLeg

        positions = await self._session.call("options.get_open_option_positions")
        positions = positions if isinstance(positions, list) else []
        groups: dict[tuple[str, str], list[ImportedLeg]] = {}
        for p in positions:
            if not isinstance(p, dict):
                continue
            oid = p.get("option_id")
            sym = str(p.get("chain_symbol") or "").upper()
            try:
                qty = int(float(p.get("quantity") or 0))
            except (TypeError, ValueError):
                qty = 0
            exp = p.get("expiration_date")
            if not (oid and sym and qty and exp):
                continue
            is_long = str(p.get("type") or "").lower() == "long"
            try:
                avg = abs(float(p.get("average_price") or 0)) / 100.0  # per-share
            except (TypeError, ValueError):
                continue
            inst = await self._session.call("options.get_option_instrument_data_by_id", oid)
            if not isinstance(inst, dict) or not inst.get("strike_price"):
                continue
            otype = (
                OptionType.CALL if str(inst.get("type")).lower() == "call" else OptionType.PUT
            )
            expd = _to_date(exp)
            if expd is None:
                continue
            groups.setdefault((sym, str(exp)), []).append(
                ImportedLeg(
                    strike=float(inst["strike_price"]), option_type=otype, is_long=is_long,
                    quantity=qty, entry_price_per_share=round(avg, 4), expiration=expd,
                )
            )
        return [(sym, legs) for (sym, _exp), legs in groups.items()]


def _to_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
