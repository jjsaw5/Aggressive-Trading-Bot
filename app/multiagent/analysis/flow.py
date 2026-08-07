"""Options-flow measurement — written around what flow does *not* tell you.

The spec is explicit: *"The system must NOT assume that every large options
transaction is bullish or bearish."* That is not a caution to keep in mind, it
is a design constraint, and it shows up here as three concrete rules:

1. **Premium, not contract count.** A thousand far-OTM lottery tickets and one
   institutional block are not comparable by size. Premium is the honest unit.
2. **Direction is a conclusion, not an input.** A call print may be a covered
   call, a hedge, a roll, a spread leg or a close. `implied_bias` is set only
   when the *net* premium leans past a configured threshold AND enough of the
   prints carry side information. Otherwise `direction_ambiguous` stays True and
   the scorer abstains.
3. **Missing side/OI data is missing.** `at_ask` is `bool | None` on the domain
   model for a reason: `None` means the vendor did not say. Counting `None` as
   "not at ask" would manufacture bid-side conviction out of silence.

The repository's own evidence sharpens this further:
`docs/FLOW_EXPERIMENT_DISPOSITION.md` records a pre-registered experiment that
failed to reject the null on flow-as-confirmer, and found at-ask aggression to
be *if anything negative* (chasing). So flow is scored as corroboration with a
small weight, its contradiction penalty is larger than any single credit, and
none of it is presented as validated edge.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from app.domain.enums import OptionType
from app.domain.options import FlowAlert
from app.multiagent.models.enums import BiasDirection, Direction, ValidationVerdict
from app.multiagent.models.measurements import (
    AbsenceReason,
    Measurement,
    Provenance,
)
from app.multiagent.models.validation import FlowSnapshot


def _premium_of(alert: FlowAlert) -> float | None:
    """Premium for an alert, or None. Never reconstructed from size alone.

    Deriving premium as `size x 100 x mark` when the vendor omitted it would
    invent a number that looks measured.
    """
    return alert.premium


def build_flow_snapshot(
    symbol: str,
    alerts: list[FlowAlert] | None,
    direction: Direction,
    *,
    now: datetime,
    lookback_hours: int,
    min_premium_usd: float,
    net_premium_ratio_strong: float,
    ask_side_share_strong: float,
    size_over_oi_ratio: float,
    provider_error: str | None = None,
) -> FlowSnapshot:
    snap = FlowSnapshot(symbol=symbol, as_of=now, lookback_hours=lookback_hours)

    if provider_error is not None:
        snap.caveats.append(f"flow provider error: {provider_error}")
        snap.verdict = ValidationVerdict.INSUFFICIENT_DATA
        snap.measurements.add(
            Measurement.absent(
                "flow_total_premium", AbsenceReason.PROVIDER_ERROR, unit="$", note=provider_error
            )
        )
        return snap

    if alerts is None:
        snap.caveats.append("no flow data retrieved")
        snap.verdict = ValidationVerdict.INSUFFICIENT_DATA
        snap.measurements.add(
            Measurement.absent("flow_total_premium", AbsenceReason.NO_DATA, unit="$")
        )
        return snap

    cutoff = now - timedelta(hours=lookback_hours)
    window = [a for a in alerts if a.ts >= cutoff]
    snap.alerts_considered = len(window)

    if not window:
        snap.caveats.append(f"no flow prints in the last {lookback_hours}h")
        snap.verdict = ValidationVerdict.INSUFFICIENT_DATA
        snap.measurements.add(
            Measurement.of("flow_alert_count", 0.0, provenance=Provenance.PROVIDER, as_of=now)
        )
        snap.measurements.add(
            Measurement.absent(
                "flow_total_premium", AbsenceReason.NO_DATA, unit="$", note="no prints in window"
            )
        )
        return snap

    call_prem = 0.0
    put_prem = 0.0
    ask_prem = 0.0
    bid_prem = 0.0
    side_known_prem = 0.0
    priced = 0
    unpriced = 0
    largest = 0.0
    by_strike: dict[float, float] = defaultdict(float)
    max_size_over_oi: float | None = None

    for a in window:
        prem = _premium_of(a)
        if prem is None:
            unpriced += 1
        else:
            priced += 1
            largest = max(largest, prem)
            if a.option_type is OptionType.CALL:
                call_prem += prem
            elif a.option_type is OptionType.PUT:
                put_prem += prem
            if a.at_ask is True:
                ask_prem += prem
                side_known_prem += prem
            elif a.at_ask is False:
                bid_prem += prem
                side_known_prem += prem
            if a.strike is not None:
                by_strike[a.strike] += prem
        if a.size is not None and a.open_interest:
            ratio = a.size / a.open_interest
            max_size_over_oi = ratio if max_size_over_oi is None else max(max_size_over_oi, ratio)

    total_directional = call_prem + put_prem
    snap.call_premium = round(call_prem, 2) if priced else None
    snap.put_premium = round(put_prem, 2) if priced else None
    snap.net_premium = round(call_prem - put_prem, 2) if priced else None
    snap.ask_side_premium = round(ask_prem, 2) if side_known_prem else None
    snap.bid_side_premium = round(bid_prem, 2) if side_known_prem else None
    snap.sweep_count = sum(1 for a in window if a.is_sweep)
    snap.largest_print_premium = round(largest, 2) if priced else None
    snap.max_size_over_oi = round(max_size_over_oi, 3) if max_size_over_oi is not None else None
    snap.likely_opening = (
        None if max_size_over_oi is None else max_size_over_oi >= size_over_oi_ratio
    )

    if by_strike:
        ordered = sorted(by_strike.items(), key=lambda kv: kv[1], reverse=True)
        snap.top_strikes = [k for k, _ in ordered[:3]]
        top_total = sum(v for _, v in ordered[:3])
        snap.concentration_ratio = (
            round(top_total / total_directional, 3) if total_directional > 0 else None
        )

    if unpriced:
        snap.caveats.append(
            f"{unpriced} of {len(window)} prints carried no premium and were excluded "
            "from the premium totals rather than being estimated"
        )

    # --- measurements the scorer reads ---------------------------------
    ms = snap.measurements
    ms.add(Measurement.of("flow_alert_count", float(len(window)), provenance=Provenance.PROVIDER, as_of=now))
    ms.add(
        Measurement.of(
            "flow_total_premium",
            round(total_directional, 2) if priced else None,
            unit="$",
            provenance=Provenance.PROVIDER,
            as_of=now,
            note="calls plus puts, prints with a stated premium only",
        )
    )

    # Signed net premium ratio, oriented to the CANDIDATE's direction. Positive
    # means flow leans with the thesis; negative means against it.
    if priced and total_directional > 0:
        net_ratio = (call_prem - put_prem) / total_directional
        oriented = net_ratio if direction == Direction.BULLISH else -net_ratio
        ms.add(
            Measurement.of(
                "flow_net_premium_ratio",
                round(oriented, 4),
                provenance=Provenance.DERIVED,
                as_of=now,
                note="oriented to the candidate's direction: >0 leans with the thesis",
            )
        )
    else:
        ms.add(
            Measurement.absent(
                "flow_net_premium_ratio",
                AbsenceReason.NO_DATA,
                note="no prints carried a premium",
            )
        )

    if side_known_prem > 0:
        ms.add(
            Measurement.of(
                "flow_ask_side_share",
                round(ask_prem / side_known_prem, 4),
                provenance=Provenance.DERIVED,
                as_of=now,
                note=(
                    "share of side-known premium executed at the ask. Prints where the "
                    "vendor did not state a side are excluded, not counted as bid-side."
                ),
            )
        )
    else:
        ms.add(
            Measurement.absent(
                "flow_ask_side_share",
                AbsenceReason.NO_DATA,
                note="no print carried side information",
            )
        )

    ms.add(Measurement.of("flow_sweep_count", float(snap.sweep_count), provenance=Provenance.PROVIDER, as_of=now))
    ms.add(
        Measurement.of(
            "flow_max_size_over_oi",
            snap.max_size_over_oi,
            unit="x",
            provenance=Provenance.DERIVED,
            as_of=now,
            note="size above open interest implies an opening position; None when OI was absent",
        )
    )
    ms.add(
        Measurement.of(
            "flow_concentration_ratio",
            snap.concentration_ratio,
            provenance=Provenance.DERIVED,
            as_of=now,
            note="top-3 strikes' share of directional premium",
        )
    )

    # --- interpretation ------------------------------------------------
    below_threshold = (not priced) or total_directional < min_premium_usd
    ask_share_m = ms.get("flow_ask_side_share")

    if below_threshold:
        snap.direction_ambiguous = True
        snap.implied_bias = BiasDirection.UNKNOWN
        snap.verdict = ValidationVerdict.INSUFFICIENT_DATA
        snap.interpretation = (
            f"Total directional premium "
            f"{'unavailable' if not priced else f'${total_directional:,.0f}'} is below the "
            f"${min_premium_usd:,.0f} threshold at which this system will read flow at all."
        )
        snap.caveats.append("premium below significance threshold — flow scoring abstains")
        return snap

    raw_net = (call_prem - put_prem) / total_directional
    leans = abs(raw_net) >= net_premium_ratio_strong
    snap.implied_bias = (
        BiasDirection.BULLISH if raw_net > 0 else BiasDirection.BEARISH
    ) if leans else BiasDirection.NEUTRAL
    snap.direction_ambiguous = not leans

    agrees = (snap.implied_bias is BiasDirection.BULLISH and direction == Direction.BULLISH) or (
        snap.implied_bias is BiasDirection.BEARISH and direction == Direction.BEARISH
    )
    opposes = (snap.implied_bias is BiasDirection.BULLISH and direction == Direction.BEARISH) or (
        snap.implied_bias is BiasDirection.BEARISH and direction == Direction.BULLISH
    )

    if snap.direction_ambiguous:
        snap.verdict = ValidationVerdict.MIXED
        snap.interpretation = (
            f"Call/put premium split is near even (net ratio {raw_net:+.2f} versus the "
            f"{net_premium_ratio_strong:.2f} threshold). This is not evidence for either "
            "direction and is not read as confirmation."
        )
    elif agrees:
        snap.verdict = ValidationVerdict.CONFIRMS
        snap.interpretation = (
            f"Net premium leans {snap.implied_bias.value} (ratio {raw_net:+.2f}), agreeing with "
            "the candidate's direction."
        )
    elif opposes:
        snap.verdict = ValidationVerdict.CONTRADICTS
        snap.interpretation = (
            f"Net premium leans {snap.implied_bias.value} (ratio {raw_net:+.2f}), against the "
            "candidate's direction."
        )
    else:
        snap.verdict = ValidationVerdict.MIXED
        snap.interpretation = "Flow direction could not be related to the candidate's direction."

    # The caveats are not decoration: they are what stops a reader treating the
    # verdict above as more than it is.
    if ask_share_m.present:
        snap.caveats.append(
            f"at-ask share {ask_share_m.require():.0%} of side-known premium. Aggressive buying "
            "is not proof of informed buying — this repository's own pre-registered flow "
            "experiment (docs/FLOW_EXPERIMENT_DISPOSITION.md) found at-ask aggression to be, "
            "if anything, negatively associated with outcome."
        )
    else:
        snap.caveats.append("no side information on any print — aggression is unknown, not absent")

    if snap.likely_opening is None:
        snap.caveats.append("open interest absent — opening versus closing cannot be inferred")
    elif not snap.likely_opening:
        snap.caveats.append(
            "largest print is smaller than open interest — could be a close or a roll rather "
            "than a new position"
        )
    return snap
