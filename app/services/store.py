"""In-memory store for candidates and proposals (MVP).

This is a deliberate placeholder so the API runs with zero infrastructure. The
persistence models in `app.db.models` + a repository layer will replace this;
route handlers already depend only on these functions, so swapping the backing
store is localized.
"""

from __future__ import annotations

import threading

from app.domain.candidates import TradeCandidate
from app.domain.trades import OrderProposal

_lock = threading.Lock()
_candidates: dict[str, TradeCandidate] = {}
_proposals: dict[str, OrderProposal] = {}


def save_candidates(candidates: list[TradeCandidate]) -> None:
    with _lock:
        for c in candidates:
            _candidates[f"{c.scan_id}:{c.symbol}"] = c


def get_candidate(scan_id: str, symbol: str) -> TradeCandidate | None:
    with _lock:
        return _candidates.get(f"{scan_id}:{symbol.upper()}")


def save_proposal(proposal: OrderProposal) -> None:
    with _lock:
        _proposals[proposal.id] = proposal


def get_proposal(proposal_id: str) -> OrderProposal | None:
    with _lock:
        return _proposals.get(proposal_id)


def list_proposals() -> list[OrderProposal]:
    with _lock:
        return list(_proposals.values())
