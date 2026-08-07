"""Shared fixtures for the multi-agent suite.

The root `tests/conftest.py` already pins every provider to mock, blanks the
Turso config and points the database at a temp SQLite file before any `app`
import. This file adds only what is specific to this subsystem.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

# The pipeline's market-open stage is gated on the real clock. Tests that need
# contract selection pass an explicit `now` inside market hours rather than
# depending on when the suite happens to run.
os.environ.setdefault("MA_AGENT_RUNNER", "deterministic")
os.environ.setdefault("MA_PERSIST", "false")

from app.domain.enums import Direction, OptionType, StrategyType  # noqa: E402
from app.domain.market import Candle, PriceHistory, Quote  # noqa: E402
from app.domain.options import FlowAlert  # noqa: E402
from app.multiagent.config import get_methodology  # noqa: E402
from app.multiagent.models.candidates import ResearchCandidate  # noqa: E402
from app.multiagent.models.enums import (  # noqa: E402
    EvidenceKind,
    EvidenceQuality,
    TimeHorizon,
)
from app.multiagent.models.evidence import (  # noqa: E402
    EvidenceItem,
    EvidenceLedger,
    make_evidence_id,
)


@pytest.fixture
def now() -> datetime:
    """A fixed instant inside US market hours (16:00 UTC = 12:00 ET)."""
    return datetime(2026, 8, 5, 16, 0, 0, tzinfo=UTC)


@pytest.fixture
def methodology():
    return get_methodology()


@pytest.fixture
def ledger(now) -> EvidenceLedger:
    """A small ledger with one news item and one scheduled earnings event."""
    led = EvidenceLedger(run_id="test-run", built_at=now)
    led.add(
        EvidenceItem(
            id=make_evidence_id(EvidenceKind.NEWS, "n1", "NVDA", "headline"),
            kind=EvidenceKind.NEWS,
            symbol="NVDA",
            source="test-wire",
            url="https://example.test/n1",
            headline="NVDA beats and raises guidance",
            summary="Synthetic test item.",
            published_at=now - timedelta(days=1),
            retrieved_at=now,
            quality=EvidenceQuality.REPORTED,
        )
    )
    led.add(
        EvidenceItem(
            id=make_evidence_id(EvidenceKind.EARNINGS_EVENT, "NVDA", "2026-08-12"),
            kind=EvidenceKind.EARNINGS_EVENT,
            symbol="NVDA",
            source="test-calendar",
            headline="NVDA earnings scheduled 2026-08-12",
            published_at=None,
            retrieved_at=now,
            quality=EvidenceQuality.CONFIRMED_FACT,
            payload={"report_date": "2026-08-12"},
        )
    )
    return led


@pytest.fixture
def news_ref(ledger) -> str:
    return next(i.id for i in ledger.items.values() if i.kind is EvidenceKind.NEWS)


@pytest.fixture
def earnings_ref(ledger) -> str:
    return next(i.id for i in ledger.items.values() if i.kind is EvidenceKind.EARNINGS_EVENT)


@pytest.fixture
def candidate(now, news_ref) -> ResearchCandidate:
    return ResearchCandidate(
        candidate_id="cand-1",
        run_id="test-run",
        generated_at=now,
        ticker="NVDA",
        direction=Direction.BULLISH,
        strategy_type=StrategyType.BULL_CALL_SPREAD,
        thesis="Beat and raise should carry the name higher into the next print.",
        primary_catalyst="NVDA beats and raises guidance",
        primary_catalyst_refs=[news_ref],
        evidence_refs=[news_ref],
        expected_holding_period=TimeHorizon.TWO_TO_FOUR_WEEKS,
        invalidation_thesis="Fails if it closes below the pre-earnings level twice.",
        underlying_reference_price=100.0,
    )


def make_history(
    symbol: str,
    *,
    start: float = 100.0,
    drift: float = 0.5,
    bars: int = 120,
    end: datetime | None = None,
) -> PriceHistory:
    """A clean synthetic history with a controllable trend.

    Deterministic and boring on purpose: a test asserting "trend is bullish"
    should fail because the trend logic changed, not because a random walk
    wandered.
    """
    last = end or datetime(2026, 8, 5, tzinfo=UTC)
    candles: list[Candle] = []
    price = start
    for i in range(bars):
        ts = last - timedelta(days=bars - 1 - i)
        opn = price
        price = price + drift
        high = max(opn, price) + 0.8
        low = min(opn, price) - 0.8
        candles.append(
            Candle(ts=ts, open=opn, high=high, low=low, close=price, volume=1_000_000)
        )
    return PriceHistory(symbol=symbol, candles=candles, source="test")


def make_quote(symbol: str, price: float, *, as_of: datetime, prev_close: float | None = None) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        bid=price - 0.01,
        ask=price + 0.01,
        volume=5_000_000,
        prev_close=prev_close if prev_close is not None else price * 0.99,
        as_of=as_of,
        source="test",
    )


def make_flow(
    symbol: str,
    *,
    now: datetime,
    call_premium: float = 400_000.0,
    put_premium: float = 100_000.0,
    at_ask: bool | None = True,
    open_interest: int | None = 100,
    size: int | None = 500,
    sweeps: int = 2,
) -> list[FlowAlert]:
    """Flow prints with controllable direction, side and OI."""
    out = [
        FlowAlert(
            symbol=symbol,
            option_type=OptionType.CALL,
            strike=100.0,
            premium=call_premium,
            size=size,
            open_interest=open_interest,
            is_sweep=sweeps > 0,
            at_ask=at_ask,
            ts=now - timedelta(hours=1),
            source="test",
        ),
        FlowAlert(
            symbol=symbol,
            option_type=OptionType.PUT,
            strike=95.0,
            premium=put_premium,
            size=size,
            open_interest=open_interest,
            is_sweep=sweeps > 1,
            at_ask=at_ask,
            ts=now - timedelta(hours=2),
            source="test",
        ),
    ]
    return out
