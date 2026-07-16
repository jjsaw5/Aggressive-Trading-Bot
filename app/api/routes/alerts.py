"""Alert endpoints — status and a manual test send.

Alerts are disabled by default; these endpoints make the configured channel
observable and let an operator verify wiring without waiting for a scan.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.alerts.base import Alert, Severity
from app.alerts.service import get_notifier
from app.config import settings

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertStatus(BaseModel):
    enabled: bool
    channel: str
    min_score: float
    resolved_notifier: str


@router.get("/status", response_model=AlertStatus)
async def status() -> AlertStatus:
    return AlertStatus(
        enabled=settings.alerts_enabled,
        channel=settings.alerts_channel,
        min_score=settings.alerts_min_score,
        resolved_notifier=get_notifier().name,
    )


@router.post("/test")
async def send_test() -> dict:
    notifier = get_notifier()
    await notifier.send(
        Alert(
            title="Test alert",
            message="Alert wiring is working.",
            severity=Severity.INFO,
            symbol="SPY",
            score=0.99,
        )
    )
    return {"sent_via": notifier.name, "enabled": settings.alerts_enabled}
