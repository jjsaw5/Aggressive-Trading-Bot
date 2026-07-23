"""Input-coverage monitor — per-feed checks, abstention, feed-outage alerting."""

from __future__ import annotations

from app.config import get_settings
from app.domain.enums import CandidateState, DTECategory
from app.domain.shortduration import ShortDurationCandidate
from app.shortduration.input_coverage import (
    aggregate_scan,
    assess_symbol_coverage,
    last_scan_coverage,
)
from app.shortduration.scoring.engine import score_candidate
from app.shortduration.strategies.base import SetupContext
from tests.test_sd_scoring import _NOW, _chain, _detection, _iv, _levels, _regime


def _ctx(**kw) -> SetupContext:
    base = {"symbol": "SPY", "now": _NOW, "regime": _regime(), "levels": _levels(),
            "change_pct": 0.8}
    base.update(kw)
    return SetupContext(**base)


def _full_ctx(**kw) -> SetupContext:
    """A context with every required 1-5DTE input present."""
    from datetime import timedelta

    from app.domain.market import Candle, PriceHistory, Quote

    closes = [100 + i * 0.5 for i in range(60)]
    daily = PriceHistory(
        symbol="SPY",
        candles=[Candle(ts=_NOW - timedelta(days=60 - i), open=c, high=c + 1, low=c - 1,
                        close=c, volume=1_000_000) for i, c in enumerate(closes)],
        source="test",
    )
    base = {
        "quote": Quote(symbol="SPY", price=101.0, as_of=_NOW, delayed_minutes=0, source="t"),
        "bars_1m": [object()], "daily": daily,
    }
    base.update(kw)
    return _ctx(**base)


def test_full_inputs_yield_full_coverage() -> None:
    from app.domain.market import Quote

    ctx = _ctx(quote=Quote(symbol="SPY", price=101.0, as_of=_NOW, delayed_minutes=0, source="t"),
               bars_1m=[object()])
    cov = assess_symbol_coverage(ctx, chain=_chain(), iv=_iv(), dte=DTECategory.ZERO_DTE, now=_NOW)
    # flow is empty -> the one required 0DTE miss; everything else present.
    assert cov.coverage >= 0.8
    assert "iv.iv_rank" not in cov.missing


def test_missing_iv_rank_is_a_named_gap() -> None:
    # The field whose silent death started all of this: an IV context with iv30 but
    # no rank must show iv.iv_rank as missing while iv.iv30 stays present.
    from app.domain.options import IVContext

    iv_no_rank = IVContext(symbol="SPY", iv30=0.3, as_of=_NOW, source="t")
    cov = assess_symbol_coverage(_ctx(), chain=_chain(), iv=iv_no_rank,
                                 dte=DTECategory.SHORT_DTE, now=_NOW)
    assert "iv.iv_rank" in cov.missing
    assert "iv.iv30" not in cov.missing


def test_low_coverage_abstains_and_holds_at_evaluating() -> None:
    # Symbol with nearly nothing: no quote, no chain, no iv, no daily -> coverage
    # far below threshold -> the card ABSTAINS and the state machine holds the
    # candidate at EVALUATING (never watchlist/arm), reason recorded.
    from app.shortduration.detection import _classify_transitions

    ctx = _ctx(levels=None)
    cov = assess_symbol_coverage(ctx, chain=None, iv=None, dte=DTECategory.SHORT_DTE, now=_NOW)
    assert cov.coverage < get_settings().input_coverage_abstain_threshold
    card = score_candidate(ctx, _detection(dte=DTECategory.SHORT_DTE), chain=None, iv=None,
                           coverage=cov)
    assert card.abstained is True
    assert "[ABSTAINED]" in card.summary
    assert "iv.iv_rank" in card.abstain_reason

    cand = ShortDurationCandidate(
        id="a1", symbol="SPY", dte_category=DTECategory.SHORT_DTE, detected_at=_NOW,
        state=CandidateState.DETECTED, score=0.95, scorecard=card,  # arm-worthy score!
    )
    trail = _classify_transitions(cand, _detection(dte=DTECategory.SHORT_DTE), _NOW, tradeable=True)
    assert cand.state == CandidateState.EVALUATING  # held: no watchlist, no arm
    assert trail[-1].to_state == CandidateState.EVALUATING
    assert any("Abstaining" in r for r in cand.reasons)


def test_good_coverage_does_not_abstain() -> None:
    ctx = _full_ctx()
    cov = assess_symbol_coverage(ctx, chain=_chain(), iv=_iv(),
                                 dte=DTECategory.SHORT_DTE, now=_NOW)
    assert cov.coverage >= get_settings().input_coverage_abstain_threshold
    card = score_candidate(ctx, _detection(dte=DTECategory.SHORT_DTE),
                           chain=_chain(), iv=_iv(), coverage=cov)
    assert card.abstained is False
    assert card.input_coverage == cov.coverage


def test_scanwide_dead_field_raises_feed_alert() -> None:
    # THE regression case: iv_rank missing for EVERY symbol in the scan (the wiring
    # bug that went unnoticed) must surface as a degraded field on the first scan.
    from app.domain.options import IVContext

    reports = []
    for sym in ("SPY", "QQQ", "AAPL"):
        iv_no_rank = IVContext(symbol=sym, iv30=0.3, as_of=_NOW, source="t")
        reports.append(assess_symbol_coverage(_ctx(symbol=sym), chain=_chain(), iv=iv_no_rank,
                                              dte=DTECategory.SHORT_DTE, now=_NOW))
    scan = aggregate_scan(reports, dte=DTECategory.SHORT_DTE, now=_NOW, alert_threshold=0.5)
    assert "iv.iv_rank" in scan.degraded
    assert "iv.iv30" not in scan.degraded
    # Latest-scan store serves the API.
    assert "1-5dte" in last_scan_coverage()
    assert last_scan_coverage("1-5dte")["1-5dte"].degraded == scan.degraded


def test_observational_fields_do_not_drive_abstention() -> None:
    # No news / catalysts / flow(1-5DTE) must not push a symbol toward abstention.
    cov = assess_symbol_coverage(_ctx(news=[], catalysts=[], flow=[]), chain=_chain(),
                                 iv=_iv(), dte=DTECategory.SHORT_DTE, now=_NOW)
    obs = [c for c in cov.checks if not c.required]
    assert {c.key for c in obs} >= {"news.items", "calendar.catalysts", "flow.alerts"}
    assert all(k not in cov.missing for k in ("news.items", "calendar.catalysts"))
