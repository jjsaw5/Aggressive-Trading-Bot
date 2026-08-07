"""The eight scoring categories.

Each function takes measured snapshots plus its config block and returns a
`ScoreComponent` whose rules carry their own evidence. No function here reads an
agent's prose, and none takes an LLM output as an input to arithmetic — the one
exception is `invalidation_defined`, which checks that the agent *wrote* an
invalidation condition, not whether it agrees with one.

Category totals are asserted against the configured weights in
`tests/multiagent/test_scoring_weights.py`, so a rule whose points do not add up
to its category's weight fails the suite rather than quietly capping a category.
"""

from __future__ import annotations

from app.multiagent.config import (
    CatalystRules,
    DataQualityRules,
    FlowRules,
    IVGreeksRules,
    LiquidityRules,
    MarketAlignmentRules,
    RiskRewardRules,
    TechnicalRules,
)
from app.multiagent.models.contracts import ProposedStructure
from app.multiagent.models.enums import Direction, EvidenceQuality
from app.multiagent.models.measurements import (
    AbsenceReason,
    Measurement,
    MeasurementSet,
    Provenance,
)
from app.multiagent.models.scoring import ScoreComponent, ScoreRule
from app.multiagent.models.validation import (
    CatalystValidation,
    ContractQualitySnapshot,
    FlowSnapshot,
    MarketAlignmentSnapshot,
    RiskRewardSnapshot,
    TechnicalSnapshot,
)
from app.multiagent.scoring.rules import (
    abstain,
    band_rule,
    boolean_rule,
    penalty_rule,
    threshold_rule,
)


def _flag(name: str, condition: bool | None, note: str = "") -> Measurement:
    """A boolean rendered as a measurement so it can be audited like any other."""
    return Measurement.of(
        name,
        None if condition is None else (1.0 if condition else 0.0),
        provenance=Provenance.DERIVED,
        reason=AbsenceReason.NO_DATA,
        note=note,
    )


# --- 1. Catalyst strength (15) ---------------------------------------------


def score_catalyst_strength(
    cv: CatalystValidation | None, cfg: CatalystRules, weight: float
) -> ScoreComponent:
    comp = ScoreComponent(category="catalyst_strength", weight=weight)
    if cv is None:
        comp.rules.append(
            abstain("catalyst.all", "catalyst validation did not run", weight, None)
        )
        comp.notes.append("no catalyst validation available")
        return comp

    comp.rules.append(
        boolean_rule(
            "catalyst.confirmed_scheduled",
            "catalyst is a confirmed, dated, scheduled event",
            cv.is_scheduled and cv.evidence_quality is EvidenceQuality.CONFIRMED_FACT,
            points=cfg.confirmed_scheduled,
            measurement=_flag("is_scheduled_confirmed_fact", cv.is_scheduled),
        )
    )
    comp.rules.append(
        boolean_rule(
            "catalyst.sourced",
            "catalyst resolves to retrieved evidence from a named source",
            bool(cv.resolved_evidence_ids)
            and cv.evidence_quality
            in (EvidenceQuality.CONFIRMED_FACT, EvidenceQuality.REPORTED),
            points=cfg.sourced_news,
            measurement=Measurement.of(
                "resolved_evidence_count",
                float(len(cv.resolved_evidence_ids)),
                provenance=Provenance.DERIVED,
            ),
        )
    )
    comp.rules.append(
        boolean_rule(
            "catalyst.timing",
            "catalyst lands inside the expected holding period",
            cv.within_expected_horizon,
            points=cfg.timing_within_horizon,
            measurement=_flag("within_expected_horizon", cv.within_expected_horizon),
        )
    )
    # "Importance" is corroboration count, not the agent's own rating. An LLM's
    # importance score is displayed but never summed.
    comp.rules.append(
        threshold_rule(
            "catalyst.corroboration",
            f"at least {cfg.corroboration_min_items:g} independent evidence items back the catalyst",
            Measurement.of(
                "resolved_evidence_count",
                float(len(cv.resolved_evidence_ids)),
                provenance=Provenance.DERIVED,
            ),
            points=cfg.high_importance,
            threshold=cfg.corroboration_min_items,
        )
    )
    age = cv.newest_evidence_age_days
    comp.rules.append(
        penalty_rule(
            "catalyst.stale",
            f"newest supporting item older than {cfg.max_news_age_days} days",
            None if (age is None and not cv.is_scheduled) else (age is not None and age > cfg.max_news_age_days),
            points=cfg.stale_news_penalty,
            measurement=Measurement.of(
                "newest_evidence_age_days", age, unit="d", provenance=Provenance.DERIVED
            ),
            threshold=float(cfg.max_news_age_days),
            detail="stale catalyst",
        )
    )
    comp.rules.append(
        penalty_rule(
            "catalyst.priced_in",
            f"underlying already moved more than {cfg.priced_in_move_pct}% with the thesis",
            cv.likely_priced_in,
            points=cfg.already_priced_in_penalty,
            measurement=Measurement.of(
                "move_since_catalyst_pct",
                cv.move_since_catalyst_pct,
                unit="%",
                provenance=Provenance.DERIVED,
            ),
            threshold=cfg.priced_in_move_pct,
            detail="heuristic, not a measure of information absorption",
        )
    )
    if cv.unresolved_refs:
        comp.notes.append(
            f"{len(cv.unresolved_refs)} agent evidence reference(s) did not resolve and were dropped"
        )
    return comp


