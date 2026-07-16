"""FastAPI application entrypoint.

Exposes the research/decision-support surface: health, provider status, scans,
candidates, and proposal lifecycle. Live order placement is NOT exposed here —
it lives behind the execution guard and is disabled by default.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import backtest, health, proposals, scans
from app.api.routes import config as config_routes
from app.config import settings
from app.logging_config import configure_logging, get_logger

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "startup",
        app_env=settings.app_env,
        trading_mode=settings.trading_mode.value,
        automation_armed=settings.automation_armed,
    )
    yield
    log.info("shutdown")


app = FastAPI(
    title="Aggressive Trading Bot — Research & Decision Support",
    version="0.1.0",
    description=(
        "Options-trading research and decision-support platform. Produces ranked "
        "candidates, defined-risk trade plans, and human-approval proposals. Does "
        "not place live trades (automation disabled by default)."
    ),
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(config_routes.router)
app.include_router(scans.router)
app.include_router(proposals.router)
app.include_router(backtest.router)
