"""Market and sector alignment — is the trade with the tape or against it?

Alignment is measured, not asserted: index and sector biases come from trailing
returns on real price history, and relative strength is the candidate's return
minus the benchmark's over the same window.

"Aligned" is a three-state question, not a boolean. A bullish candidate in a
*neutral* tape is neither aligned nor fighting it, and collapsing that to False
would let a flat market spend the fighting-the-tape penalty. Every `aligned_*`
field is `bool | None`, where None means the benchmark had no measurable bias.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.market import PriceHistory
from app.multiagent.models.brief import IndexContext
from app.multiagent.models.enums import BiasDirection, Direction
from app.multiagent.models.measurements import (
    AbsenceReason,
    Measurement,
    Provenance,
)
from app.multiagent.models.validation import MarketAlignmentSnapshot


def _trailing_return_pct(history: PriceHistory | None, lookback: int) -> float | None:
    if history is None:
        return None
    closes = [c.close for c in history.candles]
    if len(closes) <= lookback or closes[-1 - lookback] == 0:
        return None
    return (closes[-1] - closes[-1 - lookback]) / closes[-1 - lookback] * 100.0


def bias_from_return(pct: float | None, flat_threshold_pct: float) -> BiasDirection:
    if pct is None:
        return BiasDirection.UNKNOWN
    if pct > flat_threshold_pct:
        return BiasDirection.BULLISH
    if pct < -flat_threshold_pct:
        return BiasDirection.BEARISH
    return BiasDirection.NEUTRAL


def _agrees(direction: Direction, bias: BiasDirection) -> bool | None:
    """Tri-state agreement. None when the benchmark has no measurable bias."""
    if bias in (BiasDirection.UNKNOWN, BiasDirection.NEUTRAL):
        return None
    if direction == Direction.BULLISH:
        return bias is BiasDirection.BULLISH
    if direction == Direction.BEARISH:
        return bias is BiasDirection.BEARISH
    return None


def build_alignment_snapshot(
    symbol: str,
    direction: Direction,
    *,
    now: datetime,
    symbol_history: PriceHistory | None,
    indices: dict[str, IndexContext],
    sector: str | None,
    sector_proxy: str | None,
    sector_history: PriceHistory | None,
    lookback_days: int,
    flat_threshold_pct: float,
) -> MarketAlignmentSnapshot:
    snap = MarketAlignmentSnapshot(symbol=symbol, as_of=now, sector=sector, sector_proxy=sector_proxy)
    ms = snap.measurements

    spy = indices.get("SPY")
    qqq = indices.get("QQQ")
    snap.spy_bias = spy.bias if spy else BiasDirection.UNKNOWN
    snap.qqq_bias = qqq.bias if qqq else BiasDirection.UNKNOWN

    symbol_ret = _trailing_return_pct(symbol_history, lookback_days)
    sector_ret = _trailing_return_pct(sector_history, lookback_days)
    snap.sector_bias = bias_from_return(sector_ret, flat_threshold_pct)

    ms.add(
        Measurement.of(
            "symbol_trailing_return_pct",
            symbol_ret,
            unit="%",
            provenance=Provenance.DERIVED,
            as_of=now,
            note=f"{lookback_days}-bar return",
        )
    )
    ms.add(
        Measurement.of(
            "spy_trailing_return_pct",
            spy.trailing_20d_return_pct if spy else None,
            unit="%",
            provenance=Provenance.DERIVED,
            as_of=now,
        )
    )
    ms.add(
        Measurement.of(
            "sector_trailing_return_pct",
            sector_ret,
            unit="%",
            provenance=Provenance.DERIVED,
            as_of=now,
            reason=AbsenceReason.NO_DATA,
            note=(
                f"proxy {sector_proxy}" if sector_proxy else "no sector proxy resolved for this symbol"
            ),
        )
    )

    # Relative strength against SPY, oriented to the candidate's direction so a
    # bearish name underperforming a falling tape reads as strength for the
    # thesis rather than weakness.
    spy_ret = spy.trailing_20d_return_pct if spy else None
    if symbol_ret is not None and spy_ret is not None:
        raw = symbol_ret - spy_ret
        oriented = raw if direction == Direction.BULLISH else -raw
        ms.add(
            Measurement.of(
                "relative_strength_vs_spy",
                round(oriented, 4),
                unit="%",
                provenance=Provenance.DERIVED,
                as_of=now,
                note="symbol minus SPY over the window, oriented to the thesis direction",
            )
        )
    else:
        ms.add(
            Measurement.absent(
                "relative_strength_vs_spy",
                AbsenceReason.NO_DATA,
                unit="%",
                note="symbol or SPY return unavailable",
            )
        )

    snap.aligned_with_spy = _agrees(direction, snap.spy_bias)
    snap.aligned_with_qqq = _agrees(direction, snap.qqq_bias)
    snap.aligned_with_sector = _agrees(direction, snap.sector_bias)

    # Fighting the tape means both major benchmarks have a measurable bias and
    # the trade opposes both. One benchmark disagreeing is not fighting the tape.
    both_measured = snap.aligned_with_spy is not None and snap.aligned_with_qqq is not None
    snap.fighting_the_tape = (
        (not snap.aligned_with_spy and not snap.aligned_with_qqq) if both_measured else None
    )

    if spy is None:
        snap.notes.append("SPY context unavailable — market alignment partially unmeasured")
    if sector_proxy is None:
        snap.notes.append(
            f"no sector proxy for sector {sector or 'unknown'} — sector alignment abstains"
        )
    if snap.fighting_the_tape:
        snap.notes.append(
            f"{symbol} {direction.value} opposes both SPY ({snap.spy_bias.value}) and "
            f"QQQ ({snap.qqq_bias.value})"
        )
    return snap