# --- 2. Market / sector alignment (10) --------------------------------------


def score_market_alignment(
    al: MarketAlignmentSnapshot | None, cfg: MarketAlignmentRules, weight: float
) -> ScoreComponent:
    comp = ScoreComponent(category="market_alignment", weight=weight)
    if al is None:
        comp.rules.append(abstain("alignment.all", "alignment did not run", weight, None))
        return comp

    comp.rules.append(
        boolean_rule(
            "alignment.spy",
            "trade direction agrees with SPY's measured bias",
            al.aligned_with_spy,
            points=cfg.spy_aligned,
            measurement=_flag("aligned_with_spy", al.aligned_with_spy, f"spy_bias={al.spy_bias.value}"),
        )
    )
    comp.rules.append(
        boolean_rule(
            "alignment.qqq",
            "trade direction agrees with QQQ's measured bias",
            al.aligned_with_qqq,
            points=cfg.qqq_aligned,
            measurement=_flag("aligned_with_qqq", al.aligned_with_qqq, f"qqq_bias={al.qqq_bias.value}"),
        )
    )
    comp.rules.append(
        boolean_rule(
            "alignment.sector",
            "trade direction agrees with the sector proxy's measured bias",
            al.aligned_with_sector,
            points=cfg.sector_aligned,
            measurement=_flag(
                "aligned_with_sector",
                al.aligned_with_sector,
                f"proxy={al.sector_proxy or 'none'}",
            ),
        )
    )
    comp.rules.append(
        threshold_rule(
            "alignment.relative_strength",
            "outperforms SPY in the thesis direction",
            al.measurements.get("relative_strength_vs_spy"),
            points=cfg.relative_strength,
            threshold=0.0,
            higher_is_better=True,
        )
    )
    comp.rules.append(
        penalty_rule(
            "alignment.fighting_tape",
            "direction opposes both SPY and QQQ",
            al.fighting_the_tape,
            points=cfg.fighting_tape_penalty,
            measurement=_flag("fighting_the_tape", al.fighting_the_tape),
            detail="trade fights both major benchmarks",
        )
    )
    return comp


# --- 3. Technical setup (20) ------------------------------------------------


