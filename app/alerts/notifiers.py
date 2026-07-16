"""Concrete notifiers: noop, console (structured log), and Slack webhook."""

from __future__ import annotations

import httpx

from app.alerts.base import Alert, Notifier
from app.logging_config import get_logger

log = get_logger(__name__)


class NoopNotifier(Notifier):
    name = "noop"

    async def send(self, alert: Alert) -> None:
        return None


class ConsoleNotifier(Notifier):
    name = "console"

    async def send(self, alert: Alert) -> None:
        log.info(
            "alert",
            title=alert.title,
            severity=alert.severity.value,
            symbol=alert.symbol,
            score=alert.score,
            message=alert.message,
        )


def slack_payload(alert: Alert) -> dict:
    """Build a Slack incoming-webhook payload (pure — unit-testable)."""
    emoji = ":rotating_light:" if alert.severity.value == "warning" else ":chart_with_upwards_trend:"
    score = f" (score {alert.score:.2f})" if alert.score is not None else ""
    sym = f"*{alert.symbol}*  " if alert.symbol else ""
    return {"text": f"{emoji} {sym}{alert.title}{score}\n{alert.message}"}


class SlackNotifier(Notifier):
    name = "slack"

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    async def send(self, alert: Alert) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self._url, json=slack_payload(alert))
            if resp.status_code >= 400:
                log.warning("slack_alert_failed", status=resp.status_code, body=resp.text[:200])
