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
from app.domain.internals import MarketInternals
from app.domain.options import FlowAlert, OptionChain
from app.domain.shortduration import (
    CandidateTransition,
    EconomicEvent,
    IntradayLevels,
    NewsItem,
    ShortDurationCandidate,
    ShortDurationRegimeState,
    ShortDurationTrade,
)
from app.domain.trades import OrderProposal
from app.engine.universe import DEFAULT_UNIVERSE
from app.providers import registry
from app.shortduration import service
from app.shortduration.breadth import WatchlistParticipation
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
    participation: WatchlistParticipation      # our-universe proxy
    internals: MarketInternals | None = None   # real market internals, when available
    breadth: WatchlistParticipation            # deprecated alias of `participation`
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
    regime, levels, participation, internals = await service.build_market_regime()
    return RegimeResponse(
        regime=regime, participation=participation, internals=internals, breadth=participation,
        levels=sorted(levels.values(), key=lambda x: x.symbol),
    )


@router.get("/market/internals")
async def market_internals() -> MarketInternals | None:
    """Real market-wide internals (sector breadth + options-flow tide). Fields not
    available on the current provider keys are returned null + listed."""
    return await service._market_internals(datetime.now(UTC))


@router.get("/market/participation", response_model=WatchlistParticipation)
async def market_participation() -> WatchlistParticipation:
    """Watchlist participation — a PROXY over our tracked universe, not exchange breadth."""
    _regime, _levels, participation, _internals = await service.build_market_regime()
    return participation


@router.get("/candidates/{candidate_id}/freshness")
async def candidate_freshness(candidate_id: str) -> dict:
    """Re-evaluate the candidate's underlying-quote freshness against the policy for
    its current state/track — the check a trade-ready 0DTE name must pass."""
    from app.domain.enums import CandidateState
    from app.shortduration.freshness import evaluate_quote_freshness

    cand = await run_in_threadpool(repository.get_short_duration_candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "Candidate not found.")
    try:
        q = await registry.market_data_provider().get_quote(cand.symbol)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Quote unavailable: {exc}") from exc
    fr = evaluate_quote_freshness(
        as_of=q.as_of, delayed_minutes=q.delayed_minutes, now=datetime.now(UTC),
        capability="underlying", state=CandidateState(cand.state) if cand.state else None,
        dte=DTECategory(cand.dte_category), provider=q.source,
    )
    return {"candidate_id": candidate_id, "state": cand.state.value if hasattr(cand.state, "value") else cand.state,
            "recorded": cand.freshness, "current": fr.model_dump()}


@router.get("/configuration/freshness")
async def configuration_freshness() -> dict:
    """The data-freshness budgets (seconds) by use-case and capability."""
    return {
        "broad": {"underlying_s": settings.freshness_broad_underlying_s, "option_s": settings.freshness_broad_option_s},
        "watchlist": {"underlying_s": settings.freshness_watchlist_underlying_s, "option_s": settings.freshness_watchlist_option_s},
        "armed_0dte": {"underlying_s": settings.freshness_armed_underlying_s, "option_s": settings.freshness_armed_option_s,
                       "internals_s": settings.freshness_armed_internals_s, "account_s": settings.freshness_armed_account_s},
        "open_0dte": {"underlying_s": settings.freshness_open_underlying_s, "option_s": settings.freshness_open_option_s},
    }


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
    """Manual candidate action: arm / reject / watchlist (state machine) or
    `paper` (open a simulated position). Never places a live order."""
    cand = await run_in_threadpool(repository.get_short_duration_candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "Candidate not found.")

    if action in ("paper", "propose"):
        try:
            if action == "paper":
                from app.shortduration.paper import open_short_duration_paper

                await open_short_duration_paper(cand)
            else:  # propose: create a human-approval ticket (live path, gated)
                from app.shortduration.proposals import propose

                await propose(cand)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
            raise HTTPException(400, f"Cannot {action}: {exc}") from exc
        cand = await run_in_threadpool(repository.get_short_duration_candidate, candidate_id)
        transitions = await run_in_threadpool(repository.list_candidate_transitions, candidate_id)
        return CandidateDetail(candidate=cand, transitions=transitions)

    target = _MANUAL_TRANSITIONS.get(action)
    if target is None:
        raise HTTPException(
            400, f"Unsupported action '{action}'. Allowed: {sorted(_MANUAL_TRANSITIONS)} + paper"
        )
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


# --- Paper positions + performance ------------------------------------------
@router.get("/positions", response_model=list[ShortDurationTrade])
async def positions(status: str = "open", limit: int = 200) -> list[ShortDurationTrade]:
    return await run_in_threadpool(
        repository.list_short_duration_trades, status=status, limit=limit
    )


@router.post("/positions/monitor", response_model=list[ShortDurationTrade])
async def monitor_positions() -> list[ShortDurationTrade]:
    """Mark open short-duration paper positions and apply exits. No live order."""
    from app.shortduration.paper import monitor_short_duration_positions

    return await monitor_short_duration_positions()


@router.get("/performance")
async def performance() -> dict:
    from app.shortduration.paper import short_duration_performance

    return await run_in_threadpool(short_duration_performance)


# --- Human-approved live proposals (GATED — execution denied by default) ----
class ApproveRequest(BaseModel):
    approver: str = "dashboard"


class RejectRequest(BaseModel):
    note: str | None = None


@router.get("/proposals", response_model=list[OrderProposal])
async def list_proposals(limit: int = 100) -> list[OrderProposal]:
    from app.shortduration.proposals import list_sd_proposals

    return await run_in_threadpool(list_sd_proposals, limit)


@router.post("/proposals/{proposal_id}/approve", response_model=OrderProposal)
async def approve_proposal(proposal_id: str, req: ApproveRequest) -> OrderProposal:
    from app.shortduration.proposals import approve

    try:
        return await approve(proposal_id, req.approver)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@router.post("/proposals/{proposal_id}/reject", response_model=OrderProposal)
async def reject_proposal(proposal_id: str, req: RejectRequest) -> OrderProposal:
    from app.shortduration.proposals import reject

    try:
        return await reject(proposal_id, req.note)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@router.post("/proposals/{proposal_id}/execute")
async def execute_proposal(proposal_id: str) -> dict:
    """Route an APPROVED proposal through the ExecutionGuard. Denied by default
    (research mode + automation off); never places an order here."""
    from app.shortduration.proposals import execute

    try:
        decision = await execute(proposal_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {"authorized": decision.authorized, "reason": decision.reason}


@router.get("/backtest/classification")
async def backtest_classification() -> dict:
    """Honest backtest FIDELITY per DTE — reconstructed / approximate / proxy /
    not-testable — given the data actually available. Never fabricates results."""
    from app.shortduration.backtest import classify_all

    return {"classifications": [c.model_dump() for c in classify_all()]}


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
