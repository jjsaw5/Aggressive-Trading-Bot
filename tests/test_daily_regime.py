"""The daily market-regime table, and the lookahead rule that governs its join.

Pre-flight P6 (reviewer Ruling 2). The per-signal vol×tape tag was rejected as a
substitute for this because it conflates symbol-level with market-level
conditions — two signals fired in the same minute could carry different
"regimes" while experiencing the same market.

The single most important test in this file is the lookahead one. A signal fired
intraday cannot know its own session's close; joining it to that session's regime
would condition the pre-registration's per-regime cuts on the future, which is a
subtler and more damaging error than a missing column.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.analytics.daily_regime import (
    build_regime_series,
    classify,
    percentile_of_last,
    realized_vol,
    regime_as_of,
    vs_sma,
)


def _series(n: int, start: date = date(2026, 1, 5)):
    """n consecutive weekday-ish sessions; exact calendar is irrelevant here."""
    from datetime import timedelta

    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# --- Percentile ---------------------------------------------------------------
def test_percentile_needs_a_full_window_rather_than_estimating() -> None:
    # A percentile over 6 observations is not the same measurement as one over
    # 20; pooling them would make the threshold mean different things by day.
    assert percentile_of_last([1.0] * 19, 20) is None
    assert percentile_of_last([1.0] * 20, 20) is not None


def test_the_highest_value_in_the_window_is_the_top_percentile() -> None:
    assert percentile_of_last([*range(1, 20), 99.0], 20) == pytest.approx(1.0)


def test_the_lowest_value_in_the_window_is_the_bottom() -> None:
    assert percentile_of_last([*range(2, 21), 1.0], 20) == pytest.approx(0.05)


# --- Realized vol -------------------------------------------------------------
def test_realized_vol_needs_window_plus_one_closes() -> None:
    # 20 returns require 21 closes.
    assert realized_vol([100.0] * 20, 20) is None
    assert realized_vol([100.0 + i for i in range(21)], 20) is not None


def test_a_flat_series_has_zero_realized_vol() -> None:
    assert realized_vol([100.0] * 21, 20) == pytest.approx(0.0)


def test_realized_vol_is_annualised() -> None:
    # Alternating +/-1% daily: annualised vol should land well above the daily
    # move, confirming the sqrt(252) scaling is applied.
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    rv = realized_vol(closes, 20)
    assert rv is not None and 0.10 < rv < 0.30


def test_a_non_positive_close_is_bad_data_not_a_big_move() -> None:
    closes = [100.0] * 20 + [0.0]
    assert realized_vol(closes, 20) is None


# --- SMA gap ------------------------------------------------------------------
def test_price_above_its_average_is_positive() -> None:
    closes = [100.0] * 49 + [110.0]
    v = vs_sma(closes, 50)
    assert v is not None and v > 0


def test_price_below_its_average_is_negative() -> None:
    closes = [100.0] * 49 + [90.0]
    assert vs_sma(closes, 50) < 0


def test_sma_needs_a_full_window() -> None:
    assert vs_sma([100.0] * 49, 50) is None


# --- Classification -----------------------------------------------------------
def test_low_vol_above_trend() -> None:
    assert classify(0.10, 0.02) == ("lowvol_above", "lowvol", "above")


def test_high_vol_below_trend() -> None:
    assert classify(0.90, -0.03) == ("highvol_below", "highvol", "below")


def test_mid_vol_sits_between_the_thresholds() -> None:
    assert classify(0.50, 0.01)[1] == "midvol"


def test_a_missing_axis_makes_the_whole_class_unknown() -> None:
    # NOT a default middle bucket: a default would swell one class with rows
    # that were never measured, and the per-regime gate counts those classes.
    assert classify(None, 0.02)[0] == "unknown"
    assert classify(0.5, None)[0] == "unknown"


# --- Series construction ------------------------------------------------------
def _built(n: int = 60):
    sessions = _series(n)
    vix = {d: 15.0 + (i % 10) for i, d in enumerate(sessions)}
    spx = {d: 4000.0 + i * 5 for i, d in enumerate(sessions)}
    return sessions, build_regime_series(vix, spx)


def test_one_row_per_overlapping_session() -> None:
    sessions, rows = _built(60)
    assert len(rows) == len(sessions)
    assert [r.session for r in rows] == sessions


def test_a_session_missing_from_either_series_is_skipped_not_interpolated() -> None:
    sessions = _series(30)
    vix = dict.fromkeys(sessions, 15.0)
    spx = dict.fromkeys(sessions[:-1], 4000.0)  # last session absent
    rows = build_regime_series(vix, spx)
    assert sessions[-1] not in {r.session for r in rows}


def test_early_rows_are_unclassified_rather_than_guessed() -> None:
    # Before 50 sessions there is no SMA, so the class cannot be formed.
    _sessions, rows = _built(60)
    assert rows[0].regime_class == "unknown"
    assert rows[0].is_complete is False


def test_later_rows_become_fully_classified() -> None:
    _sessions, rows = _built(60)
    assert rows[-1].is_complete is True
    assert rows[-1].regime_class != "unknown"


def test_each_row_sees_only_its_own_history() -> None:
    """Row i must not be affected by data after session i.

    Truncating the inputs must leave the surviving rows byte-identical — if a
    later session could influence an earlier row, the whole table would be
    contaminated by hindsight.
    """
    sessions = _series(60)
    vix = {d: 15.0 + (i % 10) for i, d in enumerate(sessions)}
    spx = {d: 4000.0 + i * 5 for i, d in enumerate(sessions)}
    full = build_regime_series(vix, spx)
    short = build_regime_series(
        {d: vix[d] for d in sessions[:55]}, {d: spx[d] for d in sessions[:55]}
    )
    assert [r.model_dump() for r in full[:55]] == [r.model_dump() for r in short]


# --- THE lookahead rule -------------------------------------------------------
def test_the_join_never_uses_the_signals_own_session() -> None:
    """THE rule. A signal fired at 10:15 cannot know that day's close."""
    _sessions, rows = _built(60)
    target = rows[-1].session
    got = regime_as_of(rows, target)
    assert got is not None
    assert got.session < target, (
        "regime_as_of returned the signal's own session — that is lookahead: the "
        "per-regime cuts would be conditioned on information the decision did "
        "not have."
    )
    assert got.session == rows[-2].session


def test_a_weekend_signal_picks_up_the_last_trading_session() -> None:
    _sessions, rows = _built(60)
    from datetime import timedelta

    got = regime_as_of(rows, rows[-1].session + timedelta(days=3))
    assert got is not None and got.session == rows[-1].session


def test_nothing_before_the_first_session_yields_none_not_the_first_row() -> None:
    # Reaching forward to the nearest row would be lookahead by another name.
    _sessions, rows = _built(60)
    assert regime_as_of(rows, rows[0].session) is None


def test_an_empty_table_joins_to_nothing() -> None:
    assert regime_as_of([], date(2026, 6, 1)) is None
