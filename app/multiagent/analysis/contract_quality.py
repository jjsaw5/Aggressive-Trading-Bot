"""Contract quality and risk/reward measurement over a priced structure.

Everything here is arithmetic on numbers the chain supplied or the selector
computed. No thresholds are applied — that is the scoring engine's job, using
`config/methodology.yaml`. Keeping measurement and judgement apart is what makes
the score auditable: you can read what was measured without reading how it was
graded, and change the grading without touching the measurement.
"""

from __future__ import annotations

from datetime import datetime

from app.domain.options import IVContext
from app.multiagent.analysis.catalyst import horizon_days
from app.multiagent.models.candidates import ResearchCandidate
from app.multiagent.models.contracts import ProposedStructure
from app.multiagent.models.enums import TimeHorizon
from app.multiagent.models.measurements import (
    AbsenceReason,
    Measurement,
    Provenance,
)
from app.multiagent.models.validation import ContractQualitySnapshot, RiskRewardSnapshot

# A plausible IV contraction after a scheduled event, used to size crush
# exposure. Explicitly a scenario, not a forecast, and labeled as such wherever
# the resulting number surfaces.
_IV_CRUSH_SCENARIO_POINTS = 10.0


def build_contract_quality(
    structure: ProposedStructure,
    iv_context: IVContext | None,
    *,
    now: datetime,
) -> ContractQualitySnapshot:
    snap = ContractQualitySnapshot(structure_id=structure.structure_id, as_of=now)
    ms = snap.measurements

    snap.worst_spread_pct = structure.worst_leg_spread_pct
    snap.min_open_interest = structure.min_open_interest
    snap.min_volume = structure.min_volume

    long_leg = structure.legs[0] if structure.legs else None
    snap.iv = long_leg.implied_volatility if long_leg else None

    if iv_context is not None:
        snap.iv_rank = iv_context.iv_rank
        snap.iv_percentile = iv_context.iv_percentile
        snap.iv_source = iv_context.iv_rank_source or iv_context.source
        snap.term_structure_slope = iv_context.term_structure_slope

    ms.add(
        Measurement.of(
            "worst_leg_spread_pct",
            snap.worst_spread_pct,
            unit="",
            provenance=Provenance.DERIVED,
            as_of=now,
            note="widest leg's bid/ask spread over its mid; None if any leg lacks a two-sided market",
        )
    )
    ms.add(
        Measurement.of(
            "min_open_interest",
            float(snap.min_open_interest) if snap.min_open_interest is not None else None,
            provenance=Provenance.PROVIDER,
            as_of=now,
        )
    )
    ms.add(
        Measurement.of(
            "min_volume",
            float(snap.min_volume) if snap.min_volume is not None else None,
            provenance=Provenance.PROVIDER,
            as_of=now,
        )
    )
    ms.add(
        Measurement.of(
            "iv_rank",
            snap.iv_rank,
            provenance=Provenance.PROVIDER,
            source=snap.iv_source,
            as_of=now,
            note="percentile of current IV against the symbol's own history",
        )
    )
    ms.add(
        Measurement.of(
            "term_structure_slope",
            snap.term_structure_slope,
            provenance=Provenance.PROVIDER,
            source=snap.iv_source,
            as_of=now,
            reason=AbsenceReason.NO_DATA,
            note="front-to-back IV slope; negative is backwardation",
        )
    )
    ms.add(
        Measurement.of(
            "net_delta",
            structure.net_delta,
            provenance=structure.greeks_source,
            as_of=now,
            note=f"greeks_source={structure.greeks_source.value}",
        )
    )
    ms.add(
        Measurement.of(
            "abs_net_delta",
            abs(structure.net_delta) if structure.net_delta is not None else None,
            provenance=structure.greeks_source,
            as_of=now,
        )
    )
    # The delta-band rule reads the LONG LEG, not the net.
    #
    # A vertical's net delta is the difference of two same-sign deltas and is
    # therefore small by construction — a perfectly sensible 0.45/0.30 spread
    # nets about 0.15. Grading that against the single-option band (0.35-0.65)
    # would fail every spread ever built, which is a bug in the rule rather than
    # a finding about the trade. What the band is actually asking is "is the
    # option we are long struck somewhere sensible", and that is the long leg's
    # delta. Net delta is still measured above, for position exposure.
    long_leg = next((leg for leg in structure.legs if leg.action.value == "buy_to_open"), None)
    ms.add(
        Measurement.of(
            "long_leg_abs_delta",
            abs(long_leg.greeks.delta)
            if (long_leg is not None and long_leg.greeks.delta is not None)
            else None,
            provenance=structure.greeks_source,
            as_of=now,
            note="absolute delta of the long leg; the strike-placement check",
        )
    )

    if snap.worst_spread_pct is None:
        snap.notes.append(
            "at least one leg had no two-sided market — spread is unknown, which is not the "
            "same as tight"
        )
    if structure.greeks_source is Provenance.MODELED:
        snap.notes.append(
            "Greeks are Black-Scholes model output, not provider-supplied observations"
        )
    return snap


