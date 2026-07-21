"""Broker-aligned (Robinhood) option-strategy naming — consistency guardrail.

Every user-facing rendering of an option structure must use the same names the
Robinhood order ticket shows, via `StrategyType.display_name`. This locks the
mapping and keeps the dashboard's JS label map from drifting out of sync.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.domain.enums import StrategyType

# The canonical Robinhood order-ticket names.
_ROBINHOOD = {
    StrategyType.LONG_CALL: "Long Call",
    StrategyType.LONG_PUT: "Long Put",
    StrategyType.BULL_CALL_SPREAD: "Call Debit Spread",
    StrategyType.BEAR_PUT_SPREAD: "Put Debit Spread",
    StrategyType.BULL_PUT_SPREAD: "Put Credit Spread",
    StrategyType.BEAR_CALL_SPREAD: "Call Credit Spread",
    StrategyType.LONG_STRADDLE: "Long Straddle",
    StrategyType.LONG_STRANGLE: "Long Strangle",
    StrategyType.IRON_CONDOR: "Iron Condor",
}


def test_display_name_matches_robinhood_for_every_member() -> None:
    # No member is left without a broker-aligned label.
    for st in StrategyType:
        assert st.display_name == _ROBINHOOD[st]
    assert set(_ROBINHOOD) == set(StrategyType)


def test_dashboard_label_map_matches_display_name() -> None:
    html = Path("app/web/dashboard.html").read_text()
    block = re.search(r"const STRAT_LABEL = \{(.*?)\};", html, re.S)
    assert block, "STRAT_LABEL map not found in dashboard"
    pairs = dict(re.findall(r"(\w+):\s*\"([^\"]+)\"", block.group(1)))
    # Every enum value maps to exactly its display_name in the UI.
    for st in StrategyType:
        assert pairs.get(st.value) == st.display_name, f"UI label drift for {st.value}"


def test_no_user_facing_snake_case_strategy_render_remains() -> None:
    # Guard against regressing to raw `strategy.value` in human-facing text. Storage
    # (repository) and API DTO fields keep the enum value on purpose — the UI maps
    # those via STRAT_LABEL — so only the prose sites are checked here.
    import pathlib

    prose_sites = [
        "app/cli.py", "app/alerts/service.py", "app/risk/trade_plan.py",
        "app/backtest/performance.py", "app/analytics/calibration.py",
    ]
    for path in prose_sites:
        src = pathlib.Path(path).read_text()
        assert "strategy.value.replace('_', ' ')" not in src, path
        assert ".trade_plan.strategy.value}" not in src, path
