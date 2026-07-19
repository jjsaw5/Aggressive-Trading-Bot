"""Short-duration Phase 5 — paper trading, monitoring, performance."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.domain.enums import Direction, DTECategory, ExitReason
from app.domain.shortduration import ShortDurationTrade
from app.shortduration.paper import (
    _intraday_exit,
    daily_risk_state,
    short_duration_performance,
    time_of_day_bucket,
)

_ET = ZoneInfo("America/New_York")


def _et(h, m=0):
    return datetime(2026, 7, 17, h, m, tzinfo=_ET).astimezone(UTC)


# --- Intraday time-stops -----------------------------------------------------
def test_intraday_exit_expiry_and_0dte_clock() -> None:
    assert _intraday_exit(-1, 0, _et(11)) == ExitReason.EXPIRY
    assert _intraday_exit(0, 0, _et(11)) is None  # 0DTE, still mid-session
    assert _intraday_exit(0, 0, _et(15, 50)) == ExitReason.TIME_STOP  # past 15:45
    assert _intraday_exit(1, 1, _et(11)) == ExitReason.TIME_STOP  # DTE hit time stop
    assert _intraday_exit(3, 1, _et(11)) is None  # room left


def test_time_of_day_buckets() -> None:
    assert time_of_day_bucket(_et(9, 45)) == "first_hour"
    assert time_of_day_bucket(_et(12, 0)) == "midday"
    assert time_of_day_bucket(_et(15, 30)) == "power_hour"


# --- Daily risk state (feeds Phase-4 gates) ---------------------------------
def _trade(pnl, closed, **kw):
    base = {
        "id": kw.get("id", "t" + str(abs(hash((pnl, str(closed)))) % 10000)),
        "candidate_id": "c", "paper_trade_id": "p", "symbol": "SPY",
        "dte_category": DTECategory.SHORT_DTE, "direction": Direction.BULLISH,
        "opened_at": _et(10), "status": "closed", "realized_pnl_usd": pnl, "closed_at": closed,
    }
    base.update(kw)
    return ShortDurationTrade(**base)


def test_daily_risk_state_counts_today_and_consecutive_losses() -> None:
    from app.db import repository

    now = _et(15, 55)
    # Two losses then... order by closed_at; trailing consecutive losses = 2.
    repository.save_short_duration_trade(_trade(50, _et(10, 0), id="w1"))
    repository.save_short_duration_trade(_trade(-30, _et(11, 0), id="l1"))
    repository.save_short_duration_trade(_trade(-40, _et(12, 0), id="l2"))
    ds = daily_risk_state(now)
    assert ds.realized_pnl_usd == -20  # 50 - 30 - 40
    assert ds.consecutive_losses == 2


def test_performance_report_groups() -> None:
    from app.db import repository

    repository.save_short_duration_trade(_trade(80, _et(11), id="pw", time_of_day="midday"))
    repository.save_short_duration_trade(_trade(-40, _et(12), id="pl", time_of_day="first_hour"))
    perf = short_duration_performance()
    assert perf["overall"]["trades"] >= 2
    assert "by_dte" in perf and "by_time_of_day" in perf and "by_score_band" in perf
    # Profit factor = gross win / gross loss.
    assert perf["overall"]["profit_factor"] is not None


# --- Full lifecycle via the pipeline ----------------------------------------
async def test_open_and_monitor_paper_position() -> None:
    from app.db import repository
    from app.domain.enums import CandidateState
    from app.shortduration.detection import run_detection
    from app.shortduration.paper import monitor_short_duration_positions, open_short_duration_paper

    now = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)  # Fri 11:30 ET
    cands = await run_detection(DTECategory.SHORT_DTE, now=now)
    tradeable = [c for c in cands if c.trade_plan is not None and c.state != CandidateState.REJECTED]
    assert tradeable, "mock universe should yield a tradeable candidate"
    cand = tradeable[0]

    sd = await open_short_duration_paper(cand, now=now)
    assert sd.status == "open" and sd.paper_trade_id
    assert sd.max_loss_usd is not None
    reopened = repository.get_short_duration_candidate(cand.id)
    assert reopened.state == CandidateState.OPEN  # advanced to OPEN

    marked = await monitor_short_duration_positions(now=now)
    assert marked and marked[0].current_net is not None  # marked from the chain


async def test_open_rejects_candidate_without_plan() -> None:
    import pytest

    from app.domain.enums import CandidateState
    from app.domain.shortduration import ShortDurationCandidate
    from app.shortduration.paper import open_short_duration_paper

    bare = ShortDurationCandidate(
        id="nope", symbol="SPY", dte_category=DTECategory.ZERO_DTE,
        detected_at=datetime.now(UTC), state=CandidateState.ARMED, trade_plan=None,
    )
    with pytest.raises(ValueError):
        await open_short_duration_paper(bare)


def test_paper_api_endpoints() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.post("/short-duration/scans/1-5dte")
    cands = c.get("/short-duration/1-5dte/candidates?limit=50").json()
    target = next((x for x in cands if x.get("trade_plan") and x["state"] != "rejected"), None)
    assert target is not None
    opened = c.post(f"/short-duration/candidates/{target['id']}/paper")
    assert opened.status_code == 200
    assert opened.json()["candidate"]["state"] == "open"

    pos = c.get("/short-duration/positions?status=open").json()
    assert any(p["candidate_id"] == target["id"] for p in pos)
    assert c.post("/short-duration/positions/monitor").status_code == 200
    perf = c.get("/short-duration/performance").json()
    assert "overall" in perf and "by_dte" in perf
