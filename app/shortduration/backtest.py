"""Short-duration backtest FIDELITY classification.

We do not fabricate precise backtest results. Instead we state honestly what is
testable given the data actually available, per the module spec ("Clearly
classify results as fully reconstructed / approximate / proxy / not-testable;
never present approximate results as precise").

With no per-contract historical option-quote feed in the stack, short-duration
backtests are proxy-based at best — and 0DTE, whose P&L is dominated by intraday
gamma/theta, is effectively not faithfully testable without intraday option marks.
This module surfaces that classification so the UI and any caller can label
results correctly rather than implying precision that isn't there.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.config import settings
from app.domain.enums import DTECategory


class BacktestFidelity(str, Enum):
    RECONSTRUCTED = "reconstructed"  # real per-contract option marks replayed
    APPROXIMATE = "approximate"  # real underlying path + BS reprice (daily granularity)
    PROXY = "proxy"  # BS reprice at assumed/realized vol; intraday path unknown
    NOT_TESTABLE = "not_testable"  # the data needed for a faithful test is absent


class BacktestClassification(BaseModel):
    dte_category: str
    fidelity: BacktestFidelity
    reason: str
    caveats: list[str] = Field(default_factory=list)
    available_feeds: dict[str, bool] = Field(default_factory=dict)


def _has_historical_options_feed() -> bool:
    """True only if a real per-contract historical option-quote provider is
    configured. The interface exists (`HistoricalOptionsProvider`) but no live
    implementation is wired, so this is False today — stated, not assumed."""
    return getattr(settings, "provider_historical_options", None) not in (None, "", "none")


def _has_iv_history() -> bool:
    from app.providers import registry

    try:
        return registry.iv_history_provider() is not None
    except Exception:  # noqa: BLE001
        return False


_COMMON_CAVEATS = [
    "Simulated fills only — no real order book; entry/exit at mid ± modeled slippage.",
    "Bid/ask spread and liquidity at the historical instant are approximated.",
    "Corporate actions, halts, and early closes are not modeled unless noted.",
]


def classify_short_duration_backtest(dte: DTECategory) -> BacktestClassification:
    feeds = {
        "historical_option_quotes": _has_historical_options_feed(),
        "iv_history": _has_iv_history(),
        "intraday_bars": settings.provider_intraday is not None,
    }

    if feeds["historical_option_quotes"]:
        return BacktestClassification(
            dte_category=dte.value,
            fidelity=BacktestFidelity.RECONSTRUCTED,
            reason="A per-contract historical option-quote feed is configured; legs replay against recorded marks.",
            caveats=_COMMON_CAVEATS,
            available_feeds=feeds,
        )

    if dte == DTECategory.ZERO_DTE:
        return BacktestClassification(
            dte_category=dte.value,
            fidelity=BacktestFidelity.NOT_TESTABLE,
            reason=(
                "0DTE P&L is dominated by intraday gamma/theta and the exact intraday option "
                "path. Without a per-contract intraday option-quote feed, only a coarse "
                "Black-Scholes proxy is possible — not faithful enough to trust as a 0DTE backtest."
            ),
            caveats=[
                *_COMMON_CAVEATS,
                "Intraday option marks are unavailable — gamma/theta dynamics into the close are not reconstructed.",
                "Report any figures as illustrative only; do not size or select strategies from them.",
            ],
            available_feeds=feeds,
        )

    return BacktestClassification(
        dte_category=dte.value,
        fidelity=BacktestFidelity.PROXY,
        reason=(
            "1-5DTE legs are repriced with Black-Scholes on the real underlying path at "
            "trailing realized vol (no recorded option marks). Directional structure is "
            "captured; exact premium path and IV moves are approximated."
        ),
        caveats=[
            *_COMMON_CAVEATS,
            "IV path is a realized-vol proxy, not the traded implied surface.",
            "Overnight gaps are in the underlying path but option repricing is model-based.",
        ],
        available_feeds=feeds,
    )


def classify_all() -> list[BacktestClassification]:
    return [classify_short_duration_backtest(d) for d in DTECategory]
