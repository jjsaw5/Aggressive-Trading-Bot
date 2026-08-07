"""Hard rejection rules — terminal, and separate from scoring by design.

The spec's requirement: *"Do not allow a strong score in one area to override a
critical hard-risk failure."* The structural guarantee is that these rules never
see the score. They run over measurements only, and the orchestrator applies
them as a veto: a candidate that fails one is rejected whatever it scored, and
its score is still computed and stored so the future performance engine can ask
how rejected trades would have done.

Each rule returns a `HardRejection` with the code, the measured value and the
threshold, so a rejection is as auditable as an acceptance.

**Absence is not automatic failure.** Rules distinguish "measured and outside
the limit" from "not measured". A missing spread does not fail the spread rule —
it fails `MISSING_CRITICAL_DATA`, which is a different diagnosis with a
different fix. Two rules deliberately treat absence as failure, and say so:
critical-data presence and coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from app.multiagent.config import HardRuleConfig, MethodologyConfig
from app.multiagent.models.candidates import ResearchCandidate
from app.multiagent.models.contracts import ProposedStructure
from app.multiagent.models.enums import RejectionCode
from app.multiagent.models.scoring import CompositeScore
from app.multiagent.models.validation import ValidationReport


@dataclass(frozen=True)
class HardRejection:
    code: RejectionCode
    reason: str
    measured: float | str | None = None
    threshold: float | str | None = None

    def render(self) -> str:
        if self.measured is None:
            return f"[{self.code.value}] {self.reason}"
        return f"[{self.code.value}] {self.reason} (measured {self.measured}, limit {self.threshold})"


@dataclass
class RulesVerdict:
    rejections: list[HardRejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.rejections)

    def codes(self) -> list[str]:
        return [r.code.value for r in self.rejections]

    def reasons(self) -> list[str]:
        return [r.render() for r in self.rejections]


def evaluate_hard_rules(
    candidate: ResearchCandidate,
    report: ValidationReport,
    structure: ProposedStructure | None,
    cfg: MethodologyConfig,
    *,
    now: datetime | None = None,
    earnings_date: date | None = None,
    provider_price_disagreement_pct: float | None = None,
    score: CompositeScore | None = None,
) -> RulesVerdict:
    """Run every hard rule. Order is presentation only — all rules always run.

    Running all of them (rather than short-circuiting on the first) means a
    report can say a candidate failed on three counts, which is more useful than
    fixing one and rediscovering the next.
    """
    hr: HardRuleConfig = cfg.hard_rules
    when = now or datetime.now(UTC)
    v = RulesVerdict()

    # --- strategy allow-list -------------------------------------------
    if candidate.strategy_type.value not in set(cfg.strategies.allowed):
        v.rejections.append(
            HardRejection(
                RejectionCode.STRATEGY_NOT_ALLOWED,
                f"strategy {candidate.strategy_type.value} is not permitted",
                candidate.strategy_type.value,
                ", ".join(cfg.strategies.allowed),
            )
        )

    # --- catalyst must be verifiable -----------------------------------
    cv = report.catalyst
    if hr.require_catalyst_evidence:
        if cv is None:
            v.rejections.append(
                HardRejection(
                    RejectionCode.CATALYST_UNVERIFIED,
                    "catalyst validation did not run",
                )
            )
        elif not cv.resolved_evidence_ids:
            v.rejections.append(
                HardRejection(
                    RejectionCode.CATALYST_UNVERIFIED,
                    "no cited evidence resolved against the run's ledger — the stated catalyst "
                    "is unverifiable from anything retrieved",
                    0,
                    ">= 1 resolved evidence item",
                )
            )

    # --- a structure is required to judge anything about the contract ---
    if structure is None:
        v.rejections.append(
            HardRejection(
                RejectionCode.NO_VALID_CONTRACT,
                "no contract in the chain satisfied the configured DTE, delta and liquidity bands",
            )
        )
        _coverage_rule(score, hr, v)
        return v

    # --- liquidity ------------------------------------------------------
    spread = structure.worst_leg_spread_pct
    if spread is None:
        v.rejections.append(
            HardRejection(
                RejectionCode.MISSING_CRITICAL_DATA,
                "at least one leg had no two-sided market, so the spread is unknown. Unknown is "
                "not tight — a trade that cannot be priced cannot be risked",
            )
        )
    elif spread > hr.max_spread_pct:
        v.rejections.append(
            HardRejection(
                RejectionCode.SPREAD_TOO_WIDE,
                "widest leg's bid/ask spread exceeds the limit",
                round(spread, 4),
                hr.max_spread_pct,
            )
        )

    oi = structure.min_open_interest
    if oi is None:
        v.warnings.append("open interest was not supplied on every leg — liquidity is partly unknown")
    elif oi < hr.min_open_interest:
        v.rejections.append(
            HardRejection(
                RejectionCode.INSUFFICIENT_LIQUIDITY,
                "open interest on the thinnest leg is below the minimum",
                oi,
                hr.min_open_interest,
            )
        )

    vol = structure.min_volume
    if vol is None:
        v.warnings.append("session volume was not supplied on every leg")
    elif vol < hr.min_volume:
        v.rejections.append(
            HardRejection(
                RejectionCode.INSUFFICIENT_LIQUIDITY,
                "session volume on the thinnest leg is below the minimum",
                vol,
                hr.min_volume,
            )
        )

    # --- risk budget ----------------------------------------------------
    max_loss = structure.max_loss_per_contract
    if max_loss is None:
        v.rejections.append(
            HardRejection(
                RejectionCode.MISSING_CRITICAL_DATA,
                "maximum loss could not be computed from the quoted legs",
            )
        )
    elif max_loss > hr.max_defined_risk_usd:
        v.rejections.append(
            HardRejection(
                RejectionCode.COST_EXCEEDS_RISK_BUDGET,
                "a single contract's defined risk exceeds the per-trade cap "
                "(docs/RISK_POLICY.md)",
                round(max_loss, 2),
                hr.max_defined_risk_usd,
            )
        )
    elif structure.contracts < 1:
        v.rejections.append(
            HardRejection(
                RejectionCode.COST_EXCEEDS_RISK_BUDGET,
                "position could not be sized to at least one contract inside the risk budget",
                structure.contracts,
                ">= 1",
            )
        )

    if structure.contracts > hr.max_contracts:
        v.rejections.append(
            HardRejection(
                RejectionCode.COST_EXCEEDS_RISK_BUDGET,
                "contract count exceeds the concentration cap",
                structure.contracts,
                hr.max_contracts,
            )
        )

    # --- reward to risk -------------------------------------------------
    rr = report.risk_reward
    rr_value = None
    if rr is not None:
        rr_value = rr.reward_to_risk if rr.reward_to_risk is not None else rr.target_reward_to_risk
    if rr_value is None:
        v.warnings.append(
            "reward-to-risk is uncomputable (long options have no capped profit and no "
            "IV-implied target was available) — the risk/reward rule abstains rather than passing"
        )
    elif rr_value < hr.min_reward_to_risk:
        v.rejections.append(
            HardRejection(
                RejectionCode.REWARD_RISK_TOO_LOW,
                "reward-to-risk is below the minimum",
                round(rr_value, 3),
                hr.min_reward_to_risk,
            )
        )

    # --- theta ----------------------------------------------------------
    if rr is not None and rr.theta_burden is not None and rr.theta_burden > hr.max_theta_burden:
        v.rejections.append(
            HardRejection(
                RejectionCode.EXCESSIVE_THETA,
                "decay over the expected hold consumes too much of the premium paid",
                round(rr.theta_burden, 3),
                hr.max_theta_burden,
            )
        )

    # --- staleness ------------------------------------------------------
    as_of = structure.underlying_as_of or (structure.legs[0].as_of if structure.legs else None)
    if as_of is None:
        v.rejections.append(
            HardRejection(
                RejectionCode.MISSING_CRITICAL_DATA,
                "the quoted structure carries no timestamp, so its freshness cannot be established",
            )
        )
    else:
        stamped = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        age = (when - stamped).total_seconds()
        if age > hr.max_quote_age_seconds:
            v.rejections.append(
                HardRejection(
                    RejectionCode.STALE_QUOTE,
                    "option quotes are older than the freshness limit",
                    round(age, 1),
                    hr.max_quote_age_seconds,
                )
            )

    # --- earnings blackout ----------------------------------------------
    if earnings_date is not None:
        days = (earnings_date - when.date()).days
        inside = 0 <= days <= hr.earnings_blackout_days
        is_earnings_play = (
            candidate.earnings_date is not None
            and candidate.earnings_date == earnings_date
        )
        exempt = is_earnings_play and hr.earnings_blackout_applies_to_earnings_plays is False
        if inside and not exempt:
            v.rejections.append(
                HardRejection(
                    RejectionCode.EARNINGS_BLACKOUT,
                    f"earnings on {earnings_date.isoformat()} falls inside the prohibited window",
                    days,
                    hr.earnings_blackout_days,
                )
            )
        elif inside and exempt:
            v.warnings.append(
                f"earnings on {earnings_date.isoformat()} is {days}d away and IS the thesis — "
                "the blackout is waived by configuration, but expect an IV crush through the event"
            )

    # --- provider disagreement -------------------------------------------
    if (
        provider_price_disagreement_pct is not None
        and provider_price_disagreement_pct > hr.max_provider_price_disagreement_pct
    ):
        v.rejections.append(
            HardRejection(
                RejectionCode.PROVIDER_DISAGREEMENT,
                "independent underlying price sources disagree beyond the reconcilable limit",
                round(provider_price_disagreement_pct, 3),
                hr.max_provider_price_disagreement_pct,
            )
        )

    _coverage_rule(score, hr, v)
    return v


def _coverage_rule(score: CompositeScore | None, hr: HardRuleConfig, v: RulesVerdict) -> None:
    """Reject when too little of the rubric could actually be measured.

    Reads only `input_coverage`, never the score itself — a high score computed
    from a quarter of the inputs is exactly the case this rule exists to catch,
    and it must reject it regardless of how high that score is.
    """
    if score is None:
        return
    if score.input_coverage < hr.min_input_coverage:
        v.rejections.append(
            HardRejection(
                RejectionCode.INSUFFICIENT_COVERAGE,
                "too little of the scoring rubric had live inputs for the result to mean anything",
                round(score.input_coverage, 3),
                hr.min_input_coverage,
            )
        )


def below_minimum_score(score: CompositeScore, cfg: MethodologyConfig) -> HardRejection | None:
    """Score-based cutoff. Deliberately NOT a hard rule.

    Kept separate so the distinction survives into the report: a candidate below
    the ranking threshold merely did not score well enough, while a hard
    rejection means something about it is disqualifying. Conflating them would
    lose the more interesting half of the rejected set.
    """
    if score.score >= cfg.run.min_score_to_rank:
        return None
    return HardRejection(
        RejectionCode.BELOW_MINIMUM_SCORE,
        "score is below the ranking threshold",
        round(score.score, 2),
        cfg.run.min_score_to_rank,
    )
