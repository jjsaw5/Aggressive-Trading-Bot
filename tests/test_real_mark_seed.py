"""Population builders for the scaled real-mark backtest (pure, no API).

Pins the calendar math, put-call-parity spot reconstruction, vol-regime tagging,
and that the engine-mode builder actually applies the scanner's debit-vs-credit
rule (IV rank >= IV_HIGH on a directional trend -> credit vertical).
"""

from __future__ import annotations

from datetime import date, timedelta

from app.backtest.real_mark_seed import (
    build_engine_verticals,
    monthly_expiries,
    near_atm,
    occ,
    parity_spot,
    reconstruct_spot_path,
    third_friday,
    vol_regime,
)
from app.domain.historic import HistoricOptionBar
from app.engine.strategy_selector import IV_HIGH


def test_third_friday_and_expiries() -> None:
    assert third_friday(2022, 1) == date(2022, 1, 21)
    assert third_friday(2021, 12) == date(2021, 12, 17)
    exps = monthly_expiries(date(2021, 11, 1), date(2022, 2, 28))
    assert exps == [date(2021, 11, 19), date(2021, 12, 17), date(2022, 1, 21), date(2022, 2, 18)]


def test_occ_format() -> None:
    assert occ("AAPL", date(2022, 6, 17), "C", 150) == "AAPL220617C00150000"


def test_parity_spot_and_near_atm() -> None:
    call = HistoricOptionBar("c", date(2022, 1, 3), 12.0, 13.0)  # mid 12.5
    put = HistoricOptionBar("p", date(2022, 1, 3), 2.0, 3.0)  # mid 2.5
    # spot ~= 12.5 - 2.5 + 100 = 110
    assert parity_spot(call, put, 100) == 110.0
    assert near_atm([90, 100, 110, 120], 108) == 110


def test_vol_regime_thresholds() -> None:
    assert vol_regime(None) == "unknown"
    assert vol_regime(0.20) == "low"
    assert vol_regime(0.40) == "mid"
    assert vol_regime(IV_HIGH + 0.1) == "high"


def _series(strike_mid: dict[int, float], *, is_call: bool, days: int = 60):
    """Build a flat daily history per strike with a given mid (bid/ask = mid±0.1)."""
    out = {}
    for k, mid in strike_mid.items():
        bars = [
            HistoricOptionBar(
                occ("SPY", date(2022, 6, 17), "C" if is_call else "P", k),
                date(2022, 3, 1) + timedelta(days=d), mid - 0.1, mid + 0.1, iv=0.6,
                open_interest=5000, volume=500,
            )
            for d in range(days)
        ]
        out[k] = bars
    return out


def test_reconstruct_spot_uses_atm_strike() -> None:
    # Calls rise, puts fall with strike; the ATM strike is where mids are closest.
    calls = _series({400: 15.0, 420: 5.0}, is_call=True)
    puts = _series({400: 5.0, 420: 15.0}, is_call=False)
    dates = sorted({b.date for v in calls.values() for b in v})
    path = reconstruct_spot_path(calls, puts, dates)
    # At K=400: 15-5+400=410; at K=420: 5-15+420=410. Both agree -> ~410.
    assert all(abs(s - 410) < 1e-6 for s in path.values())


def test_engine_mode_sells_credit_in_high_iv_uptrend() -> None:
    # A rising ATM spot (uptrend -> bullish) with IV rank proxy = 1.0 (>= IV_HIGH)
    # must make the engine builder SELL a bull put credit spread, not buy a debit.
    strikes = list(range(380, 441, 20))
    # Rising underlying: encode via strike where call/put mids cross climbing over time.
    calls, puts = {}, {}
    for k in strikes:
        cbars, pbars = [], []
        for i, d in enumerate(date(2022, 3, 1) + timedelta(days=n) for n in range(80)):
            spot = 400 + i * 0.5  # steady uptrend
            cm = max(0.5, spot - k) / 1 + 5
            pm = max(0.5, k - spot) / 1 + 5
            cbars.append(HistoricOptionBar(occ("SPY", date(2022, 6, 17), "C", k), d, cm - 0.1, cm + 0.1, iv=0.6, open_interest=5000, volume=500))
            pbars.append(HistoricOptionBar(occ("SPY", date(2022, 6, 17), "P", k), d, pm - 0.1, pm + 0.1, iv=0.6, open_interest=5000, volume=500))
        calls[k], puts[k] = cbars, pbars
    dates = sorted({b.date for v in calls.values() for b in v})
    spot = reconstruct_spot_path(calls, puts, dates)
    trades = build_engine_verticals("SPY", date(2022, 6, 17), 20, calls, puts, spot, entry_offsets=(20,))
    assert trades, "expected at least one engine-selected trade"
    # High IV + bullish -> credit structure.
    assert all(t.strategy == "put_credit_spread" for t, _ in trades)
    assert all(t.vol_regime == "high" for t, _ in trades)
