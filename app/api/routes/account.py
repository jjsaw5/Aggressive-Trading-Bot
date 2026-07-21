"""Account-state endpoint — the capital picture sizing reads."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from app.domain.account import AccountState
from app.providers import registry

router = APIRouter(prefix="/account", tags=["account"])


@router.get("/state", response_model=AccountState)
async def account_state() -> AccountState:
    """The account state sizing draws on: equity, buying power, and committed
    (open + pending) defined risk, with a `verified` flag and `source`. Paper and
    configured-fallback sources are always unverified — nothing becomes
    live-executable from a constant."""
    try:
        return await registry.account_state_provider().get_account_state(now=datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Account state unavailable: {exc}") from exc
