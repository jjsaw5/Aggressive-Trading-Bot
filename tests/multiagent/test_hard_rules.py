"""Hard rejection rules — terminal, and blind to the score.

The requirement being tested: *"Do not allow a strong score in one area to
override a critical hard-risk failure."* The structural guarantee is that
`evaluate_hard_rules` never reads the composite score (only its coverage), so a
90 and a 40 with identical measurements produce identical rejections.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.enums import OptionAction, OptionType, StrategyType
from app.domain.options import Greeks
from app.multiagent.models.contracts import ProposedLeg, ProposedStructure
from app.multiagent.models.enums import CalibrationStatus, RejectionCode
from app.multiagent.models.measurements import Measurement, Provenance
from app.multiagent.models.scoring import CompositeScore, ScoreComponent
from app.multiagent.models.validation import (
    CatalystValidation,
    RiskRewardSnapshot,
    ValidationReport,
)
from app.multiagent.rules import below_minimum_score, evaluate_hard_rules
from app.multiagent.scoring.rules import threshold_rule

NOW = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)


def _leg(strike: float, action: OptionAction, *, bid=2.0, ask=2.1, oi=1000, vol=500) -> ProposedLeg:
    return ProposedLeg(
        underlying="NVDA",
        expiration=date(2026, 9, 4),
        strike=strike,
        option_type=OptionType.CALL,
        action=action,
        bid=bid,
        ask=ask,
        mark=(bid + ask) / 2,
        volume=vol,
        open_interest=oi,
        implied_volatility=0.35,
        greeks=Greeks(delta=0.45, gamma=0.02, theta=-0.05, vega=0.11),
        as_of=NOW,
        source="test",
    )


def _structure(**overrides) -> ProposedStructure:
    base = {
        "structure_id": "s1",
        "candidate_id": "cand-1",
        "run_id": "test-run",
        "ticker": "NVDA",
        "strategy_type": StrategyType.BULL_CALL_SPREAD,
        "legs": [
            _leg(100.0, OptionAction.BUY_TO_OPEN, bid=3.0, ask=3.1),
            _leg(105.0, OptionAction.SELL_TO_OPEN, bid=2.3, ask=2.4),
        ],
        "underlying_price": 100.0,
        "underlying_as_of": NOW,
        "net_debit_per_share": 0.7,
        "net_debit_at_ask_per_share": 0.8,
        "contracts": 1,
        "max_loss_per_contract": 70.0,
        "max_profit_per_contract": 430.0,
        "breakeven": 100.7,
        "width": 5.0,
        "selected_at": NOW,
    }
    base.update(overrides)
    return ProposedStructure(**base)


def _report(**overrides) -> ValidationReport:
    report = ValidationReport(
        candidate_id="cand-1",
        run_id="test-run",
        ticker="NVDA",
        validated_at=NOW,
        catalyst=CatalystValidation(
            ticker="NVDA",
            as_of=NOW,
            claimed_catalyst="c",
            exists=True,
            resolved_evidence_ids=["news-abc"],
        ),
        risk_reward=RiskRewardSnapshot(structure_id="s1", as_of=NOW, reward_to_risk=6.1),
    )
    for key, value in overrides.items():
        setattr(report, key, value)
    return report


def _score(value: float, coverage_weight: float = 100.0) -> CompositeScore:
    """A score with a chosen value and full coverage."""
    comp = ScoreComponent(
        category="c",
        weight=coverage_weight,
        rules=[
            threshold_rule(
                "r",
                "d",
                Measurement.of("x", value, provenance=Provenance.DERIVED),
                points=coverage_weight,
                threshold=value,
            )
        ],
    )
    return CompositeScore(
        candidate_id="cand-1",
        run_id="test-run",
        methodology_version="v",
        scored_at=NOW,
        components=[comp],
        calibration_status=CalibrationStatus.UNCALIBRATED,
    )


def test_a_clean_candidate_is_not_rejected(candidate, methodology):
    verdict = evaluate_hard_rules(
        candidate, _report(), _structure(), methodology, now=NOW, score=_score(90.0)
    )
    assert not verdict.rejected, verdict.reasons()


def test_a_wide_spread_is_terminal(candidate, methodology):
    wide = _structure(
        legs=[
            _leg(100.0, OptionAction.BUY_TO_OPEN, bid=2.0, ask=3.0),
            _leg(105.0, OptionAction.SELL_TO_OPEN, bid=1.0, ask=1.05),
        ]
    )
    verdict = evaluate_hard_rules(candidate, _report(), wide, methodology, now=NOW, score=_score(95.0))
    assert RejectionCode.SPREAD_TOO_WIDE.value in verdict.codes()


def test_a_perfect_score_cannot_override_a_hard_rejection(candidate, methodology):
    """The requirement, stated directly."""
    over_budget = _structure(max_loss_per_contract=900.0, contracts=0)
    high = evaluate_hard_rules(
        candidate, _report(), over_budget, methodology, now=NOW, score=_score(100.0)
    )
    low = evaluate_hard_rules(
        candidate, _report(), over_budget, methodology, now=NOW, score=_score(10.0)
    )
    assert high.rejected and low.rejected
    # Identical measurements -> identical rejections, whatever the score was.
    assert high.codes() == low.codes()
    assert RejectionCode.COST_EXCEEDS_RISK_BUDGET.value in high.codes()


def test_thin_open_interest_is_terminal(candidate, methodology):
    thin = _structure(
        legs=[
            _leg(100.0, OptionAction.BUY_TO_OPEN, oi=5),
            _leg(105.0, OptionAction.SELL_TO_OPEN, oi=5),
        ]
    )
    verdict = evaluate_hard_rules(candidate, _report(), thin, methodology, now=NOW, score=_score(80.0))
    assert RejectionCode.INSUFFICIENT_LIQUIDITY.value in verdict.codes()


def test_a_missing_two_sided_market_is_missing_data_not_a_tight_spread(candidate, methodology):
    """Unknown is not tight. The diagnosis must say so."""
    no_market = _structure(
        legs=[
            ProposedLeg(
                underlying="NVDA",
                expiration=date(2026, 9, 4),
                strike=100.0,
                option_type=OptionType.CALL,
                action=OptionAction.BUY_TO_OPEN,
                bid=None,
                ask=None,
                volume=500,
                open_interest=1000,
                as_of=NOW,
                source="test",
            )
        ]
    )
    verdict = evaluate_hard_rules(
        candidate, _report(), no_market, methodology, now=NOW, score=_score(80.0)
    )
    assert RejectionCode.MISSING_CRITICAL_DATA.value in verdict.codes()
    assert RejectionCode.SPREAD_TOO_WIDE.value not in verdict.codes()


def test_an_unverifiable_catalyst_is_terminal(candidate, methodology):
    unverified = _report(
        catalyst=CatalystValidation(
            ticker="NVDA", as_of=NOW, claimed_catalyst="c", exists=False, resolved_evidence_ids=[]
        )
    )
    verdict = evaluate_hard_rules(
        candidate, unverified, _structure(), methodology, now=NOW, score=_score(95.0)
    )
    assert RejectionCode.CATALYST_UNVERIFIED.value in verdict.codes()


def test_low_reward_to_risk_is_terminal(candidate, methodology):
    poor = _report(
        risk_reward=RiskRewardSnapshot(structure_id="s1", as_of=NOW, reward_to_risk=0.4)
    )
    verdict = evaluate_hard_rules(candidate, poor, _structure(), methodology, now=NOW, score=_score(88.0))
    assert RejectionCode.REWARD_RISK_TOO_LOW.value in verdict.codes()


def test_an_uncomputable_reward_to_risk_warns_rather_than_passing_silently(candidate, methodology):
    unknown = _report(risk_reward=RiskRewardSnapshot(structure_id="s1", as_of=NOW))
    verdict = evaluate_hard_rules(
        candidate, unknown, _structure(), methodology, now=NOW, score=_score(80.0)
    )
    assert RejectionCode.REWARD_RISK_TOO_LOW.value not in verdict.codes()
    assert any("uncomputable" in w for w in verdict.warnings)


def test_excessive_theta_is_terminal(candidate, methodology):
    heavy = _report(
        risk_reward=RiskRewardSnapshot(
            structure_id="s1", as_of=NOW, reward_to_risk=6.0, theta_burden=0.95
        )
    )
    verdict = evaluate_hard_rules(candidate, heavy, _structure(), methodology, now=NOW, score=_score(80.0))
    assert RejectionCode.EXCESSIVE_THETA.value in verdict.codes()


def test_a_stale_quote_is_terminal(candidate, methodology):
    old = _structure(underlying_as_of=NOW - timedelta(hours=5))
    verdict = evaluate_hard_rules(candidate, _report(), old, methodology, now=NOW, score=_score(90.0))
    assert RejectionCode.STALE_QUOTE.value in verdict.codes()


def test_earnings_inside_the_blackout_is_terminal(candidate, methodology):
    verdict = evaluate_hard_rules(
        candidate,
        _report(),
        _structure(),
        methodology,
        now=NOW,
        earnings_date=NOW.date() + timedelta(days=1),
        score=_score(92.0),
    )
    assert RejectionCode.EARNINGS_BLACKOUT.value in verdict.codes()


def test_earnings_outside_the_blackout_is_fine(candidate, methodology):
    verdict = evaluate_hard_rules(
        candidate,
        _report(),
        _structure(),
        methodology,
        now=NOW,
        earnings_date=NOW.date() + timedelta(days=20),
        score=_score(92.0),
    )
    assert RejectionCode.EARNINGS_BLACKOUT.value not in verdict.codes()


def test_an_earnings_play_is_waived_by_configuration_but_warned_about(candidate, methodology):
    earnings_day = NOW.date() + timedelta(days=1)
    candidate.earnings_date = earnings_day
    verdict = evaluate_hard_rules(
        candidate,
        _report(),
        _structure(),
        methodology,
        now=NOW,
        earnings_date=earnings_day,
        score=_score(90.0),
    )
    assert RejectionCode.EARNINGS_BLACKOUT.value not in verdict.codes()
    assert any("IV crush" in w for w in verdict.warnings)


def test_irreconcilable_provider_disagreement_is_terminal(candidate, methodology):
    verdict = evaluate_hard_rules(
        candidate,
        _report(),
        _structure(),
        methodology,
        now=NOW,
        provider_price_disagreement_pct=9.0,
        score=_score(90.0),
    )
    assert RejectionCode.PROVIDER_DISAGREEMENT.value in verdict.codes()


def test_a_forbidden_strategy_is_terminal(candidate, methodology):
    candidate.strategy_type = StrategyType.IRON_CONDOR
    verdict = evaluate_hard_rules(
        candidate, _report(), _structure(), methodology, now=NOW, score=_score(99.0)
    )
    assert RejectionCode.STRATEGY_NOT_ALLOWED.value in verdict.codes()


def test_no_structure_at_all_is_terminal(candidate, methodology):
    verdict = evaluate_hard_rules(candidate, _report(), None, methodology, now=NOW, score=_score(90.0))
    assert RejectionCode.NO_VALID_CONTRACT.value in verdict.codes()


def test_thin_coverage_rejects_however_high_the_score(candidate, methodology):
    """A 95 computed from a quarter of the rubric is the case this rule exists for."""
    sparse = CompositeScore(
        candidate_id="cand-1",
        run_id="test-run",
        methodology_version="v",
        scored_at=NOW,
        components=[
            ScoreComponent(
                category="live",
                weight=20.0,
                rules=[
                    threshold_rule(
                        "r", "d", Measurement.of("x", 5.0, provenance=Provenance.DERIVED),
                        points=20.0, threshold=1.0,
                    )
                ],
            ),
            ScoreComponent(
                category="dead",
                weight=80.0,
                rules=[
                    threshold_rule(
                        "r2", "d",
                        Measurement.absent("y"),
                        points=80.0, threshold=1.0,
                    )
                ],
            ),
        ],
    )
    assert sparse.score == 100.0  # everything measurable was earned
    assert sparse.input_coverage == 0.2

    verdict = evaluate_hard_rules(
        candidate, _report(), _structure(), methodology, now=NOW, score=sparse
    )
    assert RejectionCode.INSUFFICIENT_COVERAGE.value in verdict.codes()


def test_all_failing_rules_are_reported_not_just_the_first(candidate, methodology):
    """A report saying 'three things are wrong' beats fixing one at a time."""
    bad = _structure(
        legs=[
            _leg(100.0, OptionAction.BUY_TO_OPEN, bid=1.0, ask=3.0, oi=2, vol=1),
            _leg(105.0, OptionAction.SELL_TO_OPEN, bid=0.5, ask=0.6, oi=2, vol=1),
        ],
        max_loss_per_contract=5000.0,
        contracts=0,
        underlying_as_of=NOW - timedelta(days=1),
    )
    verdict = evaluate_hard_rules(candidate, _report(), bad, methodology, now=NOW, score=_score(50.0))
    codes = set(verdict.codes())
    assert {
        RejectionCode.SPREAD_TOO_WIDE.value,
        RejectionCode.INSUFFICIENT_LIQUIDITY.value,
        RejectionCode.COST_EXCEEDS_RISK_BUDGET.value,
        RejectionCode.STALE_QUOTE.value,
    } <= codes


def test_below_minimum_score_is_a_separate_kind_of_rejection(methodology):
    """Low score and disqualified are different findings and stay separate.

    Note the composites are built explicitly here rather than via `_score`:
    that helper sets threshold == value so the rule always passes, which is what
    the hard-rule tests want (a valid score object of no particular value) but
    would make this test assert nothing.
    """
    comp = ScoreComponent(
        category="c",
        weight=100.0,
        rules=[
            threshold_rule(
                "r", "d", Measurement.of("x", 0.0, provenance=Provenance.DERIVED),
                points=100.0, threshold=50.0,
            )
        ],
    )
    really_low = CompositeScore(
        candidate_id="c", run_id="r", methodology_version="v", scored_at=NOW, components=[comp]
    )
    assert really_low.score == 0.0
    assert below_minimum_score(really_low, methodology) is not None

    high_comp = ScoreComponent(
        category="c",
        weight=100.0,
        rules=[
            threshold_rule(
                "r", "d", Measurement.of("x", 100.0, provenance=Provenance.DERIVED),
                points=100.0, threshold=50.0,
            )
        ],
    )
    high = CompositeScore(
        candidate_id="c", run_id="r", methodology_version="v", scored_at=NOW, components=[high_comp]
    )
    assert below_minimum_score(high, methodology) is None


def test_the_risk_cap_matches_the_documented_policy(methodology):
    """docs/RISK_POLICY.md: $100 max defined risk per trade, 20 contracts."""
    from app.config import settings

    assert methodology.hard_rules.max_defined_risk_usd == settings.max_defined_risk_per_trade_usd
    assert methodology.hard_rules.max_contracts == settings.max_contracts_per_trade


@pytest.mark.parametrize("code", list(RejectionCode))
def test_every_rejection_code_renders_readably(code):
    from app.multiagent.rules import HardRejection

    rendered = HardRejection(code, "because reasons", 1.0, 2.0).render()
    assert code.value in rendered
    assert "because reasons" in rendered
