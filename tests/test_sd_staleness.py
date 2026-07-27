"""Staleness sweep: yesterday's candidates must never be served as live.

Regression for the Monday-morning board bug: a 0DTE candidate detected Friday
(legs expiring Friday) still rendered as an actionable watchlist row on Monday,
frozen quote-age badge and all — nothing ever retired pre-flight candidates.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import CandidateState, DTECategory
from app.domain.shortduration import ContractRecommendation, ShortDurationCandidate
from app.shortduration.state import staleness_reason

_FRI = datetime(2026, 7, 24, 14, 30, tzinfo=UTC)
_MON = datetime(2026, 7, 27, 13, 40, tzinfo=UTC)


def _cand(*, state=CandidateState.WATCHLIST, dte=DTECategory.ZERO_DTE,
          detected_at=_FRI, exp: str | None = "2026-07-24") -> ShortDurationCandidate:
    contract = None
    if exp is not None:
        contract = ContractRecommendation(
            description="Put Debit Spread x1",
            legs=[{"option_type": "put", "strike": 230.0, "expiration": exp},
                  {"option_type": "put", "strike": 222.5, "expiration": exp}],
        )
    return ShortDurationCandidate(
        id="c1", symbol="AMZN", dte_category=dte, detected_at=detected_at,
        state=state, contract=contract,
    )


def test_expired_contract_is_stale_on_monday() -> None:
    why = staleness_reason(_cand(), now=_MON)
    assert why is not None and "2026-07-24" in why


def test_zero_dte_is_stale_next_day_even_without_contract() -> None:
    # A 0DTE setup is same-day by definition — its opening-range/VWAP levels
    # mean nothing tomorrow, whatever contract (if any) got attached.
    why = staleness_reason(_cand(exp=None), now=_MON)
    assert why is not None and "setup day" in why


def test_same_day_candidate_is_not_stale() -> None:
    fresh = _cand(detected_at=_MON, exp="2026-07-27")
    assert staleness_reason(fresh, now=_MON) is None


def test_short_dte_with_live_expiry_survives_the_weekend() -> None:
    # A 1-5DTE (or swing) candidate whose contract still exists is NOT swept.
    c = _cand(dte=DTECategory.SHORT_DTE, exp="2026-08-21")
    assert staleness_reason(c, now=_MON) is None


def test_open_and_terminal_states_are_never_swept() -> None:
    # OPEN/MANAGING belong to the position monitor (which settles at expiry);
    # terminal states are already dead.
    for st in (CandidateState.OPEN, CandidateState.MANAGING,
               CandidateState.CLOSED, CandidateState.REJECTED):
        assert staleness_reason(_cand(state=st), now=_MON) is None


def test_proposal_with_expired_contract_is_swept_but_day_rule_spares_it() -> None:
    # A PROPOSED/APPROVED row is human-touched: the sweep retires it ONLY when
    # its contract has expired (physically unexecutable) — the softer
    # 0DTE-day rule leaves it alone.
    why = staleness_reason(_cand(state=CandidateState.PROPOSED), now=_MON)
    assert why is not None and "contract expired" in why
    assert staleness_reason(_cand(state=CandidateState.PROPOSED, exp=None), now=_MON) is None


async def test_monitor_settles_position_held_past_expiry(monkeypatch) -> None:
    # A paper position whose contract expired can't be marked from the live
    # chain — the monitor must settle it at expiry-day intrinsic, timestamped
    # at expiry, and close the candidate.
    import uuid
    from datetime import date

    from app.db import repository
    from app.domain.enums import (
        Direction,
        OptionAction,
        OptionType,
        PaperTradeStatus,
        StrategyType,
    )
    from app.domain.market import Candle, PriceHistory
    from app.domain.shortduration import ShortDurationTrade
    from app.domain.trades import ContractLeg, PaperTrade, RiskPlan, TradePlan
    from app.shortduration import paper as paper_mod

    leg = ContractLeg(symbol="TSLA", action=OptionAction.BUY_TO_OPEN,
                      option_type=OptionType.CALL, strike=100.0,
                      expiration=date(2026, 7, 24), quantity=1, entry_price=1.0)
    plan = TradePlan(symbol="TSLA", direction=Direction.BULLISH,
                     strategy=StrategyType.LONG_CALL, legs=[leg], net_debit=100.0,
                     contracts=1,
                     risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                                   profit_target_pct=0.5, stop_loss_pct=0.5))
    pt = PaperTrade(id=uuid.uuid4().hex[:12], scan_id="sd", symbol="TSLA",
                    trade_plan=plan, status=PaperTradeStatus.OPEN,
                    opened_at=datetime(2026, 7, 20, 15, tzinfo=UTC), entry_fill=1.0)
    repository.save_paper_trade(pt)
    sd = ShortDurationTrade(id=uuid.uuid4().hex[:12], candidate_id="nope",
                            paper_trade_id=pt.id, symbol="TSLA",
                            dte_category=DTECategory.SHORT_DTE,
                            opened_at=pt.opened_at, entry_net=1.0)
    repository.save_short_duration_trade(sd)

    class _MD:
        async def get_price_history(self, symbol, lookback_days=90):
            return PriceHistory(symbol=symbol, candles=[
                Candle(ts=datetime(2026, 7, 24, 20, tzinfo=UTC), open=100, high=104,
                       low=99, close=103.0, volume=1)])

    monkeypatch.setattr(paper_mod.registry, "market_data_provider", lambda: _MD())
    await paper_mod.monitor_short_duration_positions(now=datetime(2026, 7, 27, 18, tzinfo=UTC))

    settled = repository.get_paper_trade(pt.id)
    assert settled.status == PaperTradeStatus.CLOSED
    assert settled.exit_reason.value == "expiry"
    assert settled.exit_fill == 3.0  # intrinsic of the 100C at a 103 close
    assert settled.realized_pnl_usd == 200.0
    assert settled.closed_at.date() == date(2026, 7, 24)  # booked ON expiry day


def test_listing_persists_the_expiry() -> None:
    # End to end through the repository: a stale Friday row comes back EXPIRED
    # from the board listing, and the transition is durably recorded.
    import uuid

    from app.db import repository

    c = _cand()
    c.id = uuid.uuid4().hex[:12]
    repository.save_short_duration_candidate(c)
    repository.list_short_duration_candidates(dte_category="0dte", limit=500)
    stored = repository.get_short_duration_candidate(c.id)
    assert stored is not None and stored.state == CandidateState.EXPIRED
    trail = repository.list_candidate_transitions(c.id)
    assert any(t.to_state == CandidateState.EXPIRED and "expired" in t.reason
               for t in trail)
