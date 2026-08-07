"""Retrieve evidence and mint its ids — always before an agent runs.

The ordering is the control. Python calls the providers, assigns every artifact
a stable id, and only then shows an agent the ledger. An agent can therefore
select and reason over evidence, and cannot introduce any.

Failure policy: **a provider miss is recorded, never filled.** Each call is
timed, bounded and caught individually; an error becomes a `ProviderRequestRecord`
with `ok=False` plus an entry in `ledger.provider_errors`, and the run continues
with less evidence. Substituting a default for a failed call is the exact
mechanism CLAUDE.md §4 forbids, and the difference between "no news exists" and
"the news provider was down" is preserved all the way into the report.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

from app.domain.market import CatalystEvent, EarningsEvent, PriceHistory, Quote
from app.domain.shortduration import EconomicEvent, NewsItem
from app.logging_config import get_logger
from app.multiagent.models.brief import IndexContext, VIXContext
from app.multiagent.models.enums import (
    BiasDirection,
    DataQualityFlag,
    EvidenceKind,
    EvidenceQuality,
    VolatilityRegime,
)
from app.multiagent.models.evidence import EvidenceItem, EvidenceLedger, make_evidence_id
from app.multiagent.models.runs import DataQualityRecord, ProviderRequestRecord
from app.providers import registry

log = get_logger(__name__)

T = TypeVar("T")

# A VIX print outside this band is not a VIX print. Providers and mocks alike
# can return a quote for a symbol that is not the index we meant; accepting a
# 300 "VIX" would drive the volatility regime off a fiction. Out-of-band values
# are treated as UNRESOLVED rather than clamped — a clamp would invent a number.
_VIX_PLAUSIBLE_MIN = 5.0
_VIX_PLAUSIBLE_MAX = 150.0

# Sector proxies used for sector-alignment measurement. ETFs, so they always
# have a chain and a history.
SECTOR_PROXIES: dict[str, str] = {
    "Technology": "XLK",
    "Information Technology": "XLK",
    "Semiconductors": "SMH",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Consumer Discretionary": "XLY",
    "Consumer Defensive": "XLP",
    "Consumer Staples": "XLP",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Energy": "XLE",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Basic Materials": "XLB",
    "Materials": "XLB",
}


@dataclass
class MarketEvidence:
    """Everything gathered for the market-wide brief."""

    ledger: EvidenceLedger
    indices: dict[str, IndexContext] = field(default_factory=dict)
    vix: VIXContext = field(default_factory=VIXContext)
    volatility_regime: VolatilityRegime = VolatilityRegime.UNKNOWN
    requests: list[ProviderRequestRecord] = field(default_factory=list)
    quality: list[DataQualityRecord] = field(default_factory=list)
    quotes: dict[str, Quote] = field(default_factory=dict)
    histories: dict[str, PriceHistory] = field(default_factory=dict)


class ProviderCallRecorder:
    """Times, bounds and records every outbound data call.

    Never records a URL or a header — those carry credentials. Provider name,
    capability, symbol, duration and outcome only.
    """

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.requests: list[ProviderRequestRecord] = []

    async def call(
        self,
        provider_name: str,
        capability: str,
        coro,
        *,
        symbol: str | None = None,
        default: T | None = None,
    ) -> T | None:
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        record = ProviderRequestRecord(
            provider=provider_name, capability=capability, symbol=symbol, started_at=started
        )
        try:
            result = await asyncio.wait_for(coro, timeout=self.timeout)
        except TimeoutError:
            record.ok = False
            record.error = f"timed out after {self.timeout}s"
            result = default
        except Exception as exc:  # noqa: BLE001 - one provider must not kill the run
            record.ok = False
            record.error = str(exc)[:200]
            result = default
            log.warning(
                "multiagent_provider_call_failed",
                provider=provider_name,
                capability=capability,
                symbol=symbol,
                error=str(exc)[:200],
            )
        finally:
            record.duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            if isinstance(result, list):
                record.result_count = len(result)
            self.requests.append(record)
        return result

    def errors(self) -> dict[str, str]:
        return {
            f"{r.provider}.{r.capability}" + (f"[{r.symbol}]" if r.symbol else ""): r.error or "error"
            for r in self.requests
            if not r.ok
        }


def _pct_change(new: float | None, old: float | None) -> float | None:
    """Percentage change, or None. No `or 0.0` anywhere in this file."""
    if new is None or old is None or old == 0:
        return None
    return round((new - old) / old * 100.0, 4)


def _sma(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _bias_from_return(pct: float | None, flat_threshold: float) -> BiasDirection:
    if pct is None:
        return BiasDirection.UNKNOWN
    if pct > flat_threshold:
        return BiasDirection.BULLISH
    if pct < -flat_threshold:
        return BiasDirection.BEARISH
    return BiasDirection.NEUTRAL


def build_index_context(
    symbol: str,
    quote: Quote | None,
    history: PriceHistory | None,
    *,
    lookback_days: int,
    flat_threshold_pct: float,
) -> IndexContext:
    """Measured index state. Every field is None unless it was computed."""
    ctx = IndexContext(symbol=symbol)
    if quote is not None:
        ctx.price = quote.price
        ctx.change_pct = _pct_change(quote.price, quote.prev_close)
        ctx.as_of = quote.as_of
        ctx.source = quote.source

    closes = [c.close for c in history.candles] if history else []
    if len(closes) > lookback_days:
        ctx.trailing_20d_return_pct = _pct_change(closes[-1], closes[-1 - lookback_days])
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    last = closes[-1] if closes else ctx.price
    if sma20 is not None and last is not None:
        ctx.above_20d_sma = last > sma20
    if sma50 is not None and last is not None:
        ctx.above_50d_sma = last > sma50

    ctx.bias = _bias_from_return(ctx.trailing_20d_return_pct, flat_threshold_pct)
    return ctx


class EvidenceCollector:
    """Builds the run's evidence ledger from live providers."""

    def __init__(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
        timeout: float = 25.0,
        news_lookback_days: int = 7,
        econ_lookahead_days: int = 14,
        trend_lookback_days: int = 20,
        flat_threshold_pct: float = 1.0,
    ) -> None:
        self.run_id = run_id
        self.now = now or datetime.now(UTC)
        self.recorder = ProviderCallRecorder(timeout)
        self.news_lookback_days = news_lookback_days
        self.econ_lookahead_days = econ_lookahead_days
        self.trend_lookback_days = trend_lookback_days
        self.flat_threshold_pct = flat_threshold_pct

    # -- evidence item builders ------------------------------------------

    def _news_item(self, n: NewsItem) -> EvidenceItem:
        published = n.source_ts or n.provider_ts
        return EvidenceItem(
            id=make_evidence_id(EvidenceKind.NEWS, n.id, n.symbol or "", n.headline),
            kind=EvidenceKind.NEWS,
            symbol=n.symbol,
            source=n.source,
            url=n.url,
            headline=n.headline,
            summary=n.summary,
            published_at=published,
            retrieved_at=n.received_ts or self.now,
            # A headline is a report of something, not proof of it. Only dated
            # calendar entries earn CONFIRMED_FACT.
            quality=EvidenceQuality.REPORTED,
            payload={"category": n.category, "provider_id": n.id},
        )

    def _econ_item(self, e: EconomicEvent) -> EvidenceItem:
        return EvidenceItem(
            id=make_evidence_id(EvidenceKind.ECONOMIC_EVENT, e.name, e.scheduled_at.isoformat()),
            kind=EvidenceKind.ECONOMIC_EVENT,
            symbol=None,
            source=e.source,
            url=None,
            headline=e.name,
            summary=f"{e.category or 'macro'} release, impact={e.impact or 'unknown'}",
            published_at=e.scheduled_at,
            retrieved_at=self.now,
            quality=EvidenceQuality.CONFIRMED_FACT,
            payload={
                "consensus": e.consensus,
                "previous": e.previous,
                "actual": e.actual,
                "impact": e.impact,
                "affected_markets": list(e.affected_markets),
                "status": e.status,
                "event_date": e.scheduled_at.date().isoformat(),
            },
        )

    def _earnings_item(self, e: EarningsEvent) -> EvidenceItem:
        return EvidenceItem(
            id=make_evidence_id(EvidenceKind.EARNINGS_EVENT, e.symbol, e.report_date.isoformat()),
            kind=EvidenceKind.EARNINGS_EVENT,
            symbol=e.symbol,
            source=e.source,
            headline=f"{e.symbol} earnings scheduled {e.report_date.isoformat()}"
            + (f" ({e.time_of_day})" if e.time_of_day else ""),
            summary="Scheduled earnings report.",
            published_at=None,  # a schedule entry has no publication time
            retrieved_at=self.now,
            quality=EvidenceQuality.CONFIRMED_FACT,
            payload={"report_date": e.report_date.isoformat(), "time_of_day": e.time_of_day},
        )

    def _catalyst_item(self, c: CatalystEvent) -> EvidenceItem:
        return EvidenceItem(
            id=make_evidence_id(
                EvidenceKind.CALENDAR_CATALYST, c.symbol, c.event_type, c.event_date.isoformat()
            ),
            kind=EvidenceKind.CALENDAR_CATALYST,
            symbol=c.symbol,
            source=c.source,
            headline=f"{c.symbol} {c.event_type} on {c.event_date.isoformat()}",
            summary=c.description or "",
            published_at=None,
            retrieved_at=self.now,
            quality=EvidenceQuality.CONFIRMED_FACT,
            payload={
                "event_type": c.event_type,
                "event_date": c.event_date.isoformat(),
                "is_binary": c.is_binary,
            },
        )

    # -- collection -------------------------------------------------------

    async def collect_market_evidence(
        self, symbols: list[str], *, reference_symbols: list[str]
    ) -> MarketEvidence:
        """Gather the market-wide picture: indices, news, macro calendar, events."""
        ledger = EvidenceLedger(run_id=self.run_id, built_at=self.now)
        out = MarketEvidence(ledger=ledger)

        md = registry.market_data_provider()
        cal = registry.calendar_provider()
        from app.multiagent.providers import economic_calendar_provider, news_provider

        news = news_provider()
        econ = economic_calendar_provider()

        universe = _dedupe(list(reference_symbols) + list(symbols))

        quote_tasks = {
            s: self.recorder.call(md.name, "get_quote", md.get_quote(s), symbol=s) for s in universe
        }
        history_tasks = {
            s: self.recorder.call(
                md.name,
                "get_price_history",
                md.get_price_history(s, lookback_days=120),
                symbol=s,
            )
            for s in universe
        }
        news_task = self.recorder.call(
            news.meta.name,
            "get_news",
            news.get_news(
                universe, limit=200, since=self.now - timedelta(days=self.news_lookback_days)
            ),
            default=[],
        )
        econ_task = self.recorder.call(
            econ.meta.name,
            "get_economic_events",
            econ.get_economic_events(
                from_date=self.now.date(),
                to_date=(self.now + timedelta(days=self.econ_lookahead_days)).date(),
            ),
            default=[],
        )
        earnings_tasks = {
            s: self.recorder.call(cal.name, "get_earnings", cal.get_earnings(s), symbol=s)
            for s in symbols
        }
        catalyst_tasks = {
            s: self.recorder.call(
                cal.name, "get_catalysts", cal.get_catalysts(s), symbol=s, default=[]
            )
            for s in symbols
        }

        quotes, histories, news_items, econ_events, earnings, catalysts = await asyncio.gather(
            _gather_map(quote_tasks),
            _gather_map(history_tasks),
            news_task,
            econ_task,
            _gather_map(earnings_tasks),
            _gather_map(catalyst_tasks),
        )

        out.quotes = {k: v for k, v in quotes.items() if v is not None}
        out.histories = {k: v for k, v in histories.items() if v is not None}

        for sym in reference_symbols:
            ctx = build_index_context(
                sym,
                out.quotes.get(sym),
                out.histories.get(sym),
                lookback_days=self.trend_lookback_days,
                flat_threshold_pct=self.flat_threshold_pct,
            )
            out.indices[sym] = ctx
            if ctx.price is None:
                out.quality.append(
                    DataQualityRecord(
                        flag=DataQualityFlag.MISSING_FIELD,
                        subject=sym,
                        detail="no quote retrieved for reference index",
                        observed_at=self.now,
                    )
                )

        for n in news_items or []:
            ledger.add(self._news_item(n))
        for e in econ_events or []:
            ledger.add(self._econ_item(e))
        for _sym, e in (earnings or {}).items():
            if e is not None:
                ledger.add(self._earnings_item(e))
        for _sym, cs in (catalysts or {}).items():
            for c in cs or []:
                ledger.add(self._catalyst_item(c))

        out.vix, out.volatility_regime = await self._volatility_context(out)

        ledger.provider_errors.update(self.recorder.errors())
        out.requests = list(self.recorder.requests)
        log.info(
            "multiagent_evidence_collected",
            run_id=self.run_id,
            items=len(ledger),
            symbols=len(universe),
            provider_errors=len(ledger.provider_errors),
        )
        return out

    async def _volatility_context(self, ev: MarketEvidence) -> tuple[VIXContext, VolatilityRegime]:
        """VIX where credible, and a regime derived from SPY's IV rank.

        The regime is deliberately keyed off SPY IV rank rather than a VIX print:
        IV rank is a percentile against the symbol's own history, so it is
        meaningful even when the VIX symbol is unavailable or a provider returns
        something that is plainly not the index.
        """
        vix = VIXContext()
        md = registry.market_data_provider()
        quote = await self.recorder.call(
            md.name, "get_quote", md.get_quote("VIX"), symbol="VIX"
        )
        if quote is not None and quote.price is not None:
            if _VIX_PLAUSIBLE_MIN <= quote.price <= _VIX_PLAUSIBLE_MAX:
                vix.level = quote.price
                vix.change_pct = _pct_change(quote.price, quote.prev_close)
                vix.source = quote.source
                vix.as_of = quote.as_of
            else:
                vix.commentary = (
                    f"quote for VIX was {quote.price:g}, outside the plausible band "
                    f"[{_VIX_PLAUSIBLE_MIN:g}, {_VIX_PLAUSIBLE_MAX:g}]; treated as unavailable "
                    "rather than clamped"
                )
                ev.quality.append(
                    DataQualityRecord(
                        flag=DataQualityFlag.PROVIDER_ERROR,
                        subject="VIX",
                        detail=vix.commentary,
                        observed_at=self.now,
                    )
                )
        else:
            vix.commentary = "no VIX quote retrieved"

        chain = registry.options_chain_provider()
        iv_ctx = await self.recorder.call(
            chain.name, "get_iv_context", chain.get_iv_context("SPY"), symbol="SPY"
        )
        regime = VolatilityRegime.UNKNOWN
        if iv_ctx is not None and iv_ctx.iv_rank is not None:
            rank = iv_ctx.iv_rank
            if rank < 0.25:
                regime = VolatilityRegime.COMPRESSED
            elif rank < 0.60:
                regime = VolatilityRegime.NORMAL
            elif rank < 0.85:
                regime = VolatilityRegime.ELEVATED
            else:
                regime = VolatilityRegime.STRESSED
            vix.regime = regime
            if not vix.commentary:
                vix.commentary = f"regime from SPY IV rank {rank:.2f}"
        else:
            vix.commentary = (vix.commentary + "; " if vix.commentary else "") + (
                "SPY IV rank unavailable — volatility regime unknown"
            )
        return vix, regime


def _dedupe(symbols: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for s in symbols:
        seen.setdefault(s.upper(), None)
    return list(seen)


async def _gather_map(tasks: dict[str, Any]) -> dict[str, Any]:
    if not tasks:
        return {}
    keys = list(tasks)
    results = await asyncio.gather(*(tasks[k] for k in keys))
    return dict(zip(keys, results, strict=True))


def sector_proxy_for(sector: str | None) -> str | None:
    if not sector:
        return None
    return SECTOR_PROXIES.get(sector)


def upcoming_earnings_within(
    ledger: EvidenceLedger, symbol: str, *, today: date, days: int
) -> date | None:
    """Next scheduled earnings date inside the window, or None.

    None means "no scheduled earnings evidence", never "no earnings" — the
    caller decides what to do with an absence, and the earnings hard rule treats
    the two identically only because it is the conservative direction.
    """
    best: date | None = None
    for item in ledger.for_symbol(symbol):
        if item.kind is not EvidenceKind.EARNINGS_EVENT:
            continue
        raw = item.payload.get("report_date")
        if not raw:
            continue
        try:
            d = date.fromisoformat(str(raw))
        except ValueError:
            continue
        if today <= d <= today + timedelta(days=days) and (best is None or d < best):
            best = d
    return best
