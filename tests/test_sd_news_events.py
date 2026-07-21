"""Phase 5 — structured news-catalyst classification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import Direction
from app.domain.shortduration import NewsItem
from app.shortduration.scoring.news_events import (
    build_news_catalysts,
    classify_direction_mixed,
    classify_event_type,
    extract_values,
)

_NOW = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


def _item(headline, *, source="unknown", symbol="AAPL", summary="", ago_s=0) -> NewsItem:
    return NewsItem(
        id=str(abs(hash((headline, source))) % 10**9), symbol=symbol, headline=headline,
        summary=summary, source=source, received_ts=_NOW - timedelta(seconds=ago_s),
        source_ts=_NOW - timedelta(seconds=ago_s),
    )


def test_event_type_taxonomy() -> None:
    assert classify_event_type("Acme Q3 earnings beat estimates") == "earnings"
    assert classify_event_type("Acme raises full-year guidance") == "guidance"
    assert classify_event_type("Analyst upgrades Acme to Overweight, PT raised") == "rating_change"
    assert classify_event_type("Acme to acquire Beta Corp in $2B buyout") == "m_and_a"
    assert classify_event_type("Acme faces SEC investigation over accounting") == "legal_regulatory"
    assert classify_event_type("FDA approval for Acme's Phase 3 drug") == "fda_clinical"
    assert classify_event_type("Acme unveils new product launch") == "product"
    assert classify_event_type("Fed holds rates; CPI cooler than expected") == "macro"
    assert classify_event_type("Acme holds annual shareholder meeting") == "other"


def test_direction_and_mixed_outcome() -> None:
    assert classify_direction_mixed("Acme upgraded, shares surge") == (Direction.BULLISH, False)
    assert classify_direction_mixed("Acme downgraded on weak demand") == (Direction.BEARISH, False)
    # Conflicting cues -> mixed, neutral direction.
    d, mixed = classify_direction_mixed("Acme beats on revenue but misses on EPS")
    assert d == Direction.NEUTRAL and mixed is True


def test_extract_actual_vs_estimate() -> None:
    vals = extract_values("Acme Q3 EPS of $1.23 vs $1.10 estimate; revenue $5.2B vs $5.0B")
    labels = {v.label: v for v in vals}
    assert "EPS" in labels and labels["EPS"].actual == 1.23 and labels["EPS"].estimate == 1.10
    assert labels["EPS"].beat is True
    assert "revenue" in labels and labels["revenue"].unit == "B"


def test_dedup_grouping_prefers_highest_authority_primary() -> None:
    items = [
        _item("Acme Corp beats Q3 earnings estimates", source="Reuters", ago_s=30),
        _item("Acme Corp tops Q3 earnings estimates", source="Benzinga", ago_s=10),
    ]
    cats = build_news_catalysts(items, for_symbol="AAPL")
    assert len(cats) == 1
    c = cats[0]
    assert c.member_count == 2
    assert c.source == "Reuters"  # highest authority is the primary
    assert set(c.sources) == {"Reuters", "Benzinga"}
    assert c.event_type == "earnings" and c.direction == Direction.BULLISH


def test_distinct_events_not_merged_and_ranked_by_confidence() -> None:
    items = [
        _item("Acme upgraded to Buy at Reuters", source="Reuters"),
        _item("Acme faces class-action lawsuit over data breach", source="blog"),
    ]
    cats = build_news_catalysts(items, for_symbol="AAPL")
    assert len(cats) == 2
    types = {c.event_type for c in cats}
    assert "rating_change" in types and "legal_regulatory" in types
    # Ranked by classification confidence (Reuters upgrade outranks the blog item).
    assert cats[0].confidence >= cats[1].confidence


def test_symbol_filter() -> None:
    items = [_item("Acme earnings beat", symbol="AAPL"), _item("Beta earnings miss", symbol="MSFT")]
    cats = build_news_catalysts(items, for_symbol="AAPL")
    assert all(c.symbol == "AAPL" for c in cats)


def test_news_events_endpoint() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.get("/short-duration/news/events/AAPL")
    assert r.status_code == 200
    assert isinstance(r.json(), list)  # informational list, possibly empty from the mock
