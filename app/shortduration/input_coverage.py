"""Input-coverage monitor: per-symbol, per-feed, per-field — abstain, don't guess.

Born from a silent total failure: iv_rank was None on EVERY short-duration
candidate for an unknown period, and nothing noticed — the missing factor quietly
became a constant 0.25 in every composite while the board kept ranking. This
module makes input integrity a first-class, monitored fact:

- **Per-symbol:** every feed/field the scan consumed is checked present/missing/
  stale, producing a coverage fraction over the REQUIRED set for the DTE track.
- **Abstention (the Phase-0 principle applied to inputs):** when a symbol's
  coverage falls below the configured threshold, the candidate ABSTAINS from
  ranking — rendered as such and held back from watchlist/arm — rather than
  letting missing inputs default into a plausible-looking number.
- **Per-scan alerting:** field coverage is aggregated across the scan's symbols;
  a field whose scan-wide coverage drops below the alert threshold (e.g. an IV
  feed silently dying for everyone) is logged loudly and gauged in metrics, so a
  systemic outage surfaces on the first scan, not in an audit months later.

Observational fields (news, catalysts, earnings date) are tracked for the feed
report but do NOT count against the abstention fraction — absence of news is not
a data failure.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import DTECategory
from app.domain.options import IVContext, OptionChain
from app.logging_config import get_logger
from app.shortduration.strategies.base import SetupContext

log = get_logger(__name__)

_QUOTE_STALE_SECONDS = 120.0

PRESENT = "present"
MISSING = "missing"
STALE = "stale"


class InputCheck(BaseModel):
    feed: str  # provider-ish grouping: intraday | levels | market_data | flow | chain | iv | daily | news | calendar
    field: str
    status: str  # present | missing | stale
    required: bool = True
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.feed}.{self.field}"


class SymbolCoverage(BaseModel):
    symbol: str
    dte_category: str
    checks: list[InputCheck] = Field(default_factory=list)
    coverage: float = 0.0  # present / required
    missing: list[str] = Field(default_factory=list)  # required, non-present keys

    def finalize(self) -> SymbolCoverage:
        req = [c for c in self.checks if c.required]
        ok = sum(1 for c in req if c.status == PRESENT)
        self.coverage = round(ok / len(req), 3) if req else 0.0
        self.missing = [c.key for c in req if c.status != PRESENT]
        return self


def assess_symbol_coverage(
    ctx: SetupContext,
    *,
    chain: OptionChain | None,
    iv: IVContext | None,
    dte: DTECategory,
    now: datetime,
) -> SymbolCoverage:
    """Structured coverage over exactly the inputs the scan consumed for a symbol."""
    checks: list[InputCheck] = []

    def add(feed: str, field: str, present: bool, *, required: bool = True,
            stale: bool = False, detail: str = "") -> None:
        status = PRESENT if present else (STALE if stale else MISSING)
        checks.append(InputCheck(feed=feed, field=field, status=status,
                                 required=required, detail=detail))

    add("intraday", "bars_1m", bool(ctx.bars_1m),
        detail=f"{len(ctx.bars_1m or [])} bars")
    lv = ctx.levels
    add("levels", "vwap", lv is not None and lv.vwap is not None)
    fresh = False
    stale_quote = False
    if ctx.quote is not None and ctx.quote.as_of is not None:
        age = (now - ctx.quote.as_of).total_seconds()
        fresh = ctx.quote.delayed_minutes == 0 and age <= _QUOTE_STALE_SECONDS
        stale_quote = not fresh
    add("market_data", "quote_fresh", fresh, stale=stale_quote)
    add("chain", "contracts", chain is not None and bool(chain.contracts),
        detail=f"{len(chain.contracts) if chain else 0} contracts")
    add("iv", "iv30", iv is not None and iv.iv30 is not None)
    # The field whose silent death started all of this. Checked separately from
    # iv30 so a partial IV feed can never mask a dead rank join again.
    add("iv", "iv_rank", iv is not None and (iv.iv_rank is not None or iv.iv_percentile is not None),
        detail=f"source={getattr(iv, 'iv_rank_source', None)}" if iv else "")
    if dte == DTECategory.ZERO_DTE:
        add("levels", "opening_range", lv is not None and lv.opening_range_high is not None)
        add("flow", "alerts", bool(ctx.flow), detail=f"{len(ctx.flow or [])} alerts")
    else:
        add("daily", "history_50d", ctx.daily is not None and len(ctx.daily.closes) >= 50,
            detail=f"{len(ctx.daily.closes) if ctx.daily else 0} closes")
        add("flow", "alerts", bool(ctx.flow), required=False,
            detail=f"{len(ctx.flow or [])} alerts")
    # Observational: tracked for feed health, never counted against abstention.
    add("news", "items", bool(ctx.news), required=False)
    add("calendar", "catalysts", bool(ctx.catalysts), required=False)
    add("calendar", "earnings_date", ctx.next_earnings is not None, required=False)

    return SymbolCoverage(symbol=ctx.symbol, dte_category=dte.value, checks=checks).finalize()


class FieldCoverage(BaseModel):
    feed: str
    field: str
    n_symbols: int
    n_present: int
    coverage: float
    required: bool


class ScanCoverage(BaseModel):
    dte_category: str
    at: datetime
    n_symbols: int
    fields: list[FieldCoverage] = Field(default_factory=list)
    degraded: list[str] = Field(default_factory=list)  # field keys under the alert threshold
    symbols: list[SymbolCoverage] = Field(default_factory=list)


def aggregate_scan(
    reports: list[SymbolCoverage], *, dte: DTECategory, now: datetime, alert_threshold: float
) -> ScanCoverage:
    """Cross-symbol field coverage + the degraded-field alert list. A field that is
    missing for most of a scan is a FEED problem, not a symbol problem — log it as
    one, loudly, and gauge it so dashboards/alerting can see the outage."""
    agg: dict[tuple[str, str, bool], list[int]] = {}
    for rep in reports:
        for c in rep.checks:
            agg.setdefault((c.feed, c.field, c.required), []).append(1 if c.status == PRESENT else 0)
    fields = [
        FieldCoverage(feed=f, field=fl, n_symbols=len(v), n_present=sum(v),
                      coverage=round(sum(v) / len(v), 3), required=req)
        for (f, fl, req), v in sorted(agg.items())
    ]
    degraded = [f"{fc.feed}.{fc.field}" for fc in fields
                if fc.required and fc.coverage < alert_threshold]
    scan = ScanCoverage(dte_category=dte.value, at=now, n_symbols=len(reports),
                        fields=fields, degraded=degraded, symbols=reports)
    try:
        from app.observability.metrics import get_metrics
        m = get_metrics()
        for fc in fields:
            m.set_gauge(f"sd.input_coverage.{dte.value}.{fc.feed}.{fc.field}", fc.coverage)
        m.set_gauge(f"sd.input_coverage.{dte.value}.degraded_fields", float(len(degraded)))
    except Exception as exc:  # noqa: BLE001 — monitoring must never break a scan
        log.warning("sd_coverage_metrics_failed", error=str(exc))
    for key in degraded:
        fc = next(f for f in fields if f"{f.feed}.{f.field}" == key)
        log.warning(
            "sd_input_coverage_degraded", dte=dte.value, field=key,
            coverage=fc.coverage, n_symbols=fc.n_symbols,
            hint="feed-level outage: this input is missing across the scan, not one symbol",
        )
    _LAST_SCAN[dte.value] = scan
    return scan


# Latest scan coverage per DTE track, for the read-only API/dashboard.
_LAST_SCAN: dict[str, ScanCoverage] = {}


def last_scan_coverage(dte: str | None = None) -> dict[str, ScanCoverage]:
    if dte is not None:
        return {dte: _LAST_SCAN[dte]} if dte in _LAST_SCAN else {}
    return dict(_LAST_SCAN)
