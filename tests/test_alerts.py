"""Alert construction, filtering, routing, and Slack payload."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.alerts.base import Alert, Severity
from app.alerts.notifiers import ConsoleNotifier, NoopNotifier, slack_payload
from app.alerts.service import build_candidate_alerts, candidate_to_alert, get_notifier
from app.domain.candidates import Thesis, TradeCandidate
from app.domain.enums import CandidateStatus, Direction
from app.main import app

client = TestClient(app)


def _candidate(symbol: str, score: float, actionable: bool) -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        status=CandidateStatus.RANKED if actionable else CandidateStatus.REJECTED,
        composite_score=score,
        direction=Direction.BULLISH,
        thesis=Thesis(
            direction=Direction.BULLISH, why_now="flow + trend", flow_meaningful=True,
            price_confirms=True, has_catalyst=False, iv_favorable=True,
            invalidation="x",
        ),
        trade_plan=None,  # actionability also needs a plan; patched below
        generated_at=datetime.now(UTC),
        scan_id="s1",
    )


def test_build_alerts_filters_by_score_and_actionability(monkeypatch) -> None:
    # is_actionable requires RANKED + a trade_plan; fake a plan-bearing candidate
    # by monkeypatching the property via a simple stand-in.
    high = _candidate("AAA", 0.80, True)
    low = _candidate("BBB", 0.40, True)

    # Force is_actionable True for both (plan presence is exercised elsewhere).
    import app.domain.candidates as cand_mod

    monkeypatch.setattr(cand_mod.TradeCandidate, "is_actionable", property(lambda self: True))

    alerts = build_candidate_alerts([high, low], min_score=0.6)
    assert len(alerts) == 1
    assert alerts[0].symbol == "AAA"
    assert alerts[0].score == 0.80


def test_candidate_to_alert_has_symbol_and_score() -> None:
    c = _candidate("NVDA", 0.7, True)
    a = candidate_to_alert(c)
    assert a.symbol == "NVDA"
    assert a.score == 0.7
    assert "flow" in a.message


def test_slack_payload_shape() -> None:
    a = Alert(title="BULLISH setup", message="why", symbol="SPY", score=0.82,
              severity=Severity.INFO)
    payload = slack_payload(a)
    assert "text" in payload
    assert "SPY" in payload["text"]
    assert "0.82" in payload["text"]


def test_get_notifier_defaults_to_noop_when_disabled(monkeypatch) -> None:
    from app.alerts import service as svc

    monkeypatch.setattr(svc.settings, "alerts_enabled", False)
    assert isinstance(get_notifier(), NoopNotifier)


def test_get_notifier_console_when_enabled(monkeypatch) -> None:
    from app.alerts import service as svc

    monkeypatch.setattr(svc.settings, "alerts_enabled", True)
    monkeypatch.setattr(svc.settings, "alerts_channel", "console")
    assert isinstance(get_notifier(), ConsoleNotifier)


def test_slack_falls_back_to_console_without_webhook(monkeypatch) -> None:
    from app.alerts import service as svc

    monkeypatch.setattr(svc.settings, "alerts_enabled", True)
    monkeypatch.setattr(svc.settings, "alerts_channel", "slack")
    monkeypatch.setattr(svc.settings, "slack_webhook_url", None)
    assert isinstance(get_notifier(), ConsoleNotifier)


def test_alerts_status_endpoint() -> None:
    r = client.get("/alerts/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False  # disabled by default
    assert body["resolved_notifier"] == "noop"


def test_alerts_test_endpoint_noop_by_default() -> None:
    r = client.post("/alerts/test")
    assert r.status_code == 200
    assert r.json()["sent_via"] == "noop"
