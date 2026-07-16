"""Alert model and the Notifier interface."""

from __future__ import annotations

import abc
from enum import Enum

from pydantic import BaseModel


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"


class Alert(BaseModel):
    title: str
    message: str
    severity: Severity = Severity.INFO
    symbol: str | None = None
    score: float | None = None


class Notifier(abc.ABC):
    name: str

    @abc.abstractmethod
    async def send(self, alert: Alert) -> None: ...

    async def send_all(self, alerts: list[Alert]) -> int:
        sent = 0
        for a in alerts:
            await self.send(a)
            sent += 1
        return sent
