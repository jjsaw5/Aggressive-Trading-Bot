"""Replay a decision's exit policy against MINUTE bars, not daily marks.

Phase 2 of the remediation directive. The daily policy replay
(`policy_settlement.py`) can only see one price per day, so an intraday
target-or-stop is invisible to it. That is not a rounding error: all 38 audited
0DTE signals resolved `expiry`, because a same-day exit cannot exist in a series
with one point per day. The managed policy those rows claimed to run had never
been measured at all.

WHAT THIS BUYS, AND WHAT IT COSTS

The gain is that the exit becomes observable — reason, price and timestamp to
the minute — and that excursion (MFE/MAE) becomes computable at all.

The cost is honesty about the bar. A minute bar has a high and a low but no
ordering between them; we cannot know which came first. Two rules follow, and
both are deliberately biased AGAINST the strategy:

  * The stop is evaluated against the structure's WORST value in the bar, and
    checked BEFORE the target. A minute that traded through both is a loss.
  * The target is evaluated against the structure's BEST value, but only after
    the stop has already declined to fire.

For a multi-leg structure the worst case pairs each long leg's low with each
short leg's high, and vice versa for the best. Those extremes need not have
co-occurred within the minute, so the range is a true bound rather than a
realisable price — MFE/MAE are therefore reported as bounds, and named as such.

SOURCE DISCIPLINE: these bars are TRADES, not quotes (see the provider module).
An outcome graded here is stamped `managed_policy_intraday` and never pooled
with a quote-based grade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.analytics.policy_settlement import occ_symbol
from app.domain.enums import OptionAction
from app.domain.options import OptionMinuteBar
from app.domain.outcomes import DecisionOutcome, DecisionSnapshot, OutcomeResult
from app.domain.trades import TradePlan
from app.logging_config import get_logger

log = get_logger(__name__)

SCRATCH_BAND_FRAC = 0.05
OUTCOME_SOURCE = "managed_policy_intraday"


@dataclass(frozen=True)
class IntradayExit:
    exit_ts: datetime
    exit_net: float
    reason: str  # profit_target | stop_loss | time_stop | session_close | expiry


@dataclass(frozen=True)
class Excursion:
    """Bounds on how far the position ever ran, in per-share structure terms."""

    mfe_per_share: float  # most favourable (upper bound)
    mae_per_share: float  # most adverse (lower bound, <= 0 normally)
    mfe_ts: datetime | None
    mae_ts: datetime | None
    bars_seen: int


def _is_long(leg) -> bool:
    return leg.action in (OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE)


def structure_range(
    plan: TradePlan, bars: dict[str, OptionMinuteBar]
) -> tuple[float, float] | None:
    """(worst, best) signed net per share this minute, or None if any leg is unpriced.

    A long leg is worth its low in the worst case and its high in the best; a
    short leg is the reverse. Refusing to price a partially-quoted structure is
    the same rule the daily replay uses — half a spread is a different
    instrument, not an approximation of this one.
    """
    worst = best = 0.0
    for leg in plan.legs:
        bar = bars.get(occ_symbol(leg.symbol, leg.expiration, leg.option_type, leg.strike))
        if bar is None:
            return None
        if _is_long(leg):
            worst += bar.low
            best += bar.high
        else:
            worst -= bar.high
            best -= bar.low
    return round(worst, 4), round(best, 4)


def walk_intraday(
    plan: TradePlan,
    entry_net: float,
    bars_by_minute: dict[datetime, dict[str, OptionMinuteBar]],
    *,
    entry_ts: datetime | None = None,
    session_close_exit: bool = False,
) -> tuple[IntradayExit | None, Excursion | None]:
    """Replay the plan's own exit rules minute by minute.

    `session_close_exit` books a forced close on the last observed bar, which is
    what the 0DTE policy actually does (`close_all`, never held to expiry). It is
    off by default so a multi-day structure is not closed at the end of day one.
    """
    risk = plan.risk
    target, stop = risk.profit_target_pct, risk.stop_loss_pct
    denom = abs(entry_net)
    if denom < 1e-6:
        return None, None

    minutes = sorted(m for m in bars_by_minute if entry_ts is None or m >= entry_ts)
    mfe = mae = 0.0
    mfe_ts = mae_ts = None
    seen = 0
    last_priced: tuple[datetime, float] | None = None

    for m in minutes:
        rng = structure_range(plan, bars_by_minute[m])
        if rng is None:
            continue  # nothing traded on some leg this minute — hold, never invent
        worst, best = rng
        seen += 1
        last_priced = (m, best)

        # Excursion bounds, tracked on every priced bar regardless of exit.
        up, down = (best - entry_net) / denom, (worst - entry_net) / denom
        if up > mfe:
            mfe, mfe_ts = up, m
        if down < mae:
            mae, mae_ts = down, m

        # Stop first, on the WORST price in the bar. A minute that touched both
        # levels is booked as the loss.
        if stop is not None and down <= -stop:
            return (
                IntradayExit(m, worst, "stop_loss"),
                Excursion(round(mfe, 4), round(mae, 4), mfe_ts, mae_ts, seen),
            )
        if target is not None and up >= target:
            return (
                IntradayExit(m, best, "profit_target"),
                Excursion(round(mfe, 4), round(mae, 4), mfe_ts, mae_ts, seen),
            )

    exc = Excursion(round(mfe, 4), round(mae, 4), mfe_ts, mae_ts, seen) if seen else None
    if session_close_exit and last_priced is not None:
        ts, net = last_priced
        return IntradayExit(ts, net, "session_close"), exc
    return None, exc


def settle_intraday(
    snapshot: DecisionSnapshot,
    bars_by_minute: dict[datetime, dict[str, OptionMinuteBar]],
    *,
    resolved_at: datetime | None = None,
    session_close_exit: bool = False,
    include_costs: bool = True,
) -> DecisionOutcome | None:
    """Grade one decision against its own rules, at minute resolution.

    Returns None when no exit fired and no forced close applies — the decision is
    still open on this data, which is a different statement from "it expired".
    """
    plan = snapshot.trade_plan
    if plan is None or not plan.legs:
        return None
    resolved_at = resolved_at or datetime.now(UTC)
    entry_net = snapshot.entry_net_per_share
    contracts = max(1, snapshot.contracts)

    exit_, exc = walk_intraday(
        plan, entry_net, bars_by_minute,
        entry_ts=snapshot.generated_at, session_close_exit=session_close_exit,
    )
    if exit_ is None:
        return None

    gross = round((exit_.exit_net - entry_net) * 100.0 * contracts, 2)
    costs = 0.0
    if include_costs:
        from app.analytics.outcomes import _resolution_costs
        from app.config import settings

        costs = _resolution_costs(len(plan.legs), contracts, 0.0, settings)
    net = round(gross - costs, 2)

    max_loss = snapshot.max_loss_usd or abs(entry_net) * 100 * contracts
    band = abs(max_loss) * SCRATCH_BAND_FRAC
    result = (OutcomeResult.WIN if net > band
              else OutcomeResult.LOSS if net < -band
              else OutcomeResult.SCRATCH)

    hold_min = round((exit_.exit_ts - snapshot.generated_at).total_seconds() / 60.0)

    return DecisionOutcome(
        decision_id=snapshot.decision_id,
        symbol=snapshot.symbol,
        horizon_label="managed_exit_intraday",
        resolved_at=resolved_at,
        elapsed_days=max(0, (exit_.exit_ts.date() - snapshot.generated_at.date()).days),
        result=result,
        realized_pnl_usd=net,
        realized_pnl_gross_usd=gross,
        costs_usd=round(costs, 2),
        used_bs_fallback=False,
        outcome_source=OUTCOME_SOURCE,
        exit_reason=exit_.reason,
        exit_price_per_share=exit_.exit_net,
        exit_ts=exit_.exit_ts,
        hold_minutes=hold_min,
        mfe_per_share=exc.mfe_per_share if exc else None,
        mae_per_share=exc.mae_per_share if exc else None,
        mfe_ts=exc.mfe_ts if exc else None,
        mae_ts=exc.mae_ts if exc else None,
        bars_observed=exc.bars_seen if exc else 0,
        note=(
            f"Exited {exit_.reason} at {exit_.exit_ts:%Y-%m-%d %H:%M}Z, "
            f"{exit_.exit_net:+.2f}/sh vs {entry_net:+.2f} entry, on 1-minute "
            f"TRADE bars ({exc.bars_seen if exc else 0} priced). Stop evaluated on "
            "the bar low before the target on the bar high, so a minute that "
            "traded through both books as a loss."
        ),
    )


async def load_minute_bars(
    plan: TradePlan, sessions: list[date]
) -> dict[datetime, dict[str, OptionMinuteBar]]:
    """Minute bars for every leg across the given sessions, keyed minute -> leg.

    One call per (leg, session): a 3-day hold on a 2-leg spread is 6 calls. The
    caller is responsible for keeping that within the UW rate limit.

    Returns {} when no intraday provider is configured, so the caller abstains
    rather than silently falling back to daily marks — a coarser grade wearing
    the intraday label is exactly the confusion this phase exists to remove.
    """
    from app.providers import registry

    try:
        prov = registry.intraday_options_provider()
    except Exception as exc:  # noqa: BLE001 — unconfigured feed -> caller abstains
        log.warning("intraday_provider_unavailable", error=str(exc))
        return {}
    if prov is None:
        return {}

    out: dict[datetime, dict[str, OptionMinuteBar]] = {}
    for leg in plan.legs:
        sym = occ_symbol(leg.symbol, leg.expiration, leg.option_type, leg.strike)
        for session in sessions:
            try:
                bars = await prov.get_option_minute_bars(sym, session)
            except Exception as exc:  # noqa: BLE001 — one leg/day must not kill the batch
                log.warning(
                    "intraday_bars_failed", option_symbol=sym,
                    session=session.isoformat(), error=str(exc),
                )
                continue
            for b in bars:
                out.setdefault(b.start_time, {})[sym] = b
    return out
