"""A research-grade mock news and economic-calendar corpus.

Why this exists rather than an extension of `app/providers/mock/provider.py`:
that file is a **freeze-guarded path** (`.github/workflows/ci.yml`,
`GUARDED_RE`) because FINDING_01 showed a provider edit can change the shipped
scoring model with no diff under `scoring/`. Adding to it would implicate the
capture window for a subsystem that has nothing to do with it.

The platform mock emits one identical headline per symbol — correct for the
latency tests it was written for, but it exercises exactly one branch of
catalyst classification. This corpus is varied on the axes the pipeline actually
reasons over: catalyst type, sentiment, scope, evidence quality, and age.

**Every item is explicitly synthetic.** Sources are `mock-*`, URLs point at
`example.test`, and each summary says so. Nothing here can be mistaken for a
real headline in a report, a database row or an export — which matters, because
the whole architecture rests on being able to tell retrieved fact from
generated text.

Deterministic: the corpus is a pure function of the symbol and the clock, so a
scan re-run over the same inputs produces identical evidence ids.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from app.domain.shortduration import EconomicEvent, NewsItem
from app.providers.base import EconomicCalendarProvider, NewsProvider, ProviderMeta

_META = ProviderMeta(
    name="mock-research",
    requires_auth=False,
    typical_delay="none (synthetic)",
    rate_limit="none",
    licensing="synthetic data, no licensing implications",
    docs_url=None,
    verified=True,
)

# (headline template, summary, category, days_old, is_confirmed_fact)
#
# Written to span the classification space: bullish/bearish/ambiguous, company/
# sector/macro scope, fresh/stale. The pipeline's keyword classifier and the
# staleness rules both need something to bite on.
_TEMPLATES: tuple[tuple[str, str, str, float], ...] = (
    (
        "{sym} beats on earnings and raises full-year guidance",
        "Company reported above consensus and lifted its outlook.",
        "earnings",
        0.4,
    ),
    (
        "Analyst upgrades {sym} to buy, price target raised",
        "Coverage change citing improving demand.",
        "analyst",
        1.2,
    ),
    (
        "{sym} misses quarterly estimates, cuts guidance",
        "Results below consensus with a reduced outlook.",
        "earnings",
        0.6,
    ),
    (
        "Regulator opens antitrust probe into {sym}",
        "Investigation announced; scope and timeline undisclosed.",
        "regulatory",
        2.5,
    ),
    (
        "{sym} announces new product launch at industry conference",
        "Product unveiled; revenue contribution not quantified.",
        "product",
        3.0,
    ),
    (
        "{sym} wins multi-year supply contract",
        "Contract awarded; financial terms not disclosed.",
        "contract",
        1.8,
    ),
    (
        "{sym} shares slump as sector rotation accelerates",
        "Move attributed to positioning rather than company news.",
        "market",
        0.3,
    ),
    (
        "{sym} names new chief financial officer",
        "Executive appointment effective next quarter.",
        "executive",
        8.0,
    ),
)

_SYNTHETIC_NOTE = "SYNTHETIC mock item for local development — not a real headline."


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)


class ResearchMockProvider(NewsProvider, EconomicCalendarProvider):
    """Varied, deterministic, unmistakably-synthetic research inputs."""

    meta = _META

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    async def get_news(
        self,
        symbols: list[str] | None = None,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[NewsItem]:
        syms = [s.upper() for s in (symbols or ["SPY", "QQQ", "NVDA"])]
        out: list[NewsItem] = []
        for sym in syms:
            seed = _seed(sym)
            # Two items per symbol, chosen deterministically but not adjacently,
            # so neighbouring tickers do not all get the same story.
            for offset in (0, 3):
                tpl, summary, category, age_days = _TEMPLATES[(seed + offset) % len(_TEMPLATES)]
                published = self._now - timedelta(days=age_days)
                item_id = f"mockresearch:{sym}:{offset}"
                out.append(
                    NewsItem(
                        id=item_id,
                        symbol=sym,
                        headline=tpl.format(sym=sym),
                        summary=f"{summary} {_SYNTHETIC_NOTE}",
                        source="mock-research-wire",
                        category=category,
                        url=f"https://example.test/mock-research/{sym}/{offset}",
                        source_ts=published,
                        provider_ts=published + timedelta(seconds=30),
                        received_ts=self._now,
                        raw_ref=item_id,
                    )
                )
        if since is not None:
            out = [n for n in out if n.source_ts is None or n.source_ts >= since]
        # Freshest first so a `limit` truncation keeps the material that matters.
        out.sort(key=lambda n: n.source_ts or n.received_ts, reverse=True)
        return out[:limit]

    async def get_economic_events(
        self, *, from_date: date | None = None, to_date: date | None = None
    ) -> list[EconomicEvent]:
        anchor = self._now.replace(minute=30, second=0, microsecond=0)
        events = [
            EconomicEvent(
                name="CPI (MoM)",
                category="inflation",
                country="US",
                scheduled_at=anchor + timedelta(hours=2),
                impact="high",
                previous=0.3,
                consensus=0.2,
                affected_markets=["SPY", "QQQ", "IWM"],
                status="scheduled",
                source="mock-research",
            ),
            EconomicEvent(
                name="FOMC Rate Decision",
                category="monetary_policy",
                country="US",
                scheduled_at=anchor + timedelta(days=2, hours=6),
                impact="high",
                # No consensus published for this one: absent stays absent, and
                # the brief must render it as a gap rather than as 0.0.
                previous=None,
                consensus=None,
                affected_markets=["SPY", "QQQ", "IWM"],
                status="scheduled",
                source="mock-research",
            ),
            EconomicEvent(
                name="Initial Jobless Claims",
                category="employment",
                country="US",
                scheduled_at=anchor + timedelta(days=1),
                impact="medium",
                previous=220000.0,
                consensus=225000.0,
                affected_markets=["SPY"],
                status="scheduled",
                source="mock-research",
            ),
            EconomicEvent(
                name="ISM Manufacturing PMI",
                category="activity",
                country="US",
                scheduled_at=anchor + timedelta(days=4),
                impact="medium",
                previous=48.7,
                consensus=49.1,
                affected_markets=["SPY", "IWM"],
                status="scheduled",
                source="mock-research",
            ),
        ]
        if from_date is not None:
            events = [e for e in events if e.scheduled_at.date() >= from_date]
        if to_date is not None:
            events = [e for e in events if e.scheduled_at.date() <= to_date]
        return events
