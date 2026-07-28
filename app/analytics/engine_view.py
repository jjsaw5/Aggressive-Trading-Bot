"""The engine's on-the-record read of a symbol, frozen at entry.

Two jobs, both deliberately narrow:

1. **Suggest an invalidation level.** A structural price level computed from
   daily closes (SMA20, SMA50) — arithmetic on price history, not a forecast.
   It answers "what price action would prove this position's premise wrong?",
   which is a question the app CAN answer, unlike "will it go up?".

2. **Record what the engine thinks, and whether it agrees with you.** Frozen at
   entry so the app's own directional read becomes a scored prediction rather
   than a comment. This is the missing half of the calibration loop: the
   warehouse holds thousands of engine decisions but the app never went on
   record about the trades actually taken.

Nothing here claims edge. The engine's direction is the same hand-weighted
daily-trend read the scanner uses, and no feature has cleared the validation
gate (7 pre-registered/OOS tests, all null). It is recorded so it can be
GRADED — that is the only path by which conviction is ever earned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from app.domain.enums import Direction
from app.logging_config import get_logger

log = get_logger(__name__)

UNCALIBRATED_NOTE = (
    "The engine's directional read is a hand-weighted daily-trend signal, "
    "UNCALIBRATED — no feature has cleared out-of-sample validation. It is "
    "recorded here so it can be graded against what actually happened."
)


@dataclass(frozen=True)
class InvalidationSuggestion:
    price: float
    source: str  # sma20 | sma50
    note: str
    already_breached: bool
    alternate_price: float | None = None
    alternate_source: str = ""


def suggest_invalidation(
    direction: Direction, details: dict | None
) -> InvalidationSuggestion | None:
    """The structural level that would prove this position's premise wrong.

    A bearish position lives below its mean and dies on a close back above it;
    a bullish one is the mirror. SMA20 is the primary level. When price has
    ALREADY crossed it the suggestion is still returned — flagged as breached,
    with SMA50 offered as the next level out — because silently picking a
    further level would hide the fact that the position has no structural
    support left."""
    if not details or direction not in (Direction.BULLISH, Direction.BEARISH):
        return None
    price = details.get("price")
    sma20 = details.get("sma20")
    sma50 = details.get("sma50")
    if not price or not sma20:
        return None

    bearish = direction == Direction.BEARISH
    side = "above" if bearish else "below"
    breached = price >= sma20 if bearish else price <= sma20

    alt_price, alt_source = None, ""
    if sma50 and ((price < sma50) if bearish else (price > sma50)):
        alt_price, alt_source = float(sma50), "sma50"

    note = (
        f"Daily close back {side} SMA20 ({sma20:g}) — the 20-day mean this "
        f"position is trading {'below' if bearish else 'above'}."
    )
    if breached:
        note = (
            f"Price ({price:g}) is ALREADY {side} SMA20 ({sma20:g}) — this position "
            "has no structural support at the 20-day mean"
            + (f"; the next level out is SMA50 ({alt_price:g})." if alt_price else ".")
        )
    return InvalidationSuggestion(
        price=float(sma20), source="sma20", note=note, already_breached=breached,
        alternate_price=alt_price, alternate_source=alt_source,
    )


class EngineView(BaseModel):
    """The engine's read of a symbol, frozen at position-entry time."""

    as_of: datetime
    engine_direction: str = "neutral"  # bullish | bearish | neutral
    # Does the engine's own read match the direction you actually took?
    # None when the engine abstains (neutral) or has no data.
    agrees_with_position: bool | None = None
    price: float | None = None
    sma20: float | None = None
    sma50: float | None = None
    rsi: float | None = None
    rationale: str = ""
    invalidation_price: float | None = None
    invalidation_source: str = ""
    invalidation_note: str = ""
    invalidation_already_breached: bool = False
    uncalibrated_note: str = UNCALIBRATED_NOTE


async def build_engine_view(symbol: str, position_direction: Direction) -> EngineView | None:
    """Fetch daily closes, run the same price-action read the scanner uses, and
    freeze it. Returns None when there isn't enough history — abstain rather
    than stamp a guess."""
    from app.engine.price_action import analyze_price_action
    from app.providers import registry

    try:
        history = await registry.market_data_provider().get_price_history(
            symbol, lookback_days=120
        )
    except Exception as exc:  # noqa: BLE001 — a stamp must never block an entry
        log.warning("engine_view_history_failed", symbol=symbol, error=str(exc))
        return None

    sig = analyze_price_action(history)
    details = sig.details or {}
    if not details.get("sma20"):
        return None

    sugg = suggest_invalidation(position_direction, details)
    agrees: bool | None = None
    if sig.direction in (Direction.BULLISH, Direction.BEARISH) and position_direction in (
        Direction.BULLISH, Direction.BEARISH
    ):
        agrees = sig.direction == position_direction

    return EngineView(
        as_of=datetime.now(UTC),
        engine_direction=sig.direction.value,
        agrees_with_position=agrees,
        price=details.get("price"),
        sma20=details.get("sma20"),
        sma50=details.get("sma50"),
        rsi=details.get("rsi"),
        rationale=sig.rationale,
        invalidation_price=sugg.price if sugg else None,
        invalidation_source=sugg.source if sugg else "",
        invalidation_note=sugg.note if sugg else "",
        invalidation_already_breached=bool(sugg and sugg.already_breached),
    )