def score_technical_setup(
    tech: TechnicalSnapshot | None, direction: Direction, cfg: TechnicalRules, weight: float
) -> ScoreComponent:
    comp = ScoreComponent(category="technical_setup", weight=weight)
    if tech is None:
        comp.rules.append(abstain("technical.all", "technical snapshot did not run", weight, None))
        return comp

    ms: MeasurementSet = tech.measurements
    bullish = direction == Direction.BULLISH

    # Trend, oriented to the thesis: a bearish candidate wants a negative trend.
    trend = ms.get("trend_return_pct")
    oriented_trend = (
        Measurement.of(
            "trend_return_oriented_pct",
            (trend.require() if bullish else -trend.require()),
            unit="%",
            provenance=Provenance.DERIVED,
            note="20-bar return signed to the thesis direction",
        )
        if trend.present
        else Measurement.absent("trend_return_oriented_pct", AbsenceReason.NO_DATA, unit="%")
    )
    comp.rules.append(
        threshold_rule(
            "technical.trend_aligned",
            "20-bar trend runs with the thesis",
            oriented_trend,
            points=cfg.trend_aligned,
            threshold=0.0,
        )
    )

    sma20 = ms.get("pct_above_sma20")
    oriented_sma = (
        Measurement.of(
            "pct_above_sma20_oriented",
            (sma20.require() if bullish else -sma20.require()),
            unit="%",
            provenance=Provenance.DERIVED,
            note="distance from the 20-bar SMA, signed to the thesis",
        )
        if sma20.present
        else Measurement.absent("pct_above_sma20_oriented", AbsenceReason.NO_DATA, unit="%")
    )
    comp.rules.append(
        threshold_rule(
            "technical.key_ma",
            "price is on the thesis side of the 20-bar SMA",
            oriented_sma,
            points=cfg.above_below_key_ma,
            threshold=0.0,
        )
    )
    comp.rules.append(
        threshold_rule(
            "technical.relative_volume",
            f"relative volume at or above {cfg.rel_volume_strong}x",
            ms.get("relative_volume"),
            points=cfg.relative_volume,
            threshold=cfg.rel_volume_strong,
            partial=True,
            floor=1.0,  # 1.0x is average volume: no signal, no credit
        )
    )
    comp.rules.append(
        threshold_rule(
            "technical.momentum",
            "majority of recent closes moved with the thesis",
            ms.get("momentum_agreement_ratio"),
            points=cfg.momentum_confirmation,
            threshold=cfg.momentum_agreement_strong,
            partial=True,
            floor=cfg.momentum_agreement_floor,
        )
    )

    # Room to run: distance to the opposing level, in ATR.
    room_key = "distance_to_resistance_atr" if bullish else "distance_to_support_atr"
    comp.rules.append(
        threshold_rule(
            "technical.room_to_target",
            f"at least {cfg.crowded_level_atr * 2:g} ATR of room to the opposing level",
            ms.get(room_key),
            points=cfg.room_to_target,
            threshold=cfg.crowded_level_atr * 2,
            partial=True,
            floor=cfg.crowded_level_atr,  # at the crowded distance, no credit
        )
    )
    comp.rules.append(
        band_rule(
            "technical.atr_supports_move",
            "daily range is large enough to matter but not disorderly",
            ms.get("atr_pct"),
            points=cfg.atr_supports_move,
            low=cfg.atr_pct_min,
            high=cfg.atr_pct_max,
        )
    )

    room = ms.get(room_key)
    comp.rules.append(
        penalty_rule(
            "technical.crowded_level",
            f"within {cfg.crowded_level_atr} ATR of the opposing level",
            (room.require() < cfg.crowded_level_atr) if room.present else None,
            points=cfg.crowded_level_penalty,
            measurement=room,
            threshold=cfg.crowded_level_atr,
            detail="price is pressed against the level it must clear",
        )
    )
    ext = ms.get("extension_atr")
    comp.rules.append(
        penalty_rule(
            "technical.extended",
            f"more than {cfg.extended_atr_multiple} ATR from the 20-bar SMA",
            (ext.require() > cfg.extended_atr_multiple) if ext.present else None,
            points=cfg.extended_penalty,
            measurement=ext,
            threshold=cfg.extended_atr_multiple,
            detail="entry would be chasing an extended move",
        )
    )
    comp.notes.extend(tech.notes[:4])
    return comp


# --- 4. Options flow confirmation (15) --------------------------------------


