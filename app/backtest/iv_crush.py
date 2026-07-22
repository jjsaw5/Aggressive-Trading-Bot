"""IV-crush validation harness.

Measures what a Black-Scholes-at-realized-vol backtest structurally cannot: the
volatility collapse around a catalyst, and whether the short-premium book's tail
is real. For an earnings-tagged trade it reads each leg's recorded implied
volatility on the day *before* and *after* the event, reports the crush and the
mark move it caused, and — for the put-credit-spread population that went 10–0 in
the live ledger — buckets realized P&L by the post-event underlying move so the
left tail is visible on history instead of awaited live.

Pure over already-fetched bars. The option IV/mark comes from UW history; the
post-event underlying move is supplied by the caller (from the market-data
provider), since a per-contract option bar does not carry the underlying price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.domain.historic import HistoricOptionBar


@dataclass(frozen=True)
class IVCrushObservation:
    contract_id: str
    event_date: date
    pre_date: date | None
    post_date: date | None
    iv_pre: float | None
    iv_post: float | None
    mark_pre: float | None
    mark_post: float | None

    @property
    def crush(self) -> float | None:
        """IV drop across the event (positive = vol collapsed, the expected sign
        for a long-premium holder getting hurt / short-premium holder helped)."""
        if self.iv_pre is None or self.iv_post is None:
            return None
        return self.iv_pre - self.iv_post

    @property
    def mark_change(self) -> float | None:
        if self.mark_pre is None or self.mark_post is None:
            return None
        return self.mark_post - self.mark_pre


def measure_iv_crush(
    bars: list[HistoricOptionBar], event_date: date
) -> IVCrushObservation:
    """Pre = the last bar strictly before the event; post = the first strictly
    after. Uses only bars carrying a usable IV / mid so a quote gap can't fake a
    crush."""
    contract_id = bars[0].contract_id if bars else ""
    before = [b for b in bars if b.date < event_date and b.iv is not None]
    after = [b for b in bars if b.date > event_date and b.iv is not None]
    pre = max(before, key=lambda b: b.date) if before else None
    post = min(after, key=lambda b: b.date) if after else None
    return IVCrushObservation(
        contract_id=contract_id,
        event_date=event_date,
        pre_date=pre.date if pre else None,
        post_date=post.date if post else None,
        iv_pre=pre.iv if pre else None,
        iv_post=post.iv if post else None,
        mark_pre=pre.mid if pre else None,
        mark_post=post.mid if post else None,
    )


# --- P&L conditioned on the post-event underlying move -----------------------
_MOVE_EDGES = [
    ("down_big", -1e9, -0.05),
    ("down", -0.05, -0.02),
    ("flat", -0.02, 0.02),
    ("up", 0.02, 0.05),
    ("up_big", 0.05, 1e9),
]


@dataclass(frozen=True)
class MoveBucket:
    label: str
    n: int
    avg_pnl_usd: float | None
    worst_pnl_usd: float | None  # the left tail — the number that pays for the streak
    win_rate: float | None


def bucket_pnl_by_move(pairs: list[tuple[float, float]]) -> list[MoveBucket]:
    """`pairs` = (net_pnl_usd, post_event_underlying_move_pct). Buckets by the move
    so the short-premium book's payoff on a large adverse move is explicit."""
    buckets: list[MoveBucket] = []
    for label, lo, hi in _MOVE_EDGES:
        members = [pnl for pnl, mv in pairs if lo <= mv < hi]
        if not members:
            continue
        wins = sum(1 for p in members if p > 0)
        buckets.append(
            MoveBucket(
                label=label,
                n=len(members),
                avg_pnl_usd=round(sum(members) / len(members), 2),
                worst_pnl_usd=round(min(members), 2),
                win_rate=round(wins / len(members), 4),
            )
        )
    return buckets


@dataclass(frozen=True)
class IVCrushReport:
    n: int
    avg_crush: float | None
    pct_crushed: float | None  # share with iv_post < iv_pre
    move_buckets: list[MoveBucket]
    iv_data: str = "real"  # feeds the scorecard: this rests on recorded IV


def build_iv_crush_report(
    observations: list[IVCrushObservation],
    pnl_by_move: list[tuple[float, float]] | None = None,
) -> IVCrushReport:
    crushes = [o.crush for o in observations if o.crush is not None]
    crushed = sum(1 for c in crushes if c > 0)
    return IVCrushReport(
        n=len(observations),
        avg_crush=round(sum(crushes) / len(crushes), 4) if crushes else None,
        pct_crushed=round(crushed / len(crushes), 4) if crushes else None,
        move_buckets=bucket_pnl_by_move(pnl_by_move or []),
    )
