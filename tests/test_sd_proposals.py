"""Short-duration Phase 7 — human-approved live proposals (gated OFF)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.enums import CandidateState, DTECategory, ProposalStatus
from app.shortduration import proposals

_NOW = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)


async def _tradeable_candidate():
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.SHORT_DTE, now=_NOW)
    cand = next((c for c in cands if c.trade_plan and c.state != CandidateState.REJECTED), None)
    assert cand is not None
    return cand


async def test_propose_advances_candidate_and_creates_ticket() -> None:
    from app.db import repository

    cand = await _tradeable_candidate()
    p = await proposals.propose(cand, now=_NOW)
    assert p.status == ProposalStatus.PENDING_APPROVAL
    assert p.scan_id == f"sd:{cand.id}"
    stored = repository.get_short_duration_candidate(cand.id)
    assert stored.state == CandidateState.PROPOSED
    # The full live-path trail is recorded.
    tos = [t.to_state.value for t in repository.list_candidate_transitions(cand.id)]
    assert "triggered" in tos and "proposed" in tos


async def test_approve_then_execute_is_denied_by_default() -> None:
    from app.db import repository

    cand = await _tradeable_candidate()
    p = await proposals.propose(cand, now=_NOW)
    approved = await proposals.approve(p.id, "tester", now=_NOW)
    assert approved.status == ProposalStatus.APPROVED
    assert approved.approved_by == "tester"
    assert repository.get_short_duration_candidate(cand.id).state == CandidateState.APPROVED

    # The execution guard denies under the safe defaults (research mode + off).
    decision = await proposals.execute(p.id, now=_NOW)
    assert decision.authorized is False
    assert "no_live_execution" in decision.reason or "kill_switch" in decision.reason


async def test_reject_marks_candidate_rejected() -> None:
    from app.db import repository

    cand = await _tradeable_candidate()
    p = await proposals.propose(cand, now=_NOW)
    rejected = await proposals.reject(p.id, "not now", now=_NOW)
    assert rejected.status == ProposalStatus.REJECTED
    assert repository.get_short_duration_candidate(cand.id).state == CandidateState.REJECTED


def test_propose_requires_a_sized_plan() -> None:
    from app.domain.shortduration import ShortDurationCandidate
    from app.modes.proposals import ProposalError
    from app.shortduration.proposals import create_short_duration_proposal

    bare = ShortDurationCandidate(
        id="x", symbol="SPY", dte_category=DTECategory.ZERO_DTE,
        detected_at=_NOW, state=CandidateState.ARMED, trade_plan=None,
    )
    with pytest.raises(ProposalError):
        create_short_duration_proposal(bare)


async def test_execute_never_authorizes_in_research_mode(monkeypatch) -> None:
    # Even a well-formed approved proposal must be denied while research mode /
    # automation-off. (Guards the "no unrestricted live trading" invariant.)
    from app.config import TradingMode, settings

    monkeypatch.setattr(settings, "trading_mode", TradingMode.RESEARCH, raising=False)
    monkeypatch.setattr(settings, "automation_enabled", False, raising=False)
    cand = await _tradeable_candidate()
    p = await proposals.propose(cand, now=_NOW)
    await proposals.approve(p.id, "tester", now=_NOW)
    assert (await proposals.execute(p.id, now=_NOW)).authorized is False


# --- State machine advance() -------------------------------------------------
def test_advance_prefers_direct_paper_path() -> None:
    from app.domain.shortduration import ShortDurationCandidate
    from app.shortduration.state import advance

    c = ShortDurationCandidate(id="a", symbol="SPY", dte_category=DTECategory.ZERO_DTE,
                               detected_at=_NOW, state=CandidateState.TRIGGERED)
    trail = advance(c, CandidateState.OPEN, trigger="paper_open")
    # TRIGGERED -> OPEN is a single direct hop (skips PROPOSED/APPROVED).
    assert [t.to_state for t in trail] == [CandidateState.OPEN]
    assert c.state == CandidateState.OPEN


def test_advance_walks_to_proposed() -> None:
    from app.domain.shortduration import ShortDurationCandidate
    from app.shortduration.state import advance

    c = ShortDurationCandidate(id="b", symbol="SPY", dte_category=DTECategory.ZERO_DTE,
                               detected_at=_NOW, state=CandidateState.ARMED)
    trail = advance(c, CandidateState.PROPOSED, trigger="propose")
    assert [t.to_state for t in trail] == [CandidateState.TRIGGERED, CandidateState.PROPOSED]


def test_proposal_api_flow() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.post("/short-duration/scans/1-5dte")
    cands = c.get("/short-duration/1-5dte/candidates?limit=50").json()
    target = next((x for x in cands if x.get("trade_plan") and x["state"] != "rejected"), None)
    assert target is not None
    proposed = c.post(f"/short-duration/candidates/{target['id']}/propose")
    assert proposed.status_code == 200 and proposed.json()["candidate"]["state"] == "proposed"

    props = c.get("/short-duration/proposals").json()
    pid = next(p["id"] for p in props if p["scan_id"] == f"sd:{target['id']}")
    assert c.post(f"/short-duration/proposals/{pid}/approve", json={"approver": "me"}).status_code == 200
    execd = c.post(f"/short-duration/proposals/{pid}/execute").json()
    assert execd["authorized"] is False  # gated: no live execution by default
