"""Build the live single-symbol research report.

Fans out concurrently to every provider we already use plus both play engines
(short-duration 0DTE/1-5DTE and the core swing scanner), for ANY ticker. Each
section is independent and best-effort: a provider miss or a slow engine records
an error for that section and the rest of the report still returns.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.domain.enums import DTECategory
from app.domain.research import FlowSummary, SymbolReport
from app.engine.universe import UniverseConfig
from app.logging_config import get_logger
from app.providers import registry

log = get_logger(__name__)

# A slow engine/provider must not hang the whole report.
_SECTION_TIMEOUT_S = 25.0


async def _guard(report: SymbolReport, key: str, coro, default=None):
    """Await a section coroutine; on error/timeout record it and return default."""
    try:
        return await asyncio.wait_for(coro, timeout=_SECTION_TIMEOUT_S)
    except TimeoutError:
        report.errors[key] = "timed out"
    except Exception as exc:  # noqa: BLE001 - one section must not kill the report
        report.errors[key] = str(exc)[:200]
        log.warning("symbol_report_section_failed", symbol=report.symbol, section=key, error=str(exc))
    return default


def _summarize_flow(alerts) -> FlowSummary:
    from app.domain.enums import OptionType

    if not alerts:
        return FlowSummary()
    calls = sum(1 for a in alerts if a.option_type == OptionType.CALL)
    puts = sum(1 for a in alerts if a.option_type == OptionType.PUT)
    prem = sum((a.premium or 0.0) for a in alerts)
    sents = [a.sentiment for a in alerts if a.sentiment is not None]
    net = round(sum(sents) / len(sents), 3) if sents else None
    top = sorted(alerts, key=lambda a: (a.premium or 0.0), reverse=True)[:6]
    return FlowSummary(
        alerts=len(alerts), calls=calls, puts=puts,
        total_premium_usd=round(prem, 2), net_sentiment=net, top=top,
    )


async def build_symbol_report(symbol: str, *, now: datetime | None = None) -> SymbolReport:
    symbol = symbol.upper().strip()
    now = now or datetime.now(UTC)
    report = SymbolReport(symbol=symbol, as_of=now)

    async def _quote():
        q = await registry.market_data_provider().get_quote(symbol)
        report.quote = q
        if q and q.prev_close:
            report.change_pct = round((q.price - q.prev_close) / q.prev_close * 100, 3)
        return q

    async def _flow():
        alerts = await registry.options_flow_provider().get_flow_alerts(
            symbol, unusual_only=True, limit=50
        )
        report.flow = _summarize_flow(alerts)

    async def _levels():
        from app.shortduration import service
        report.levels = await service.get_symbol_levels(symbol, now=now)

    async def _iv():
        report.iv = await registry.options_chain_provider().get_iv_context(symbol)

    async def _news():
        report.news = await registry.news_provider().get_news([symbol], limit=15)

    async def _earnings():
        report.earnings = await registry.calendar_provider().get_earnings(symbol)

    async def _catalysts():
        report.catalysts = await registry.calendar_provider().get_catalysts(symbol)

    async def _fundamentals():
        report.fundamentals = await registry.fundamentals_provider().get_fundamentals(symbol)

    async def _zero_dte():
        from app.shortduration.detection import run_detection
        report.zero_dte = await run_detection(DTECategory.ZERO_DTE, now=now, universe=[symbol])

    async def _short_dte():
        from app.shortduration.detection import run_detection
        report.one_five_dte = await run_detection(DTECategory.SHORT_DTE, now=now, universe=[symbol])

    async def _swing():
        from app.services.scan_service import run_scan
        report.swing = await run_scan(universe=UniverseConfig(symbols=[symbol]))

    await asyncio.gather(
        _guard(report, "quote", _quote()),
        _guard(report, "flow", _flow()),
        _guard(report, "levels", _levels()),
        _guard(report, "iv", _iv()),
        _guard(report, "news", _news()),
        _guard(report, "earnings", _earnings()),
        _guard(report, "catalysts", _catalysts()),
        _guard(report, "fundamentals", _fundamentals()),
        _guard(report, "zero_dte", _zero_dte()),
        _guard(report, "one_five_dte", _short_dte()),
        _guard(report, "swing", _swing()),
    )
    log.info("symbol_report", symbol=symbol,
             plays=len(report.zero_dte) + len(report.one_five_dte) + len(report.swing),
             errors=len(report.errors))
    return report
