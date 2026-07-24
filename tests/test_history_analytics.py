"""Trading-history analytics: source filters, calendar day grouping, stats math."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import (
    Direction,
    ExitReason,
    OptionAction,
    OptionType,
    PaperTradeStatus,
    StrategyType,
)
from app.domain.trades import ContractLeg, PaperTrade, RiskPlan, TradePlan


def _plan(sym: str, contracts: int) -> TradePlan:
    leg = ContractLeg(symbol=sym, action=OptionAction.BUY_TO_OPEN,
                      option_type=OptionType.CALL, strike=100.0,
                      expiration=date(2026, 12, 18), quantity=contracts, entry_price=1.0)
    return TradePlan(symbol=sym, direction=Direction.BULLISH,
                     strategy=StrategyType.LONG_CALL, legs=[leg], net_debit=1.0,
                     contracts=contracts,
                     risk=RiskPlan(max_loss_usd=100.0, account_risk_pct=0.05,
                                   profit_target_pct=0.5, stop_loss_pct=0.5))


def _closed(sym: str, source: str, opened: str, closed: str, entry: float,
            exit_: float, contracts: int = 1,
            reason: ExitReason = ExitReason.MANUAL) -> PaperTrade:
    pnl = round((exit_ - entry) * 100 * contracts, 2)
    return PaperTrade(
        id=uuid.uuid4().hex[:12], scan_id=source, symbol=sym,
        trade_plan=_plan(sym, contracts), status=PaperTradeStatus.CLOSED,
        opened_at=datetime.fromisoformat(opened).replace(tzinfo=UTC),
        entry_fill=entry, closed_at=datetime.fromisoformat(closed).replace(tzinfo=UTC),
        exit_fill=exit_, exit_reason=reason, realized_pnl_usd=pnl,
    )


_FIXTURE = [
    # live (manual + rh_sync): TSLA +245, JPM -50, NVDA +100
    _closed("TSLA", "manual", "2026-07-18T14:30", "2026-07-21T15:00", 2.45, 4.90),
    _closed("JPM", "rh_sync", "2026-07-20T14:30", "2026-07-21T18:00", 0.25, 0.00,
            contracts=2, reason=ExitReason.EXPIRY),
    _closed("NVDA", "manual", "2026-06-25T14:30", "2026-06-30T15:00", 2.00, 3.00),
    # paper (engine-originated scan id)
    _closed("SPY", "scan_abc123", "2026-07-19T14:30", "2026-07-21T15:00", 1.00, 2.00),
    # still open — must never appear in history
    PaperTrade(id=uuid.uuid4().hex[:12], scan_id="manual", symbol="MSFT",
               trade_plan=_plan("MSFT", 1), status=PaperTradeStatus.OPEN,
               opened_at=datetime(2026, 7, 16, 14, 30, tzinfo=UTC), entry_fill=4.68),
]


@pytest.fixture
def client(monkeypatch) -> TestClient:
    import app.api.routes.positions as pos
    import app.main as m

    monkeypatch.setattr(pos.repository, "list_paper_trades",
                        lambda limit=2000: list(_FIXTURE))
    return TestClient(m.app)


def test_live_filter_stats_and_calendar_grouping(client: TestClient) -> None:
    d = client.get("/positions/history/analytics?source=live").json()
    s = d["stats"]
    assert {t["symbol"] for t in d["trades"]} == {"TSLA", "JPM", "NVDA"}
    assert s["n"] == 3 and s["wins"] == 2 and s["losses"] == 1
    assert s["net_pnl_usd"] == pytest.approx(295.0)
    assert s["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["expectancy_usd"] == pytest.approx(98.33, abs=0.01)
    assert s["profit_factor"] == pytest.approx(345 / 50, abs=1e-3)
    assert s["avg_win_usd"] == pytest.approx(172.5)
    assert s["avg_loss_usd"] == pytest.approx(-50.0)

    # Calendar day grouping: two live trades closed 7/21 sum to +195.
    assert s["best_day"] == ["2026-07-21", 195.0]
    assert s["worst_day"] == ["2026-06-30", 100.0]
    assert s["equity_curve"] == [["2026-06-30", 100.0], ["2026-07-21", 295.0]]
    assert s["by_month"] == [["2026-06", 100.0, 1], ["2026-07", 195.0, 2]]

    tsla = next(t for t in d["trades"] if t["symbol"] == "TSLA")
    assert tsla["closed_date"] == "2026-07-21" and tsla["days_held"] == 3
    assert tsla["pnl_pct"] == pytest.approx(1.0)  # +245 on a $245 basis


def test_paper_and_all_filters(client: TestClient) -> None:
    paper = client.get("/positions/history/analytics?source=paper").json()
    assert [t["symbol"] for t in paper["trades"]] == ["SPY"]
    both = client.get("/positions/history/analytics?source=all").json()
    assert both["stats"]["n"] == 4  # open MSFT excluded everywhere
    assert both["stats"]["net_pnl_usd"] == pytest.approx(395.0)


def test_breakdowns_carry_per_key_win_rates(client: TestClient) -> None:
    s = client.get("/positions/history/analytics?source=live").json()["stats"]
    by_sym = {row[0]: row for row in s["by_symbol"]}
    assert by_sym["JPM"][1:] == [1, -50.0, 0.0]
    assert by_sym["TSLA"][1:] == [1, 245.0, 1.0]
    by_reason = {row[0]: row for row in s["by_exit_reason"]}
    assert by_reason["expiry"][2] == pytest.approx(-50.0)
    assert by_reason["manual"][1] == 2
