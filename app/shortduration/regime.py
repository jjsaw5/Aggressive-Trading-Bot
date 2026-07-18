"""Short-duration intraday market-regime engine.

A transparent rules engine (no ML) that fuses index trend (SPY/QQQ/IWM vs VWAP
and intraday change), the breadth proxy, a volatility reading, and the next macro
event into a `ShortDurationRegimeState`. Its operative outputs are the gates:
`allow_new_trades` and `reduce_size` — the regime can veto or throttle entries
independent of any single candidate. Every input is surfaced as a supporting or
contradicting factor so the banner can explain itself.

Missing inputs are handled explicitly: an index with no reading simply doesn't
vote; it is never counted as neutral-bullish.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.enums import ShortDurationRegime
from app.domain.shortduration import (
    EconomicEvent,
    IntradayLevels,
    RegimeFactor,
    ShortDurationRegimeState,
)
from app.shortduration.breadth import BreadthProxy

_MAJORS = ("SPY", "QQQ", "IWM")


@dataclass(frozen=True)
class RegimeConfig:
    high_vol_threshold: float = 0.60  # vol_reading (e.g. IV rank) at/above = high
    low_vol_threshold: float = 0.35
    strong_breadth: float = 0.60  # >= is a strong directional breadth
    weak_breadth: float = 0.40  # <= confirms the downside
    trend_min_pct: float = 0.10  # min |intraday %| for an index to vote
    event_blackout_minutes: float = 15.0  # within this -> block new trades
    event_caution_minutes: float = 60.0  # within this -> reduce size


def _index_vote(change_pct: float | None, levels: IntradayLevels | None, cfg: RegimeConfig) -> int:
    """+1 bullish, -1 bearish, 0 no decisive read for one index."""
    if change_pct is None:
        return 0
    above = levels.above_vwap if levels else None
    if change_pct >= cfg.trend_min_pct and above is not False:
        return 1
    if change_pct <= -cfg.trend_min_pct and above is not True:
        return -1
    return 0


def compute_regime(
    *,
    index_change_pct: dict[str, float | None],
    index_levels: dict[str, IntradayLevels],
    breadth: BreadthProxy | None,
    vol_reading: float | None,
    next_event: EconomicEvent | None,
    now: datetime,
    restriction_active: bool = False,
    config: RegimeConfig | None = None,
) -> ShortDurationRegimeState:
    cfg = config or RegimeConfig()
    factors: list[RegimeFactor] = []

    votes = {
        s: _index_vote(index_change_pct.get(s), index_levels.get(s), cfg) for s in _MAJORS
    }
    bulls = sum(1 for v in votes.values() if v > 0)
    bears = sum(1 for v in votes.values() if v < 0)
    decisive = bulls + bears
    for s in _MAJORS:
        v = votes[s]
        chg = index_change_pct.get(s)
        if chg is not None:
            factors.append(
                RegimeFactor(
                    name=f"{s} trend",
                    value=f"{chg:+.2f}% {'▲' if v > 0 else '▼' if v < 0 else '·'}",
                    supports=v != 0,
                )
            )

    breadth_pct = breadth.above_vwap_pct if breadth else None
    if breadth_pct is not None:
        factors.append(
            RegimeFactor(
                name="Breadth (proxy)",
                value=f"{breadth_pct * 100:.0f}% above VWAP",
                supports=breadth_pct >= cfg.strong_breadth or breadth_pct <= cfg.weak_breadth,
            )
        )

    is_high_vol = vol_reading is not None and vol_reading >= cfg.high_vol_threshold
    is_low_vol = vol_reading is not None and vol_reading <= cfg.low_vol_threshold
    if vol_reading is not None:
        factors.append(
            RegimeFactor(
                name="Volatility",
                value=("high" if is_high_vol else "low" if is_low_vol else "mid"),
                supports=True,
            )
        )

    # Event proximity.
    mins = next_event.minutes_until(now) if next_event else None
    high_impact_soon = (
        next_event is not None
        and (next_event.impact or "").lower() == "high"
        and mins is not None
        and 0 <= mins <= cfg.event_caution_minutes
    )
    in_blackout = restriction_active or (
        high_impact_soon and mins is not None and mins <= cfg.event_blackout_minutes
    )
    if next_event is not None and mins is not None and mins >= 0:
        factors.append(
            RegimeFactor(
                name="Next macro event",
                value=f"{next_event.name} in {mins:.0f}m ({next_event.impact or 'n/a'})",
                supports=not high_impact_soon,
            )
        )

    # --- Classify ---
    net = bulls - bears
    bull_breadth = breadth_pct is None or breadth_pct >= cfg.strong_breadth
    bear_breadth = breadth_pct is None or breadth_pct <= cfg.weak_breadth

    if in_blackout:
        regime = ShortDurationRegime.MACRO_EVENT_DRIVEN
    elif decisive == 0:
        regime = (
            ShortDurationRegime.HIGH_VOL_CHOP if is_high_vol
            else ShortDurationRegime.LOW_VOL_COMPRESSION if is_low_vol
            else ShortDurationRegime.RANGE_BOUND
        )
    elif net >= 2 and bull_breadth:
        regime = ShortDurationRegime.HIGH_VOL_TREND if is_high_vol else ShortDurationRegime.BULL_TREND
    elif net <= -2 and bear_breadth:
        regime = ShortDurationRegime.HIGH_VOL_TREND if is_high_vol else ShortDurationRegime.BEAR_TREND
    elif bulls > 0 and bears > 0:
        regime = ShortDurationRegime.HIGH_VOL_CHOP if is_high_vol else ShortDurationRegime.RANGE_BOUND
    else:
        regime = ShortDurationRegime.RANGE_BOUND

    # Confidence: index agreement strength, tempered by breadth alignment.
    agreement = (abs(net) / len(_MAJORS)) if decisive else 0.0
    confidence = round(min(1.0, 0.35 + 0.5 * agreement + (0.15 if breadth_pct is not None else 0.0)), 3)
    if regime in (ShortDurationRegime.RANGE_BOUND, ShortDurationRegime.HIGH_VOL_CHOP):
        confidence = round(min(confidence, 0.5), 3)

    allow_new = not in_blackout
    reduce_size = (
        is_high_vol
        or regime in (ShortDurationRegime.HIGH_VOL_CHOP, ShortDurationRegime.MACRO_EVENT_DRIVEN)
        or (high_impact_soon or False)
    )

    return ShortDurationRegimeState(
        regime=regime,
        confidence=confidence,
        factors=factors,
        allow_new_trades=allow_new,
        reduce_size=bool(reduce_size),
        next_event_name=next_event.name if next_event else None,
        next_event_minutes=mins if (next_event and mins is not None and mins >= 0) else None,
        spy_trend_pct=index_change_pct.get("SPY"),
        qqq_trend_pct=index_change_pct.get("QQQ"),
        iwm_trend_pct=index_change_pct.get("IWM"),
        breadth_above_vwap_pct=breadth_pct,
        vol_reading=vol_reading,
        as_of=now,
        notes="Breadth is a universe proxy, not exchange internals." if breadth else "",
    )