def score_options_flow(flow: FlowSnapshot | None, cfg: FlowRules, weight: float) -> ScoreComponent:
    comp = ScoreComponent(category="options_flow", weight=weight)
    if flow is None:
        comp.rules.append(abstain("flow.all", "flow snapshot did not run", weight, None))
        return comp

    ms = flow.measurements
    total = ms.get("flow_total_premium")

    # Below the significance threshold the entire category abstains. Reading
    # thin flow is worse than not reading it: it manufactures confirmation from
    # noise, and this repository's own pre-registered experiment
    # (docs/FLOW_EXPERIMENT_DISPOSITION.md) failed to reject the null on
    # flow-as-confirmer even at size.
    if not total.present or total.require() < cfg.min_premium_usd:
        for rid, desc, pts in (
            ("flow.direction", "net premium leans with the thesis", cfg.directional_agreement),
            ("flow.ask_side", "aggressive (at-ask) buying", cfg.ask_side_aggression),
            ("flow.sweeps", "sweep prints present", cfg.sweep_presence),
            ("flow.size_vs_oi", "print size above open interest", cfg.size_vs_open_interest),
            ("flow.concentration", "premium concentrated in coherent strikes", cfg.concentration),
        ):
            comp.rules.append(abstain(rid, desc, pts, total))
        comp.notes.append(
            f"flow premium below the ${cfg.min_premium_usd:,.0f} significance threshold — the "
            "whole category abstains rather than scoring noise"
        )
        return comp

    comp.rules.append(
        threshold_rule(
            "flow.direction",
            f"net premium leans with the thesis by at least {cfg.net_premium_ratio_strong}",
            ms.get("flow_net_premium_ratio"),
            points=cfg.directional_agreement,
            threshold=cfg.net_premium_ratio_strong,
            partial=True,
            floor=0.0,  # an even call/put split leans nowhere
        )
    )
    comp.rules.append(
        threshold_rule(
            "flow.ask_side",
            f"at-ask share of side-known premium at or above {cfg.ask_side_share_strong}",
            ms.get("flow_ask_side_share"),
            points=cfg.ask_side_aggression,
            threshold=cfg.ask_side_share_strong,
        )
    )
    comp.rules.append(
        threshold_rule(
            "flow.sweeps",
            "at least one sweep print",
            ms.get("flow_sweep_count"),
            points=cfg.sweep_presence,
            threshold=1.0,
        )
    )
    comp.rules.append(
        threshold_rule(
            "flow.size_vs_oi",
            f"largest print at or above {cfg.size_over_oi_ratio}x open interest (likely opening)",
            ms.get("flow_max_size_over_oi"),
            points=cfg.size_vs_open_interest,
            threshold=cfg.size_over_oi_ratio,
        )
    )
    comp.rules.append(
        threshold_rule(
            "flow.concentration",
            "top-3 strikes hold a majority of directional premium",
            ms.get("flow_concentration_ratio"),
            points=cfg.concentration,
            threshold=cfg.concentration_strong,
            partial=True,
            floor=cfg.concentration_floor,
        )
    )
    net = ms.get("flow_net_premium_ratio")
    comp.rules.append(
        penalty_rule(
            "flow.contradiction",
            "net premium leans against the thesis",
            (net.require() <= -cfg.net_premium_ratio_strong) if net.present else None,
            points=cfg.contradiction_penalty,
            measurement=net,
            threshold=-cfg.net_premium_ratio_strong,
            detail="flow argues the other way",
        )
    )
    if flow.direction_ambiguous:
        comp.notes.append(
            "flow direction is ambiguous — a large print is not inherently bullish or bearish"
        )
    comp.notes.extend(flow.caveats[:3])
    return comp


# --- 5. IV / Greeks structure (10) ------------------------------------------


