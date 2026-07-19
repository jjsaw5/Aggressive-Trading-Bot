"""Short-duration Phase 6 — validation: loop, backtest fidelity, failure sims,
scenario gates."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.domain.enums import Direction, DTECategory, ShortDurationRegime
from app.shortduration.backtest import BacktestFidelity, classify_short_duration_backtest
from app.shortduration.loop import ShortDurationLoop

_ET = ZoneInfo("America/New_York")
_FRI_RTH = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)  # Fri 11:00 ET
_SAT = datetime(2026, 7, 18, 15, 0, tzinfo=UTC)  # weekend


# --- Loop --------------------------------------------------------------------
def test_loop_due_jobs_respect_cadence() -> None:
    loop = ShortDurationLoop()
    assert loop.due_jobs(0.0) == ["monitor", "scan_0dte", "scan_1_5dte"]  # first pass: all due
    loop._last = {"monitor": 100.0, "scan_0dte": 100.0, "scan_1_5dte": 100.0}
    assert loop.due_jobs(120.0) == ["monitor"]  # only the 15s monitor elapsed
    assert "scan_0dte" in loop.due_jobs(500.0)  # 5-min scan now due too


async def test_loop_skips_closed_market() -> None:
    loop = ShortDurationLoop()
    ran = await loop.tick(now=_SAT, mono=10.0)
    assert ran == []  # RTH only


async def test_loop_runs_and_records_during_rth() -> None:
    loop = ShortDurationLoop()
    ran = await loop.tick(now=_FRI_RTH, mono=10.0)
    assert set(ran) == {"monitor", "scan_0dte", "scan_1_5dte"}


async def test_loop_isolates_a_failing_job() -> None:
    loop = ShortDurationLoop()

    async def boom(job, now):
        if job == "monitor":
            raise RuntimeError("monitor blew up")

    loop._run_job = boom  # type: ignore[assignment]
    # A failing job must not stop the others from running / being scheduled.
    ran = await loop.tick(now=_FRI_RTH, mono=10.0)
    assert set(ran) == {"monitor", "scan_0dte", "scan_1_5dte"}


# --- Backtest fidelity -------------------------------------------------------
def test_0dte_backtest_is_not_testable_without_option_history() -> None:
    c = classify_short_duration_backtest(DTECategory.ZERO_DTE)
    assert c.fidelity == BacktestFidelity.NOT_TESTABLE
    assert c.available_feeds["historical_option_quotes"] is False
    assert any("gamma" in cav or "illustrative" in cav for cav in c.caveats)


def test_1_5dte_backtest_is_proxy() -> None:
    c = classify_short_duration_backtest(DTECategory.SHORT_DTE)
    assert c.fidelity == BacktestFidelity.PROXY
    assert "Black-Scholes" in c.reason


def test_backtest_classification_endpoint() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/short-duration/backtest/classification")
    assert r.status_code == 200
    fids = {c["dte_category"]: c["fidelity"] for c in r.json()["classifications"]}
    assert fids["0dte"] == "not_testable" and fids["1-5dte"] == "proxy"


# --- Failure simulations -----------------------------------------------------
class _BrokenChain:
    def name(self):  # pragma: no cover
        return "broken"

    async def get_option_chain(self, *a, **k):
        raise RuntimeError("provider outage")

    async def get_option_chain_for_expirations(self, *a, **k):
        raise RuntimeError("provider outage")

    async def get_iv_context(self, *a, **k):
        raise RuntimeError("provider outage")


async def test_detection_survives_options_provider_outage(monkeypatch) -> None:
    from app.shortduration import detection

    monkeypatch.setattr(detection.registry, "options_chain_provider", lambda: _BrokenChain())
    # Regime build also uses the chain for IV — should degrade, not crash.
    cands = await detection.run_detection(DTECategory.SHORT_DTE, now=_FRI_RTH)
    # Candidates still produced; with no chain they are rejected (no contract),
    # never a crash and never a silently-forced trade.
    assert all(c.state.value in {"rejected", "evaluating", "watchlist", "armed"} for c in cands)
    assert all(c.max_risk_usd is None for c in cands)  # nothing sized without a chain


async def test_monitor_survives_chain_failure(monkeypatch) -> None:
    from app.db import repository
    from app.domain.enums import CandidateState
    from app.shortduration import paper
    from app.shortduration.detection import run_detection

    cands = await run_detection(DTECategory.SHORT_DTE, now=_FRI_RTH)
    tradeable = next((c for c in cands if c.trade_plan and c.state != CandidateState.REJECTED), None)
    if tradeable is None:
        return
    await paper.open_short_duration_paper(tradeable, now=_FRI_RTH)
    monkeypatch.setattr(paper.registry, "options_chain_provider", lambda: _BrokenChain())
    # A marking outage must leave the position OPEN, not crash or falsely close it.
    updated = await paper.monitor_short_duration_positions(now=_FRI_RTH)
    assert all(t.status == "open" for t in updated)
    assert repository.list_short_duration_trades(status="open")


# --- Scenario gates ----------------------------------------------------------
def _regime(regime=ShortDurationRegime.RANGE_BOUND, allow=True, reduce=False, mins=None, name=None):
    from app.domain.shortduration import ShortDurationRegimeState

    return ShortDurationRegimeState(
        regime=regime, confidence=0.6, allow_new_trades=allow, reduce_size=reduce,
        next_event_name=name, next_event_minutes=mins, as_of=_FRI_RTH,
    )


def test_scenario_macro_event_blackout_blocks_entry() -> None:
    from app.shortduration.risk import DailyRiskState, evaluate_entry_gates

    g = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BULLISH,
        regime=_regime(regime=ShortDurationRegime.MACRO_EVENT_DRIVEN, allow=False, name="CPI", mins=8),
        now=datetime(2026, 7, 17, 12, 0, tzinfo=_ET).astimezone(UTC),
        quote_stale=False, daily=DailyRiskState(), equity=2000,
    )
    assert not g.allowed


def test_scenario_power_hour_reduce_size() -> None:
    from app.shortduration.risk import DailyRiskState, evaluate_entry_gates

    g = evaluate_entry_gates(
        dte=DTECategory.SHORT_DTE, direction=Direction.BULLISH,
        regime=_regime(regime=ShortDurationRegime.HIGH_VOL_CHOP, reduce=True),
        now=datetime(2026, 7, 17, 15, 30, tzinfo=_ET).astimezone(UTC),  # power hour
        quote_stale=False, daily=DailyRiskState(), equity=2000,
    )
    assert g.allowed and g.size_modifier == 0.5


def test_scenario_regime_reduces_market_alignment_score() -> None:
    # A cautious/blocked regime must lower the market-alignment component, not
    # silently pass. (Exercises the news/macro-day scenario through scoring.)
    from app.shortduration.scoring.components import market_alignment
    from app.shortduration.strategies.base import SetupContext

    ctx = SetupContext(symbol="SPY", now=_FRI_RTH, regime=_regime(allow=False))
    comp = market_alignment(ctx, Direction.BULLISH)
    assert comp.value is not None and comp.value < 0.6
    assert "gated" in comp.explanation


# --- Load / concurrency ------------------------------------------------------
async def test_scan_stays_bounded_over_larger_universe() -> None:
    # A larger sweep must complete, respect bounded concurrency, and produce
    # bounded, well-formed candidates (no runaway fan-out, no partial crash).
    from app.shortduration.detection import run_detection

    universe = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMD", "META",
                "AMZN", "GOOGL", "TSLA", "NFLX", "AVGO", "CRM", "COST"]
    cands = await run_detection(DTECategory.SHORT_DTE, now=_FRI_RTH, universe=universe)
    assert len(cands) <= len(universe) * 2  # at most a couple strategies per symbol
    assert all(0.0 <= c.score <= 1.0 for c in cands)
    assert cands == sorted(cands, key=lambda c: c.score, reverse=True)