def build_risk_reward(
    structure: ProposedStructure,
    candidate: ResearchCandidate,
    *,
    now: datetime,
    current_price: float | None,
    max_risk_usd: float,
) -> RiskRewardSnapshot:
    snap = RiskRewardSnapshot(structure_id=structure.structure_id, as_of=now)
    ms = snap.measurements

    snap.max_loss = structure.total_max_loss
    snap.max_profit = structure.total_max_profit
    snap.breakeven = structure.breakeven
    snap.reward_to_risk = structure.reward_to_risk

    spot = current_price if current_price is not None else structure.underlying_price
    if snap.breakeven is not None and spot:
        snap.breakeven_move_pct = round(abs(snap.breakeven - spot) / spot * 100.0, 4)

    hold = horizon_days(candidate.expected_holding_period or TimeHorizon.UNKNOWN)
    dte = structure.dte(now.date())
    # Theta is charged over the shorter of the hold and the contract's life —
    # you cannot pay decay past expiry.
    decay_days = min(hold, dte) if dte is not None else hold

    debit_per_contract = (
        structure.net_debit_per_share * 100.0 if structure.net_debit_per_share is not None else None
    )
    if structure.net_theta is not None and debit_per_contract:
        # net_theta is per share per day; x100 for one contract.
        theta_cost = abs(structure.net_theta) * 100.0 * decay_days
        snap.theta_burden = round(theta_cost / debit_per_contract, 4)

    if structure.net_vega is not None and debit_per_contract:
        crush = abs(structure.net_vega) * 100.0 * _IV_CRUSH_SCENARIO_POINTS
        snap.iv_crush_exposure = round(crush / debit_per_contract, 4)

    # IV-implied expected move over the hold: sigma * sqrt(days/365).
    long_leg = structure.legs[0] if structure.legs else None
    iv = long_leg.implied_volatility if long_leg else None
    if iv and decay_days > 0:
        snap.expected_move_pct = round(iv * (decay_days / 365.0) ** 0.5 * 100.0, 4)

    # For an unbounded long option, reward-to-risk at expiry is undefined. A
    # target-based figure is computed instead — from the IV-implied move, which
    # is a market-derived quantity rather than an invented target.
    if snap.reward_to_risk is None and snap.expected_move_pct and spot and snap.breakeven:
        bullish = candidate.is_bullish()
        target = spot * (1 + snap.expected_move_pct / 100.0 * (1 if bullish else -1))
        intrinsic = (target - snap.breakeven) if bullish else (snap.breakeven - target)
        if intrinsic > 0 and structure.max_loss_per_contract:
            snap.target_reward_to_risk = round(
                intrinsic * 100.0 / structure.max_loss_per_contract, 3
            )
            snap.notes.append(
                f"reward-to-risk at expiry is undefined for an unbounded long option; "
                f"{snap.target_reward_to_risk:.2f} is measured to an IV-implied "
                f"{snap.expected_move_pct:.1f}% target, not to a forecast"
            )

    ms.add(
        Measurement.of(
            "max_loss_usd",
            snap.max_loss,
            unit="$",
            provenance=Provenance.DERIVED,
            as_of=now,
            note=f"total defined risk for {structure.contracts} contract(s)",
        )
    )
    ms.add(
        Measurement.of(
            "reward_to_risk",
            snap.reward_to_risk if snap.reward_to_risk is not None else snap.target_reward_to_risk,
            provenance=Provenance.DERIVED,
            as_of=now,
            reason=AbsenceReason.NO_DATA,
            note=(
                "defined max profit over max loss"
                if snap.reward_to_risk is not None
                else "measured to an IV-implied target (long option has no capped profit)"
            ),
        )
    )
    ms.add(
        Measurement.of(
            "breakeven_move_pct",
            snap.breakeven_move_pct,
            unit="%",
            provenance=Provenance.DERIVED,
            as_of=now,
        )
    )
    ms.add(
        Measurement.of(
            "expected_move_pct",
            snap.expected_move_pct,
            unit="%",
            provenance=Provenance.MODELED,
            as_of=now,
            note=f"IV-implied one-sigma move over {decay_days}d; modeled, not forecast",
        )
    )
    if snap.breakeven_move_pct is not None and snap.expected_move_pct:
        ms.add(
            Measurement.of(
                "breakeven_over_expected_move",
                round(snap.breakeven_move_pct / snap.expected_move_pct, 4),
                provenance=Provenance.DERIVED,
                as_of=now,
                note="<1 means breakeven sits inside the market's own implied move",
            )
        )
    else:
        ms.add(
            Measurement.absent(
                "breakeven_over_expected_move",
                AbsenceReason.NO_DATA,
                note="needs both a breakeven and an IV-implied move",
            )
        )
    ms.add(
        Measurement.of(
            "theta_burden",
            snap.theta_burden,
            provenance=structure.greeks_source,
            as_of=now,
            note=f"decay over {decay_days}d as a share of premium paid",
        )
    )
    ms.add(
        Measurement.of(
            "iv_crush_exposure",
            snap.iv_crush_exposure,
            provenance=Provenance.MODELED,
            as_of=now,
            note=(
                f"loss from a {_IV_CRUSH_SCENARIO_POINTS:g}-point IV drop as a share of premium; "
                "a scenario, not a prediction"
            ),
        )
    )
    ms.add(
        Measurement.of(
            "risk_budget_utilisation",
            round(snap.max_loss / max_risk_usd, 4) if snap.max_loss and max_risk_usd else None,
            provenance=Provenance.DERIVED,
            as_of=now,
            note=f"total defined risk over the ${max_risk_usd:,.0f} per-trade cap",
        )
    )
    ms.add(
        Measurement.of(
            "cost_drag_pct",
            structure.cost_drag_pct,
            provenance=Provenance.DERIVED,
            as_of=now,
            note="round-trip spread tax as a share of defined max loss",
        )
    )
    ms.add(
        Measurement.of(
            "invalidation_defined",
            1.0 if candidate.invalidation_thesis.strip() else 0.0,
            provenance=Provenance.AGENT,
            as_of=now,
            note="whether the candidate states a checkable invalidation condition",
        )
    )

    if structure.contracts == 0:
        snap.notes.append(
            f"unsizeable: one contract risks ${structure.max_loss_per_contract or 0:,.2f} against "
            f"a ${max_risk_usd:,.0f} per-trade cap"
        )
    if dte is not None and dte < hold:
        snap.event_risk_notes.append(
            f"contract expires in {dte}d, inside the {hold}d expected hold — the thesis needs "
            "to resolve before expiry"
        )
    return snap