def score_iv_greeks(
    cq: ContractQualitySnapshot | None,
    rr: RiskRewardSnapshot | None,
    cfg: IVGreeksRules,
    weight: float,
    *,
    long_delta_min: float,
    long_delta_max: float,
) -> ScoreComponent:
    comp = ScoreComponent(category="iv_greeks", weight=weight)
    if cq is None:
        comp.rules.append(abstain("iv.all", "contract quality did not run", weight, None))
        return comp

    ms = cq.measurements
    # Debit structures are long premium, so LOW IV rank is the favourable case.
    comp.rules.append(
        threshold_rule(
            "iv.rank_favorable",
            f"IV rank at or below {cfg.iv_rank_low} (cheap premium for a debit structure)",
            ms.get("iv_rank"),
            points=cfg.iv_rank_favorable,
            threshold=cfg.iv_rank_low,
            higher_is_better=False,
        )
    )
    slope = ms.get("term_structure_slope")
    comp.rules.append(
        threshold_rule(
            "iv.term_structure",
            "term structure is not in backwardation",
            slope,
            points=cfg.term_structure_ok,
            threshold=0.0,
            higher_is_better=True,
        )
    )
    comp.rules.append(
        band_rule(
            "iv.delta_band",
            f"long leg's delta inside [{long_delta_min}, {long_delta_max}]",
            ms.get("long_leg_abs_delta"),
            points=cfg.delta_in_band,
            low=long_delta_min,
            high=long_delta_max,
        )
    )
    theta = rr.measurements.get("theta_burden") if rr else Measurement.absent(
        "theta_burden", AbsenceReason.NOT_IMPLEMENTED
    )
    comp.rules.append(
        threshold_rule(
            "iv.theta_tolerable",
            f"decay over the hold at or below {cfg.theta_burden_max:.0%} of premium paid",
            theta,
            points=cfg.theta_tolerable,
            threshold=cfg.theta_burden_max,
            higher_is_better=False,
        )
    )
    rank = ms.get("iv_rank")
    comp.rules.append(
        penalty_rule(
            "iv.elevated",
            f"IV rank above {cfg.iv_rank_high} — paying up for premium",
            (rank.require() > cfg.iv_rank_high) if rank.present else None,
            points=cfg.iv_elevated_penalty,
            measurement=rank,
            threshold=cfg.iv_rank_high,
            detail="elevated IV raises the cost of a debit structure and its crush exposure",
        )
    )
    comp.notes.extend(cq.notes[:3])
    return comp


# --- 6. Contract liquidity (10) ---------------------------------------------


def score_contract_liquidity(
    cq: ContractQualitySnapshot | None, cfg: LiquidityRules, weight: float
) -> ScoreComponent:
    comp = ScoreComponent(category="contract_liquidity", weight=weight)
    if cq is None:
        comp.rules.append(abstain("liquidity.all", "contract quality did not run", weight, None))
        return comp

    ms = cq.measurements
    spread = ms.get("worst_leg_spread_pct")
    if spread.present:
        value = spread.require()
        if value <= cfg.spread_pct_excellent:
            awarded = cfg.spread_tight
            detail = f"{value:.1%} is at or inside the {cfg.spread_pct_excellent:.0%} excellent bar"
        elif value <= cfg.spread_pct_good:
            awarded = round(cfg.spread_tight * 0.6, 3)
            detail = f"{value:.1%} clears the {cfg.spread_pct_good:.0%} good bar but not excellent"
        else:
            awarded = 0.0
            detail = f"{value:.1%} is wider than the {cfg.spread_pct_good:.0%} good bar"
        comp.rules.append(
            ScoreRule(
                rule_id="liquidity.spread",
                description="widest leg's bid/ask spread",
                points_awarded=awarded,
                points_possible=cfg.spread_tight,
                measurement=spread,
                threshold=cfg.spread_pct_good,
                comparison="<=",
                detail=detail,
            )
        )
    else:
        comp.rules.append(
            abstain("liquidity.spread", "widest leg's bid/ask spread", cfg.spread_tight, spread)
        )

    comp.rules.append(
        _tiered(
            "liquidity.open_interest",
            "open interest on the thinnest leg",
            ms.get("min_open_interest"),
            points=cfg.open_interest,
            good=float(cfg.open_interest_good),
            ok=float(cfg.open_interest_ok),
        )
    )
    comp.rules.append(
        _tiered(
            "liquidity.volume",
            "session volume on the thinnest leg",
            ms.get("min_volume"),
            points=cfg.volume,
            good=float(cfg.volume_good),
            ok=float(cfg.volume_ok),
        )
    )
    return comp


def _tiered(
    rule_id: str, description: str, m: Measurement, *, points: float, good: float, ok: float
) -> ScoreRule:
    """Full points at `good`, 60% at `ok`, none below. Abstains when absent."""
    if not m.present:
        return abstain(rule_id, description, points, m)
    value = m.require()
    if value >= good:
        awarded, detail = points, f"{value:g} clears the {good:g} bar"
    elif value >= ok:
        awarded, detail = round(points * 0.6, 3), f"{value:g} clears {ok:g} but not {good:g}"
    else:
        awarded, detail = 0.0, f"{value:g} is below the {ok:g} minimum for credit"
    return ScoreRule(
        rule_id=rule_id,
        description=description,
        points_awarded=awarded,
        points_possible=points,
        measurement=m,
        threshold=good,
        comparison=">=",
        detail=detail,
    )


