"""Pre-scoring rejections, and the conviction gate as an execution precondition.

Phase 0 of the remediation directive. All three rules exist because the audit of
build 7afa098 found the research record polluted in ways downstream analysis
cannot repair:

  * An AAPL call spread was picked #1 the day before earnings. The system had
    already detected the conflict and written it into thesis prose — advisory
    text that gated nothing.
  * All 38 audited 0DTE signals resolved `expiry`, because daily marks cannot see
    an intraday exit. The managed policy those rows claim to run was never
    actually measured, so collecting more of them adds rows, not information.
  * The conviction gate was RED and correct to be, but nothing consulted it
    before placing an order. A gate nothing consults is documentation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.config import settings
from app.domain.enums import DTECategory, ProposalStatus, RejectReason
from app.shortduration.capture_gates import (
    bucket_suspended,
    earnings_before_expiry,
    evaluate_capture_gates,
)

_EXP = date(2026, 8, 21)


# --- Earnings is a rejection, not a footnote ----------------------------------
def test_earnings_before_expiry_rejects_the_setup() -> None:
    r = earnings_before_expiry(date(2026, 8, 5), _EXP)
    assert r is not None and r.reason == RejectReason.EARNINGS_GATE
    assert "event binary" in r.detail


def test_earnings_on_the_expiry_date_itself_rejects() -> None:
    # A report ON expiration day resolves the position on the print. Inclusive.
    assert earnings_before_expiry(_EXP, _EXP) is not None


def test_earnings_after_expiry_is_fine() -> None:
    assert earnings_before_expiry(date(2026, 8, 22), _EXP) is None


def test_an_unknown_earnings_date_does_not_reject() -> None:
    # Absence of a known date is not evidence of an event; the guard would
    # otherwise reject every symbol whose calendar lookup failed.
    assert earnings_before_expiry(None, _EXP) is None
    assert earnings_before_expiry(date(2026, 8, 5), None) is None


def test_the_gate_can_be_configured_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "capture_earnings_hard_gate", False)
    assert earnings_before_expiry(date(2026, 8, 5), _EXP) is None


# --- Suspended buckets --------------------------------------------------------
def test_zero_dte_is_suspended_by_default() -> None:
    r = bucket_suspended(DTECategory.ZERO_DTE)
    assert r is not None and r.reason == RejectReason.BUCKET_SUSPENDED


def test_other_buckets_are_not_suspended() -> None:
    assert bucket_suspended(DTECategory.SHORT_DTE) is None
    assert bucket_suspended(DTECategory.MEDIUM_DTE) is None


def test_suspension_is_configurable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "capture_suspended_buckets", "1-5dte,medium")
    assert bucket_suspended(DTECategory.ZERO_DTE) is None
    assert bucket_suspended(DTECategory.SHORT_DTE) is not None


def test_an_empty_suspension_list_suspends_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "capture_suspended_buckets", "")
    assert bucket_suspended(DTECategory.ZERO_DTE) is None


# --- Combined evaluation ------------------------------------------------------
def test_suspension_is_reported_before_earnings(monkeypatch) -> None:
    # Both apply; the bucket rule is the broader statement, so it wins.
    r = evaluate_capture_gates(
        dte=DTECategory.ZERO_DTE, symbol="AAPL",
        next_earnings=date(2026, 8, 5), expiration=_EXP,
    )
    assert r.reason == RejectReason.BUCKET_SUSPENDED


def test_the_aapl_case_cannot_reach_a_candidate(monkeypatch) -> None:
    # THE regression, by name: 1-5DTE (not suspended), earnings before expiry.
    monkeypatch.setattr(settings, "capture_suspended_buckets", "")
    r = evaluate_capture_gates(
        dte=DTECategory.SHORT_DTE, symbol="AAPL",
        next_earnings=date(2026, 7, 30), expiration=date(2026, 7, 31),
    )
    assert r is not None and r.reason == RejectReason.EARNINGS_GATE


def test_a_clean_setup_passes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "capture_suspended_buckets", "")
    assert evaluate_capture_gates(
        dte=DTECategory.SHORT_DTE, symbol="SPY",
        next_earnings=date(2026, 12, 1), expiration=_EXP,
    ) is None


# --- The conviction gate now blocks execution ---------------------------------
def _approved_proposal():
    from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
    from app.domain.trades import ContractLeg, OrderProposal, RiskPlan, TradePlan

    plan = TradePlan(
        symbol="SPY", direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD,
        legs=[
            ContractLeg(symbol="SPY", action=OptionAction.BUY_TO_OPEN,
                        option_type=OptionType.CALL, strike=500.0, expiration=_EXP,
                        quantity=1, entry_price=2.0),
            ContractLeg(symbol="SPY", action=OptionAction.SELL_TO_OPEN,
                        option_type=OptionType.CALL, strike=505.0, expiration=_EXP,
                        quantity=1, entry_price=1.0),
        ],
        net_debit=100.0, contracts=1,
        risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                      profit_target_pct=0.5, stop_loss_pct=0.5),
    )
    return OrderProposal(
        id="p1", scan_id="s1", symbol="SPY", trade_plan=plan,
        thesis_summary="test fixture",
        status=ProposalStatus.APPROVED, created_at=datetime.now(UTC),
    )


def test_a_red_conviction_gate_denies_execution(monkeypatch) -> None:
    # Signal-only capture mode: approval + armed automation are NOT sufficient
    # while the engine has not demonstrated calibration.
    from app.config import TradingMode
    from app.modes.execution_guard import ExecutionGuard

    monkeypatch.setattr(settings, "trading_mode", TradingMode.AUTOMATION)
    monkeypatch.setattr(settings, "automation_enabled", True)
    d = ExecutionGuard().authorize(_approved_proposal())
    assert d.authorized is False
    assert d.reason.startswith("conviction_gate_red[")


def test_an_unevaluable_gate_denies_rather_than_allows(monkeypatch) -> None:
    # Fails CLOSED. An engine that cannot show calibration is, from the broker's
    # side, indistinguishable from one that has none.
    import app.modes.execution_guard as eg
    import app.shortduration.conviction_gate as cg
    from app.config import TradingMode

    monkeypatch.setattr(settings, "trading_mode", TradingMode.AUTOMATION)
    monkeypatch.setattr(settings, "automation_enabled", True)

    def _boom():
        raise RuntimeError("warehouse unreachable")

    monkeypatch.setattr(cg, "get_conviction_gate", _boom)
    d = eg.ExecutionGuard().authorize(_approved_proposal())
    assert d.authorized is False and d.reason == "conviction_gate_unevaluable"


def test_approval_is_still_checked_before_the_gate() -> None:
    # Ordering matters for the deny REASON a human reads; the earliest failure
    # should be the one reported.
    from app.domain.enums import ProposalStatus as PS
    from app.modes.execution_guard import ExecutionGuard

    p = _approved_proposal()
    p.status = PS.DRAFT
    assert ExecutionGuard().authorize(p).reason == "proposal_not_approved"
