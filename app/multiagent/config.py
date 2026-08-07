"""Methodology configuration for the multi-agent research subsystem.

Everything tunable about the methodology lives in `config/methodology.yaml` and
is parsed into the models below. Code reads `get_methodology()`; it never
hardcodes a threshold.

Two separate configuration surfaces, deliberately:

* **Methodology** (this file, YAML) — weights, thresholds, bands. Changing one
  changes what the system recommends, so it belongs in a reviewable file with a
  version string, not in an environment variable someone can flip unnoticed.
* **Runtime** (`app.config.settings`, env) — credentials, provider routing,
  enable flags. Changing one changes where data comes from, not what a score
  means.

The `version` string is stamped onto every run and every persisted score, so a
stored recommendation can always be traced to the methodology that produced it.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _REPO_ROOT / "config" / "methodology.yaml"

# The eight scoring categories, in report order. Named here rather than derived
# from the YAML so a typo in the file fails loudly instead of silently dropping
# a category from the total.
CATEGORY_ORDER: tuple[str, ...] = (
    "catalyst_strength",
    "market_alignment",
    "technical_setup",
    "options_flow",
    "iv_greeks",
    "contract_liquidity",
    "risk_reward",
    "data_quality",
)

TOTAL_POINTS = 100.0


class RunConfig(BaseModel):
    max_candidates: int = 10
    min_score_to_rank: float = 60.0
    max_ranked_in_report: int = 10
    provider_timeout_seconds: float = 25.0
    validation_concurrency: int = 6
    market_reference_symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    context_symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    discovery_universe: list[str] = Field(default_factory=list)


class StrategyConfig(BaseModel):
    allowed: list[str] = Field(default_factory=list)


class ScoringWeights(BaseModel):
    catalyst_strength: float
    market_alignment: float
    technical_setup: float
    options_flow: float
    iv_greeks: float
    contract_liquidity: float
    risk_reward: float
    data_quality: float

    @model_validator(mode="after")
    def _must_total_100(self) -> ScoringWeights:
        total = sum(getattr(self, c) for c in CATEGORY_ORDER)
        if abs(total - TOTAL_POINTS) > 1e-6:
            raise ValueError(
                f"scoring weights must sum to {TOTAL_POINTS}, got {total}. "
                "Every category's rule points are expressed out of its weight, "
                "so a total other than 100 makes the composite uninterpretable."
            )
        return self

    def for_category(self, category: str) -> float:
        return float(getattr(self, category))


class _RuleBlock(BaseModel):
    """Base for per-category rule blocks.

    Permits extra keys so the YAML can carry documentation or forward-looking
    knobs without breaking the loader, but every key the engine reads is
    declared explicitly below so a missing one fails at load rather than at
    scoring time.
    """

    model_config = {"extra": "allow"}


class CatalystRules(_RuleBlock):
    confirmed_scheduled: float
    sourced_news: float
    timing_within_horizon: float
    high_importance: float
    stale_news_penalty: float
    already_priced_in_penalty: float
    max_news_age_days: int
    priced_in_move_pct: float
    corroboration_min_items: float


class MarketAlignmentRules(_RuleBlock):
    spy_aligned: float
    qqq_aligned: float
    sector_aligned: float
    relative_strength: float
    fighting_tape_penalty: float
    relative_strength_lookback_days: int
    trend_flat_threshold_pct: float


class TechnicalRules(_RuleBlock):
    trend_aligned: float
    above_below_key_ma: float
    relative_volume: float
    momentum_confirmation: float
    room_to_target: float
    atr_supports_move: float
    crowded_level_penalty: float
    extended_penalty: float
    rel_volume_strong: float
    momentum_agreement_strong: float
    momentum_agreement_floor: float
    atr_pct_min: float
    atr_pct_max: float
    momentum_lookback_days: int
    crowded_level_atr: float
    extended_atr_multiple: float
    atr_period: int


class FlowRules(_RuleBlock):
    directional_agreement: float
    ask_side_aggression: float
    sweep_presence: float
    size_vs_open_interest: float
    concentration: float
    contradiction_penalty: float
    min_premium_usd: float
    ask_side_share_strong: float
    net_premium_ratio_strong: float
    concentration_strong: float
    concentration_floor: float
    size_over_oi_ratio: float
    lookback_hours: int


class IVGreeksRules(_RuleBlock):
    iv_rank_favorable: float
    term_structure_ok: float
    delta_in_band: float
    theta_tolerable: float
    iv_elevated_penalty: float
    iv_rank_low: float
    iv_rank_high: float
    theta_burden_max: float


class LiquidityRules(_RuleBlock):
    spread_tight: float
    open_interest: float
    volume: float
    spread_pct_excellent: float
    spread_pct_good: float
    open_interest_good: int
    open_interest_ok: int
    volume_good: int
    volume_ok: int


class RiskRewardRules(_RuleBlock):
    reward_to_risk: float
    breakeven_reachable: float
    within_risk_budget: float
    invalidation_defined: float
    rr_excellent: float
    rr_good: float
    rr_minimum: float
    breakeven_over_expected_move_max: float


class DataQualityRules(_RuleBlock):
    providers_agree: float
    data_fresh: float
    full_coverage: float
    price_disagreement_pct: float
    max_quote_age_seconds: int
    coverage_for_bonus: float


class ScoringConfig(BaseModel):
    weights: ScoringWeights
    abstain_is_not_zero: bool = True
    catalyst_strength: CatalystRules
    market_alignment: MarketAlignmentRules
    technical_setup: TechnicalRules
    options_flow: FlowRules
    iv_greeks: IVGreeksRules
    contract_liquidity: LiquidityRules
    risk_reward: RiskRewardRules
    data_quality: DataQualityRules

    def rules_for(self, category: str) -> _RuleBlock:
        return getattr(self, category)


class ClassificationBand(BaseModel):
    min: float
    label: str
    name: str


class ClassificationConfig(BaseModel):
    bands: list[ClassificationBand]

    @model_validator(mode="after")
    def _descending(self) -> ClassificationConfig:
        mins = [b.min for b in self.bands]
        if mins != sorted(mins, reverse=True):
            raise ValueError("classification bands must be listed high to low")
        if not self.bands or self.bands[-1].min > 0.0:
            raise ValueError("the lowest classification band must start at 0.0 so every score classifies")
        return self

    def classify(self, score: float) -> ClassificationBand:
        for band in self.bands:
            if score >= band.min:
                return band
        return self.bands[-1]


class HardRuleConfig(BaseModel):
    max_spread_pct: float
    min_open_interest: int
    min_volume: int
    min_reward_to_risk: float
    max_defined_risk_usd: float
    max_contracts: int
    earnings_blackout_days: int
    earnings_blackout_applies_to_earnings_plays: bool
    max_theta_burden: float
    min_input_coverage: float
    max_provider_price_disagreement_pct: float
    require_catalyst_evidence: bool
    max_quote_age_seconds: int


class ContractConfig(BaseModel):
    preferred_dte_min: int
    preferred_dte_max: int
    long_delta_min: float
    long_delta_max: float
    spread_long_delta_min: float
    spread_long_delta_max: float
    spread_short_delta_min: float
    spread_short_delta_max: float
    spread_widths: list[float]
    spread_width_strike_steps: list[int] = [1, 2, 4]
    max_expirations_considered: int
    max_proposals_per_candidate: int


class EventRiskConfig(BaseModel):
    high_impact_econ_window_hours: int
    fomc_window_hours: int
    warn_only_events: list[str] = Field(default_factory=list)
    blocking_events: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    definitions_dir: str = ".claude/agents"
    market_intelligence: str = "market-intelligence"
    opportunity_generator: str = "opportunity-generator"
    trade_validator: str = "trade-validator"
    risk_reviewer: str = "risk-reviewer"
    max_evidence_items: int = 120
    drop_unreferenced_claims: bool = True


class MethodologyConfig(BaseModel):
    version: str
    run: RunConfig
    strategies: StrategyConfig
    scoring: ScoringConfig
    classification: ClassificationConfig
    hard_rules: HardRuleConfig
    contracts: ContractConfig
    event_risk: EventRiskConfig
    agents: AgentConfig

    # Resolved at load time so callers can report which file produced a score.
    source_path: str = ""

    def definitions_path(self) -> Path:
        return _REPO_ROOT / self.agents.definitions_dir


def load_methodology(path: str | Path | None = None) -> MethodologyConfig:
    """Parse a methodology file. Raises on anything malformed — never guesses."""
    target = Path(path) if path else _DEFAULT_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"methodology config not found at {target}. This file is required: the "
            "scoring engine has no built-in defaults by design."
        )
    raw: dict[str, Any] = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    cfg = MethodologyConfig.model_validate(raw)
    return cfg.model_copy(update={"source_path": str(target)})


@functools.lru_cache(maxsize=4)
def _cached(path: str | None) -> MethodologyConfig:
    return load_methodology(path)


def get_methodology(path: str | Path | None = None) -> MethodologyConfig:
    """Cached accessor. Pass a path in tests to load an alternate methodology."""
    return _cached(str(path) if path else None)


def clear_cache() -> None:
    _cached.cache_clear()
