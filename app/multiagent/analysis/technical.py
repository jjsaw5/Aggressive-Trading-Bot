"""Technical measurement — a registry, so indicators are added not edited.

The spec asks that the technical framework let indicators be *added or modified
easily*. So an indicator is a registered function from a context to
measurements, and `build_technical_snapshot` simply runs every registered one:

    @register("my_indicator", requires_bars=30)
    def _my_indicator(ctx: IndicatorContext) -> list[Measurement]:
        ...

Adding one is a decorator. No model changes, no scorer changes, no branching in
the caller. What an indicator may *not* do is award points — scoring reads the
resulting measurements by name, and the mapping lives in
`config/methodology.yaml`.

Every indicator returns `Measurement`s, so an indicator without enough bars
returns an absent measurement with a reason rather than a zero. `requires_bars`
is enforced by the runner, so an indicator never has to defend itself against a
short history: a 14-period ATR computed from 9 bars is not an ATR, and reporting
one would be a fabricated number wearing a real name.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from app.domain.market import Candle, PriceHistory, Quote
from app.multiagent.models.enums import BiasDirection, Direction
from app.multiagent.models.measurements import (
    AbsenceReason,
    Measurement,
    MeasurementSet,
    Provenance,
)


@dataclass
class IndicatorContext:
    """Everything an indicator may look at."""

    symbol: str
    now: datetime
    direction: Direction
    quote: Quote | None
    history: PriceHistory | None
    candles: list[Candle] = field(default_factory=list)
    # Config knobs an indicator needs (atr_period, momentum_lookback_days, ...).
    params: dict[str, float] = field(default_factory=dict)

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    @property
    def highs(self) -> list[float]:
        return [c.high for c in self.candles]

    @property
    def lows(self) -> list[float]:
        return [c.low for c in self.candles]

    @property
    def volumes(self) -> list[int]:
        return [c.volume for c in self.candles]

    @property
    def last_price(self) -> float | None:
        if self.quote is not None and self.quote.price is not None:
            return self.quote.price
        return self.candles[-1].close if self.candles else None

    def param(self, name: str, default: float) -> float:
        return float(self.params.get(name, default))


Indicator = Callable[[IndicatorContext], Iterable[Measurement]]


@dataclass(frozen=True)
class _Registered:
    name: str
    fn: Indicator
    requires_bars: int
    description: str


_REGISTRY: dict[str, _Registered] = {}


def register(name: str, *, requires_bars: int = 0, description: str = "") -> Callable[[Indicator], Indicator]:
    def deco(fn: Indicator) -> Indicator:
        if name in _REGISTRY:
            raise ValueError(f"indicator {name!r} is already registered")
        _REGISTRY[name] = _Registered(name, fn, requires_bars, description or (fn.__doc__ or "").strip())
        return fn

    return deco


def registered_indicators() -> dict[str, _Registered]:
    return dict(_REGISTRY)


# --- helpers ----------------------------------------------------------------


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    return sum(values[-window:]) / window


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100.0


def true_range(prev_close: float, high: float, low: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def average_true_range(candles: list[Candle], period: int) -> float | None:
    """Wilder-style simple mean of true ranges. None if the history is short."""
    if len(candles) < period + 1:
        return None
    trs = [
        true_range(candles[i - 1].close, candles[i].high, candles[i].low)
        for i in range(len(candles) - period, len(candles))
    ]
    return sum(trs) / len(trs)


def swing_levels(candles: list[Candle], *, window: int = 3, lookback: int = 60) -> tuple[list[float], list[float]]:
    """Local extrema as support/resistance candidates.

    A pivot is a bar whose high (low) exceeds every bar within `window` on both
    sides. Simple and transparent by choice: an opaque level-detection routine
    would put unauditable numbers into a score that must be auditable.
    """
    recent = candles[-lookback:] if len(candles) > lookback else candles
    if len(recent) < 2 * window + 1:
        return [], []
    highs: list[float] = []
    lows: list[float] = []
    for i in range(window, len(recent) - window):
        seg = recent[i - window : i + window + 1]
        if recent[i].high >= max(c.high for c in seg):
            highs.append(recent[i].high)
        if recent[i].low <= min(c.low for c in seg):
            lows.append(recent[i].low)
    return sorted(set(lows)), sorted(set(highs))


# --- indicators -------------------------------------------------------------


@register("trend", requires_bars=21, description="20-bar percentage return")
def _trend(ctx: IndicatorContext) -> list[Measurement]:
    lookback = int(ctx.param("trend_lookback", 20))
    closes = ctx.closes
    if len(closes) <= lookback:
        return [Measurement.absent("trend_return_pct", AbsenceReason.NO_DATA, unit="%")]
    return [
        Measurement.of(
            "trend_return_pct",
            _pct(closes[-1], closes[-1 - lookback]),
            unit="%",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
        )
    ]


@register("moving_averages", requires_bars=20, description="Price versus 20/50-bar SMA")
def _moving_averages(ctx: IndicatorContext) -> list[Measurement]:
    closes = ctx.closes
    price = ctx.last_price
    out: list[Measurement] = []
    for window in (20, 50):
        sma = _sma(closes, window)
        name = f"pct_above_sma{window}"
        out.append(
            Measurement.of(
                name,
                _pct(price, sma),
                unit="%",
                provenance=Provenance.DERIVED,
                as_of=ctx.now,
                note=f"None when fewer than {window} bars are available",
            )
        )
    return out


@register("relative_volume", requires_bars=21, description="Latest volume over 20-bar average")
def _relative_volume(ctx: IndicatorContext) -> list[Measurement]:
    vols = [float(v) for v in ctx.volumes]
    if len(vols) < 21:
        return [Measurement.absent("relative_volume", AbsenceReason.NO_DATA, unit="x")]
    baseline = sum(vols[-21:-1]) / 20.0
    if baseline <= 0:
        return [
            Measurement.absent(
                "relative_volume", AbsenceReason.UNRESOLVED, unit="x", note="zero baseline volume"
            )
        ]
    return [
        Measurement.of(
            "relative_volume",
            vols[-1] / baseline,
            unit="x",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
        )
    ]


@register("momentum", requires_bars=6, description="Short-lookback return and consecutive closes")
def _momentum(ctx: IndicatorContext) -> list[Measurement]:
    lookback = int(ctx.param("momentum_lookback_days", 5))
    closes = ctx.closes
    out: list[Measurement] = []
    if len(closes) > lookback:
        out.append(
            Measurement.of(
                "momentum_return_pct",
                _pct(closes[-1], closes[-1 - lookback]),
                unit="%",
                provenance=Provenance.DERIVED,
                as_of=ctx.now,
            )
        )
    else:
        out.append(Measurement.absent("momentum_return_pct", AbsenceReason.NO_DATA, unit="%"))

    # Directional agreement: how many of the last `lookback` closes moved the
    # candidate's way. A count, not a verdict.
    if len(closes) > lookback:
        deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - lookback, len(closes))]
        wanted_up = ctx.direction == Direction.BULLISH
        agree = sum(1 for d in deltas if (d > 0) == wanted_up)
        out.append(
            Measurement.of(
                "momentum_agreement_ratio",
                agree / len(deltas),
                provenance=Provenance.DERIVED,
                as_of=ctx.now,
                note=f"{agree}/{len(deltas)} recent closes moved with the thesis",
            )
        )
    else:
        out.append(Measurement.absent("momentum_agreement_ratio", AbsenceReason.NO_DATA))
    return out


@register("atr", requires_bars=15, description="Average true range, absolute and as % of price")
def _atr(ctx: IndicatorContext) -> list[Measurement]:
    period = int(ctx.param("atr_period", 14))
    atr = average_true_range(ctx.candles, period)
    price = ctx.last_price
    return [
        Measurement.of(
            f"atr{period}",
            atr,
            unit="$",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
            note=f"None when fewer than {period + 1} bars are available",
        ),
        Measurement.of(
            "atr_pct",
            (atr / price * 100.0) if (atr is not None and price) else None,
            unit="%",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
        ),
    ]


@register("structure", requires_bars=10, description="Higher highs / lower lows over the lookback")
def _structure(ctx: IndicatorContext) -> list[Measurement]:
    candles = ctx.candles[-40:]
    if len(candles) < 10:
        return [Measurement.absent("structure_score", AbsenceReason.NO_DATA)]
    half = len(candles) // 2
    first_high = max(c.high for c in candles[:half])
    second_high = max(c.high for c in candles[half:])
    first_low = min(c.low for c in candles[:half])
    second_low = min(c.low for c in candles[half:])
    # +1 higher highs AND higher lows, -1 the reverse, 0 mixed.
    if second_high > first_high and second_low > first_low:
        score = 1.0
    elif second_high < first_high and second_low < first_low:
        score = -1.0
    else:
        score = 0.0
    return [
        Measurement.of(
            "structure_score",
            score,
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
            note="+1 higher highs and higher lows, -1 lower highs and lower lows, 0 mixed",
        )
    ]


@register("levels", requires_bars=15, description="Distance to nearest support and resistance, in ATR")
def _levels(ctx: IndicatorContext) -> list[Measurement]:
    price = ctx.last_price
    period = int(ctx.param("atr_period", 14))
    atr = average_true_range(ctx.candles, period)
    supports, resistances = swing_levels(ctx.candles)
    out: list[Measurement] = []

    if price is None or atr is None or atr <= 0:
        return [
            Measurement.absent(
                "distance_to_resistance_atr", AbsenceReason.NO_DATA, note="price or ATR unavailable"
            ),
            Measurement.absent("distance_to_support_atr", AbsenceReason.NO_DATA),
        ]

    above = [r for r in resistances if r > price]
    below = [s for s in supports if s < price]
    out.append(
        Measurement.of(
            "distance_to_resistance_atr",
            ((min(above) - price) / atr) if above else None,
            unit="ATR",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
            reason=AbsenceReason.NO_DATA,
            note="no swing high above price" if not above else "",
        )
    )
    out.append(
        Measurement.of(
            "distance_to_support_atr",
            ((price - max(below)) / atr) if below else None,
            unit="ATR",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
            reason=AbsenceReason.NO_DATA,
            note="no swing low below price" if not below else "",
        )
    )
    return out


@register("extension", requires_bars=21, description="Distance from the 20-bar SMA in ATR")
def _extension(ctx: IndicatorContext) -> list[Measurement]:
    price = ctx.last_price
    sma20 = _sma(ctx.closes, 20)
    atr = average_true_range(ctx.candles, int(ctx.param("atr_period", 14)))
    if price is None or sma20 is None or not atr:
        return [Measurement.absent("extension_atr", AbsenceReason.NO_DATA, unit="ATR")]
    return [
        Measurement.of(
            "extension_atr",
            abs(price - sma20) / atr,
            unit="ATR",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
            note="how far price has travelled from its mean, in units of daily range",
        )
    ]


@register("gap", requires_bars=2, description="Opening gap versus the prior close")
def _gap(ctx: IndicatorContext) -> list[Measurement]:
    if len(ctx.candles) < 2:
        return [Measurement.absent("gap_pct", AbsenceReason.NO_DATA, unit="%")]
    prev_close = ctx.candles[-2].close
    today_open = ctx.candles[-1].open
    return [
        Measurement.of(
            "gap_pct",
            _pct(today_open, prev_close),
            unit="%",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
        )
    ]


@register("rolling_vwap", requires_bars=20, description="20-bar volume-weighted average price")
def _rolling_vwap(ctx: IndicatorContext) -> list[Measurement]:
    """Named `rolling_vwap`, not `vwap`.

    True VWAP is an intraday, session-anchored measure. This is a 20-bar
    volume-weighted mean of typical prices from daily bars, which is a different
    statistic. Calling it VWAP would be a quietly wrong label on a number a human
    will act on.
    """
    bars = ctx.candles[-20:]
    if len(bars) < 20:
        return [Measurement.absent("pct_above_rolling_vwap", AbsenceReason.NO_DATA, unit="%")]
    num = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in bars)
    den = sum(c.volume for c in bars)
    if den <= 0:
        return [
            Measurement.absent(
                "pct_above_rolling_vwap", AbsenceReason.UNRESOLVED, unit="%", note="zero total volume"
            )
        ]
    return [
        Measurement.of(
            "pct_above_rolling_vwap",
            _pct(ctx.last_price, num / den),
            unit="%",
            provenance=Provenance.DERIVED,
            as_of=ctx.now,
        )
    ]


# --- runner -----------------------------------------------------------------


def run_indicators(ctx: IndicatorContext) -> tuple[MeasurementSet, list[str]]:
    """Run every registered indicator. Returns measurements and notes."""
    ms = MeasurementSet()
    notes: list[str] = []
    bars = len(ctx.candles)
    for reg in _REGISTRY.values():
        if bars < reg.requires_bars:
            notes.append(
                f"{reg.name}: skipped, needs {reg.requires_bars} bars, have {bars}"
            )
            continue
        try:
            for m in reg.fn(ctx):
                ms.add(m)
        except Exception as exc:  # noqa: BLE001 - one indicator must not kill the snapshot
            notes.append(f"{reg.name}: failed ({str(exc)[:120]})")
    return ms, notes


def trend_bias(ms: MeasurementSet, flat_threshold_pct: float) -> BiasDirection:
    """Directional read of the trend measurement. UNKNOWN when unmeasured."""
    trend = ms.get("trend_return_pct")
    if not trend.present:
        return BiasDirection.UNKNOWN
    value = trend.require()
    if value > flat_threshold_pct:
        return BiasDirection.BULLISH
    if value < -flat_threshold_pct:
        return BiasDirection.BEARISH
    return BiasDirection.NEUTRAL
