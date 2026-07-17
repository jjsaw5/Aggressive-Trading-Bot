"""publish_scan_events runs detectors over scan output and publishes to the bus."""

from __future__ import annotations

from datetime import UTC, datetime

import app.services.events_service as es
from app.domain.candidates import Thesis, TradeCandidate
from app.domain.enums import CandidateStatus, Direction, StrategyType
from app.domain.trades import RiskPlan, SpreadAnalytics, TradePlan
from app.events.bus import get_event_bus
from app.events.types import Event, EventType


def _candidate(symbol: str, spot: float, score: float = 0.7) -> TradeCandidate:
    plan = TradePlan(
        symbol=symbol,
        direction=Direction.BULLISH,
        strategy=StrategyType.BULL_CALL_SPREAD,
        legs=[],
        net_debit=150.0,
        contracts=1,
        risk=RiskPlan(
            max_loss_usd=150.0,
            account_risk_pct=0.075,
            profit_target_pct=0.5,
            stop_loss_pct=0.5,
        ),
        analytics=SpreadAnalytics(spot_at_analysis=spot),
    )
    return TradeCandidate(
        symbol=symbol,
        status=CandidateStatus.RANKED,
        composite_score=score,
        direction=Direction.BULLISH,
        thesis=Thesis(
            direction=Direction.BULLISH,
            why_now="test",
            flow_meaningful=True,
            price_confirms=True,
            has_catalyst=False,
            iv_favorable=True,
            invalidation="n/a",
        ),
        signals=[],
        trade_plan=plan,
        generated_at=datetime(2026, 7, 17, 15, 0, tzinfo=UTC),
        scan_id="scanE",
    )


def _reset_service_state() -> None:
    es._price._last.clear()
    es._proxy_last.clear()
    es._regime._last = None
    es._default_subscriber_registered = False
    get_event_bus().reset()


async def test_first_scan_is_baseline_then_price_change_fires() -> None:
    _reset_service_state()
    captured: list[Event] = []

    async def cap(e: Event) -> None:
        captured.append(e)

    get_event_bus().subscribe_all(cap)

    assert await es.publish_scan_events([_candidate("AAPL", 100.0)]) == 0  # baseline
    n = await es.publish_scan_events([_candidate("AAPL", 105.0)])  # +5% move
    assert n == 1
    assert any(
        e.type == EventType.PRICE_CHANGED and e.symbol == "AAPL" for e in captured
    )
    _reset_service_state()


async def test_no_material_move_publishes_nothing() -> None:
    _reset_service_state()
    await es.publish_scan_events([_candidate("MSFT", 400.0)])  # baseline
    n = await es.publish_scan_events([_candidate("MSFT", 400.2)])  # +0.05%
    assert n == 0
    _reset_service_state()


async def test_disabled_events_is_noop(monkeypatch) -> None:
    _reset_service_state()
    monkeypatch.setattr(es.settings, "events_enabled", False)
    await es.publish_scan_events([_candidate("AAPL", 100.0)])
    n = await es.publish_scan_events([_candidate("AAPL", 200.0)])
    assert n == 0  # disabled -> no events regardless of move
    _reset_service_state()
