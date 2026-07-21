"""Structure-aware exit plans for short-duration trades.

The core exit engine (`app.risk.exit_plan`) prices premium-based take-profits and
stops off the option itself — necessary, but not sufficient for an intraday trade
that lives and dies on PRICE STRUCTURE (VWAP, the opening range, swing levels) and
the CLOCK. This builder layers those in:

- **Primary / secondary invalidation** — the underlying levels that kill the thesis
  (a decisive close back through VWAP, back inside the opening range), not a premium %.
- **Premium backstop** — the defined-risk hard floor, read from the sized contract's
  core exit plan, so a slow bleed still has a mechanical cut.
- **PT1 / PT2** — staged profit taking, premium marks from the core plan.
- **Time stop** — an intraday clock for 0DTE (flatten well before the close), DTE for
  multi-day.
- **Momentum stop** — N consecutive 1-min closes against the structural level.
- **EOD / expiration actions** — explicit, so a 0DTE is never accidentally carried
  into settlement/pin risk.

The structural plan stands on its own even before a contract is sized; premium-priced
fields fill in once a `TradePlan` exists.
"""

from __future__ import annotations

from app.config import get_settings
from app.domain.enums import Direction, DTECategory, ShortDurationStrategy
from app.domain.shortduration import (
    IntradayLevels,
    ShortDurationExitPlan,
    ShortDurationExitTarget,
)
from app.domain.trades import ExitPlan as CoreExitPlan
from app.domain.trades import TradePlan
from app.shortduration.strategies.base import StrategyDetection

_ORB = ShortDurationStrategy.OPENING_RANGE_BREAKOUT
_VWAP = ShortDurationStrategy.VWAP_TREND_CONTINUATION


def _structural_levels(
    det: StrategyDetection, levels: IntradayLevels | None
) -> tuple[tuple[str, float | None], tuple[str, float | None]]:
    """(primary, secondary) invalidation as (rule, price), from the setup's structure.

    ORB is invalidated by re-entering the range; VWAP-continuation by losing VWAP.
    Each names the other level as its softer, secondary warning. Multi-day setups
    fall back to VWAP as the intraday guardrail. Missing levels leave the price None
    (the rule text still stands)."""
    d = det.direction
    bull = d == Direction.BULLISH
    vwap = levels.vwap if levels else None
    orh = levels.opening_range_high if levels else None
    orl = levels.opening_range_low if levels else None
    or_level = orh if bull else orl
    meta_level = det.metadata.get("level") if det.metadata else None
    range_level = meta_level if isinstance(meta_level, (int, float)) else or_level

    range_side = "below" if bull else "above"
    vwap_side = "below" if bull else "above"
    range_rule = (
        f"Close back {range_side} the opening-range {'high' if bull else 'low'}"
        + (f" ({range_level:g})" if range_level is not None else "")
    )
    vwap_rule = (
        f"Decisive close {vwap_side} VWAP" + (f" ({vwap:g})" if vwap is not None else "")
        + " against the trade"
    )

    if det.strategy == _ORB:
        return (range_rule, range_level), (vwap_rule, vwap)
    if det.strategy == _VWAP:
        return (vwap_rule, vwap), (range_rule, range_level)
    # Multi-day / other 0DTE setups: prefer the detection's own invalidation text,
    # guarded intraday by VWAP.
    primary = (det.invalidation or vwap_rule, range_level if range_level is not None else vwap)
    return primary, (vwap_rule, vwap)


def _premium_legs(core: CoreExitPlan | None) -> tuple[float | None, str, list[ShortDurationExitTarget]]:
    """Lift the premium stop + take-profit marks off the core (premium-priced) plan."""
    if core is None:
        return None, "", []
    stop_net = stop_note = None
    targets: list[ShortDurationExitTarget] = []
    tp_i = 0
    for lvl in core.levels:
        if lvl.kind == "stop":
            stop_net = lvl.net_price
            stop_note = lvl.note
        elif lvl.kind == "take_profit":
            tp_i += 1
            targets.append(
                ShortDurationExitTarget(
                    label=f"PT{tp_i}", trigger=lvl.label, premium_net=lvl.net_price,
                    pnl_usd=lvl.pnl_usd, note=lvl.note,
                )
            )
    return stop_net, stop_note or "", targets


def build_short_duration_exit_plan(
    det: StrategyDetection,
    *,
    levels: IntradayLevels | None = None,
    plan: TradePlan | None = None,
) -> ShortDurationExitPlan:
    """Assemble a structure-aware exit plan for a detected setup. Uses the sized
    contract's premium marks when available, and always names the structural
    invalidations, time/momentum stops, and end-of-life actions."""
    s = get_settings()
    dte = det.dte_category
    is_0dte = dte == DTECategory.ZERO_DTE
    (prim_rule, prim_px), (sec_rule, sec_px) = _structural_levels(det, levels)
    stop_net, stop_note, targets = _premium_legs(plan.exit_plan if plan else None)

    if is_0dte:
        time_stop = (
            f"Flatten by {s.short_duration_0dte_flatten_et} ET; no new/added risk after "
            f"{s.short_duration_0dte_cutoff_et} ET. 0DTE — do not carry into the close."
        )
        eod_action = (
            "close_all — 0DTE expires today; flatten the position by the flatten time "
            "even at a loss. Never hold for a settlement print."
        )
        expiration_action = (
            "Expires today. An ITM long risks assignment/pin; a spread near max value "
            "should be closed, not held to settlement."
        )
    else:
        tstop_dte = (plan.risk.time_stop_dte if plan and plan.risk else None) or \
            s.short_duration_1_5dte_time_stop_dte
        time_stop = f"Close or roll by {tstop_dte} DTE — theta + gamma accelerate into expiry."
        eod_action = "reassess into the close; hold overnight only if the thesis is intact and defined-risk."
        expiration_action = f"Do not carry through expiration; act by the {tstop_dte}-DTE time stop."

    momentum_stop = (
        f"{s.short_duration_momentum_stop_bars} consecutive 1-min closes against the structure "
        f"({prim_rule.split('(')[0].strip().lower()}) = momentum has failed; exit."
    )
    # Flag PT1 as a scale-out to match the staged-exit policy.
    if targets:
        targets[0].note = (
            f"Scale out ~{int(s.short_duration_pt1_scale_pct * 100)}% here; "
            f"trail the runner to PT2/structure. {targets[0].note}".strip()
        )

    max_loss = plan.risk.max_loss_usd if plan and plan.risk else None
    rationale = (
        f"{'0DTE' if is_0dte else dte.value} {det.direction.value} trade managed off "
        f"structure and the clock: primary invalidation is a {prim_rule.lower()}, with a "
        f"premium backstop{'' if stop_net is None else f' at {stop_net:g}'} as the hard floor. "
        f"{'Flatten before the close — no settlement risk.' if is_0dte else 'Time-stopped before expiry.'}"
    )

    return ShortDurationExitPlan(
        dte_category=dte,
        direction=det.direction,
        primary_invalidation=prim_rule,
        primary_invalidation_price=prim_px,
        secondary_invalidation=sec_rule,
        secondary_invalidation_price=sec_px,
        premium_stop_net=stop_net,
        premium_stop_note=stop_note,
        profit_targets=targets,
        time_stop=time_stop,
        momentum_stop=momentum_stop,
        eod_action=eod_action,
        expiration_action=expiration_action,
        max_loss_usd=max_loss,
        rationale=rationale,
    )