# --- 7. Risk / reward (15) --------------------------------------------------


def score_risk_reward(
    rr: RiskRewardSnapshot | None,
    structure: ProposedStructure | None,
    cfg: RiskRewardRules,
    weight: float,
) -> ScoreComponent:
    comp = ScoreComponent(category="risk_reward", weight=weight)
    if rr is None:
        comp.rules.append(abstain("rr.all", "risk/reward did not run", weight, None))
        return comp

    ms = rr.measurements
    rr_m = ms.get("reward_to_risk")
    if rr_m.present:
        value = rr_m.require()
        if value >= cfg.rr_excellent:
            awarded, detail = cfg.reward_to_risk, f"{value:.2f} clears the {cfg.rr_excellent:g} bar"
        elif value >= cfg.rr_good:
            awarded, detail = round(cfg.reward_to_risk * 0.7, 3), f"{value:.2f} clears {cfg.rr_good:g}"
        elif value >= cfg.rr_minimum:
            awarded, detail = round(cfg.reward_to_risk * 0.4, 3), f"{value:.2f} clears {cfg.rr_minimum:g}"
        else:
            awarded, detail = 0.0, f"{value:.2f} is below the {cfg.rr_minimum:g} minimum"
        comp.rules.append(
            ScoreRule(
                rule_id="rr.reward_to_risk",
                description="reward-to-risk on defined max profit and loss",
                points_awarded=awarded,
                points_possible=cfg.reward_to_risk,
                measurement=rr_m,
                threshold=cfg.rr_good,
                comparison=">=",
                detail=detail,
            )
        )
    else:
        comp.rules.append(
            abstain("rr.reward_to_risk", "reward-to-risk", cfg.reward_to_risk, rr_m)
        )

    comp.rules.append(
        threshold_rule(
            "rr.breakeven_reachable",
            f"breakeven move at or under {cfg.breakeven_over_expected_move_max:.0%} of the implied move",
            ms.get("breakeven_over_expected_move"),
            points=cfg.breakeven_reachable,
            threshold=cfg.breakeven_over_expected_move_max,
            higher_is_better=False,
        )
    )
    comp.rules.append(
        threshold_rule(
            "rr.within_budget",
            "defined risk fits inside the per-trade budget",
            ms.get("risk_budget_utilisation"),
            points=cfg.within_risk_budget,
            threshold=1.0,
            higher_is_better=False,
        )
    )
    comp.rules.append(
        threshold_rule(
            "rr.invalidation_defined",
            "candidate states a checkable invalidation condition",
            ms.get("invalidation_defined"),
            points=cfg.invalidation_defined,
            threshold=1.0,
        )
    )
    if structure is not None and structure.contracts == 0:
        comp.notes.append("structure is unsizeable within the per-trade risk cap")
    comp.notes.extend(rr.notes[:3])
    return comp


# --- 8. Data agreement / data quality (5) -----------------------------------


def score_data_quality(
    *,
    cross_check: Measurement,
    quote_age_seconds: Measurement,
    coverage: Measurement,
    cfg: DataQualityRules,
    weight: float,
) -> ScoreComponent:
    comp = ScoreComponent(category="data_quality", weight=weight)
    comp.rules.append(
        threshold_rule(
            "data.providers_agree",
            f"independent price sources within {cfg.price_disagreement_pct}%",
            cross_check,
            points=cfg.providers_agree,
            threshold=cfg.price_disagreement_pct,
            higher_is_better=False,
        )
    )
    comp.rules.append(
        threshold_rule(
            "data.freshness",
            f"underlying quote no older than {cfg.max_quote_age_seconds}s",
            quote_age_seconds,
            points=cfg.data_fresh,
            threshold=float(cfg.max_quote_age_seconds),
            higher_is_better=False,
        )
    )
    comp.rules.append(
        threshold_rule(
            "data.coverage",
            f"at least {cfg.coverage_for_bonus:.0%} of scoring inputs were measurable",
            coverage,
            points=cfg.full_coverage,
            threshold=cfg.coverage_for_bonus,
        )
    )
    return comp
