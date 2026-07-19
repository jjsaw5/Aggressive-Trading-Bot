"""Short-duration (0DTE / 1-5DTE) read-only API — Phase 1.

Exposes market regime, intraday levels, option chain, flow, news, macro events,
context candidates, and the candidate state machine. No live-order endpoints are
exposed here; any future execution flows through the existing ExecutionGuard.

Conventions mirror the rest of the API: APIRouter(prefix, tags), pydantic
response_models, and DB work handed to a threadpool.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.config import settings
from app.db import repository
from app.domain.enums import CandidateState, DTECategory
from app.domain.options import FlowAlert, OptionChain
from app.domain.shortduration import (
    CandidateTransition,
    EconomicEvent,
    IntradayLevels,
    NewsItem,
    ShortDurationCandidate,
    ShortDurationRegimeState,
)
from app.engine.universe import DEFAULT_UNIVERSE
from app.providers import registry
from app.shortduration import service
from app.shortduration.breadth import BreadthProxy
from app.shortduration.detection import run_detection

router = APIRouter(prefix="/short-duration", tags=["short-duration"])

# States a user may drive from the board in Phase 1 (manual, to exercise the
# machine). Detection-driven transitions arrive with the strategy engine.
_MANUAL_TRANSITIONS = {
    "arm": CandidateState.ARMED,
    "reject": CandidateState.REJECTED,
    "watchlist": CandidateState.WATCHLIST,
}


class RegimeResponse(BaseModel):
    regime: ShortDurationRegimeState
    breadth: BreadthProxy
    levels: list[IntradayLevels]


class CandidateDetail(BaseModel):
    candidate: ShortDurationCandidate
    transitions: list[CandidateTransition]


class ScanResult(BaseModel):
    dte_category: str
    created: int
    note: str


class ConfigResponse(BaseModel):
    enabled: bool
    trading_mode: str
    live_trading_enabled: bool
    universe: list[str]
    providers: dict[str, str]
    note: str


# --- Market context ---------------------------------------------------------
@router.get("/market-regime", response_model=RegimeResponse)
async def market_regime() -> RegimeResponse:
    regime, levels, breadth = await service.build_market_regime()
    return RegimeResponse(
        regime=regime, breadth=breadth, levels=sorted(levels.values(), key=lambda x: x.symbol)
    )


@router.get("/levels/{symbol}", response_model=IntradayLevels)
async def symbol_levels(symbol: str) -> IntradayLevels:
    lv = await service.get_symbol_levels(symbol)
    if lv is None:
        raise HTTPException(404, f"No intraday levels available for {symbol.upper()}.")
    return lv


@router.get("/options/{symbol}", response_model=OptionChain)
async def options(symbol: str) -> OptionChain:
    """Near-dated option chain for the symbol (nearest expirations)."""
    try:
        return await registry.options_chain_provider().get_option_chain(
            symbol.upper(), expirations=2
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
        raise HTTPException(502, f"Option chain unavailable: {exc}") from exc


@router.get("/flow/{symbol}", response_model=list[FlowAlert])
async def flow(symbol: str, limit: int = 50) -> list[FlowAlert]:
    try:
        return await registry.options_flow_provider().get_flow_alerts(
            symbol.upper(), unusual_only=True, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Flow unavailable: {exc}") from exc


@router.get("/news", response_model=list[NewsItem])
async def news_all(limit: int = 50) -> list[NewsItem]:
    return await service.get_news(limit=limit)


@router.get("/news/{symbol}", response_model=list[NewsItem])
async def news_symbol(symbol: str, limit: int = 50) -> list[NewsItem]:
    return await service.get_news([symbol.upper()], limit=limit)


@router.get("/events", response_model=list[EconomicEvent])
async def events() -> list[EconomicEvent]:
    return await service.get_events()


# --- Candidates + state machine ---------------------------------------------
@router.get("/0dte/candidates", response_model=list[ShortDurationCandidate])
async def zero_dte_candidates(limit: int = 100) -> list[ShortDurationCandidate]:
    return await run_in_threadpool(
        repository.list_short_duration_candidates, dte_category="0dte", limit=limit
    )


@router.get("/1-5dte/candidates", response_model=list[ShortDurationCandidate])
async def short_dte_candidates(limit: int = 100) -> list[ShortDurationCandidate]:
    return await run_in_threadpool(
        repository.list_short_duration_candidates, dte_category="1-5dte", limit=limit
    )


@router.get("/candidates/{candidate_id}", response_model=CandidateDetail)
async def candidate_detail(candidate_id: str) -> CandidateDetail:
    cand = await run_in_threadpool(repository.get_short_duration_candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "Candidate not found.")
    transitions = await run_in_threadpool(
        repository.list_candidate_transitions, candidate_id
    )
    return CandidateDetail(candidate=cand, transitions=transitions)


@router.post("/scans/0dte", response_model=ScanResult)
async def scan_0dte() -> ScanResult:
    created = await run_detection(DTECategory.ZERO_DTE)
    return ScanResult(
        dte_category="0dte", created=len(created),
        note="Opening-range breakout + VWAP continuation. Setups only — no contract/order yet.",
    )


@router.post("/scans/1-5dte", response_model=ScanResult)
async def scan_short_dte() -> ScanResult:
    created = await run_detection(DTECategory.SHORT_DTE)
    return ScanResult(
        dte_category="1-5dte", created=len(created),
        note="Trend + catalyst continuation. Setups only — no contract/order yet.",
    )


@router.post("/candidates/{candidate_id}/{action}", response_model=CandidateDetail)
async def transition_candidate(candidate_id: str, action: str) -> CandidateDetail:
    """Manual state transition (Phase 1 supports arm / reject / watchlist to
    exercise the machine). Never places an order."""
    target = _MANUAL_TRANSITIONS.get(action)
    if target is None:
        raise HTTPException(
            400, f"Unsupported action '{action}'. Allowed: {sorted(_MANUAL_TRANSITIONS)}"
        )
    cand = await run_in_threadpool(repository.get_short_duration_candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "Candidate not found.")
    now = datetime.now(UTC)
    prev = cand.state
    cand.state = target
    await run_in_threadpool(repository.save_short_duration_candidate, cand)
    await run_in_threadpool(
        repository.append_candidate_transition,
        CandidateTransition(
            candidate_id=candidate_id, from_state=prev, to_state=target, at=now,
            trigger=f"manual:{action}", actor="dashboard",
            reason=f"Manual {action} via UI.", score_at=cand.score,
        ),
    )
    transitions = await run_in_threadpool(
        repository.list_candidate_transitions, candidate_id
    )
    return CandidateDetail(candidate=cand, transitions=transitions)


# --- Configuration ----------------------------------------------------------
@router.get("/configuration", response_model=ConfigResponse)
async def configuration() -> ConfigResponse:
    return ConfigResponse(
        enabled=settings.short_duration_enabled,
        trading_mode=settings.trading_mode.value,
        live_trading_enabled=settings.automation_armed,
        universe=list(DEFAULT_UNIVERSE),
        providers={
            "intraday": settings.provider_intraday.value,
            "news": settings.provider_news.value,
            "econ_calendar": settings.provider_econ_calendar.value,
            "options_chain": settings.provider_options_chain.value,
            "options_flow": settings.provider_options_flow.value,
        },
        note="Research/paper only. Live trading is disabled for this module.",
    )
