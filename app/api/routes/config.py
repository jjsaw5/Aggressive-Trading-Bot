"""Configuration + provider-status endpoints (read-only, no secrets)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.engine.universe import UniverseConfig
from app.providers import registry
from app.risk.policy import RiskPolicy

router = APIRouter(prefix="/config", tags=["config"])


class ProviderStatus(BaseModel):
    capability: str
    provider: str
    verified: bool
    requires_auth: bool
    typical_delay: str | None
    rate_limit: str | None
    licensing: str | None
    docs_url: str | None
    error: str | None = None


class RuntimeConfig(BaseModel):
    app_env: str
    trading_mode: str
    automation_armed: bool
    universe: list[str]
    risk_policy: dict


@router.get("/runtime", response_model=RuntimeConfig)
async def runtime_config() -> RuntimeConfig:
    policy = RiskPolicy.from_settings()
    return RuntimeConfig(
        app_env=settings.app_env,
        trading_mode=settings.trading_mode.value,
        automation_armed=settings.automation_armed,
        universe=UniverseConfig().normalized_symbols(),
        risk_policy={
            "account_equity_usd": policy.account_equity_usd,
            "max_trade_risk_usd": policy.max_trade_risk_usd,
            "max_account_risk_usd": policy.max_account_risk_usd,
            "max_concurrent_positions": policy.max_concurrent_positions,
        },
    )


@router.get("/providers", response_model=list[ProviderStatus])
async def provider_status() -> list[ProviderStatus]:
    resolvers = {
        "market_data": registry.market_data_provider,
        "fundamentals": registry.fundamentals_provider,
        "options_chain": registry.options_chain_provider,
        "options_flow": registry.options_flow_provider,
        "calendar": registry.calendar_provider,
        "brokerage": registry.brokerage_provider,
    }
    out: list[ProviderStatus] = []
    for capability, resolve in resolvers.items():
        try:
            provider = resolve()
            meta = provider.meta
            out.append(
                ProviderStatus(
                    capability=capability,
                    provider=meta.name,
                    verified=meta.verified,
                    requires_auth=meta.requires_auth,
                    typical_delay=meta.typical_delay,
                    rate_limit=meta.rate_limit,
                    licensing=meta.licensing,
                    docs_url=meta.docs_url,
                )
            )
        except Exception as exc:
            out.append(
                ProviderStatus(
                    capability=capability,
                    provider="unresolved",
                    verified=False,
                    requires_auth=True,
                    typical_delay=None,
                    rate_limit=None,
                    licensing=None,
                    docs_url=None,
                    error=str(exc),
                )
            )
    return out
