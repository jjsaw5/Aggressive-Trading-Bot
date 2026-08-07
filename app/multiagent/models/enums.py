"""Enumerations for the multi-agent research pipeline.

Explicit enums everywhere an agent supplies a categorical value. An LLM asked
for free text will invent a taxonomy; an LLM constrained to an enum either picks
a member or fails validation, and a validation failure is visible in
`agent_runs`. That is the whole reason these exist rather than `str`.

Where the existing platform already has an enum for a concept
(`app.domain.enums.Direction`, `StrategyType`, `OptionType`) this module
re-exports it rather than defining a parallel one. Two enums for one concept is
how a mapping bug gets written.
"""

from __future__ import annotations

from enum import Enum

from app.domain.enums import Direction, OptionType, StrategyType

__all__ = [
    "Direction",
    "OptionType",
    "StrategyType",
    "MARKET_STRATEGIES",
    "CatalystScope",
    "CatalystType",
    "EvidenceQuality",
    "EvidenceKind",
    "ExpectedDirection",
    "Importance",
    "TimeHorizon",
    "MarketRegime",
    "VolatilityRegime",
    "BiasDirection",
    "PipelineStage",
    "RunStatus",
    "AgentName",
    "ValidationVerdict",
    "RejectionCode",
    "Classification",
    "CalibrationStatus",
    "DecisionAction",
    "DataQualityFlag",
    "MeasurementStatus",
]


# Strategies this milestone permits. Anything else is rejected by the rules
# engine even if an agent proposes it and even if it scores well.
MARKET_STRATEGIES: frozenset[StrategyType] = frozenset(
    {
        StrategyType.LONG_CALL,
        StrategyType.LONG_PUT,
        StrategyType.BULL_CALL_SPREAD,
        StrategyType.BEAR_PUT_SPREAD,
    }
)


class CatalystScope(str, Enum):
    """Whether a catalyst moves the whole market, a sector, or one name."""

    MARKET = "market"
    SECTOR = "sector"
    COMPANY = "company"


class CatalystType(str, Enum):
    # Macro / policy
    CPI = "cpi"
    PPI = "ppi"
    PCE = "pce"
    GDP = "gdp"
    EMPLOYMENT = "employment"
    JOBLESS_CLAIMS = "jobless_claims"
    RETAIL_SALES = "retail_sales"
    CONSUMER_CONFIDENCE = "consumer_confidence"
    ISM = "ism"
    TREASURY_YIELDS = "treasury_yields"
    DOLLAR = "dollar"
    FOMC = "fomc"
    FED_SPEAKER = "fed_speaker"
    FED_MINUTES = "fed_minutes"
    CENTRAL_BANK_OTHER = "central_bank_other"

    # Company
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    ESTIMATE_REVISION = "estimate_revision"
    ANALYST_RATING = "analyst_rating"
    PRICE_TARGET = "price_target"
    MERGER_ACQUISITION = "merger_acquisition"
    PRODUCT_LAUNCH = "product_launch"
    FDA_DECISION = "fda_decision"
    LITIGATION = "litigation"
    REGULATORY = "regulatory"
    SEC_FILING = "sec_filing"
    EXECUTIVE_CHANGE = "executive_change"
    INVESTOR_DAY = "investor_day"
    CONFERENCE = "conference"
    CONTRACT_AWARD = "contract_award"
    DIVIDEND = "dividend"
    SPLIT = "split"

    # Sector / industry
    SECTOR_ROTATION = "sector_rotation"
    INDUSTRY_DEVELOPMENT = "industry_development"
    COMMODITY_MOVE = "commodity_move"
    SUPPLY_CHAIN = "supply_chain"
    GEOPOLITICAL = "geopolitical"

    # Market structure
    INDEX_REBALANCE = "index_rebalance"
    OPTIONS_EXPIRATION = "options_expiration"
    TECHNICAL_LEVEL = "technical_level"

    OTHER = "other"


class EvidenceKind(str, Enum):
    """What kind of retrieved artifact an evidence item is.

    Every id in the ledger has one of these. An agent citing an id gets its
    claim bound to a real artifact of a known kind.
    """

    NEWS = "news"
    ECONOMIC_EVENT = "economic_event"
    EARNINGS_EVENT = "earnings_event"
    CALENDAR_CATALYST = "calendar_catalyst"
    QUOTE = "quote"
    PRICE_HISTORY = "price_history"
    FUNDAMENTALS = "fundamentals"
    OPTION_CHAIN = "option_chain"
    IV_CONTEXT = "iv_context"
    FLOW_ALERT = "flow_alert"
    MARKET_INTERNALS = "market_internals"


class EvidenceQuality(str, Enum):
    """How firm a claim is. The distinction is load-bearing.

    CONFIRMED_FACT — a scheduled event on a calendar, or a retrieved headline
    from a named source with a publication timestamp.
    REPORTED       — a news item whose substance is a claim by a third party.
    INTERPRETATION — the agent's reading of confirmed material.
    SPECULATION    — the agent's guess. Scores nothing.
    """

    CONFIRMED_FACT = "confirmed_fact"
    REPORTED = "reported"
    INTERPRETATION = "interpretation"
    SPECULATION = "speculation"


class ExpectedDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    VOLATILE = "volatile"      # direction unknown, magnitude expected
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class Importance(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TimeHorizon(str, Enum):
    INTRADAY = "intraday"
    ONE_TO_THREE_DAYS = "1-3d"
    ONE_WEEK = "1w"
    TWO_TO_FOUR_WEEKS = "2-4w"
    ONE_TO_THREE_MONTHS = "1-3m"
    UNKNOWN = "unknown"


class MarketRegime(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    ROTATIONAL = "rotational"
    RANGE_BOUND = "range_bound"
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    UNKNOWN = "unknown"


class VolatilityRegime(str, Enum):
    COMPRESSED = "compressed"
    NORMAL = "normal"
    ELEVATED = "elevated"
    STRESSED = "stressed"
    UNKNOWN = "unknown"


class BiasDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class PipelineStage(str, Enum):
    """Premarket research and market-open validation are different acts.

    Option quotes before the options market opens are stale or absent. A
    contract chosen against them is chosen against a fiction, so the pipeline
    refuses to finalise one until the market-open stage.
    """

    PREMARKET = "premarket"
    MARKET_OPEN = "market_open"
    FULL = "full"  # both stages back to back (what the CLI does by default)


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class AgentName(str, Enum):
    MARKET_INTELLIGENCE = "market_intelligence"
    OPPORTUNITY_GENERATOR = "opportunity_generator"
    TRADE_VALIDATOR = "trade_validator"
    RISK_REVIEWER = "risk_reviewer"


class ValidationVerdict(str, Enum):
    CONFIRMS = "confirms"
    MIXED = "mixed"
    CONTRADICTS = "contradicts"
    INSUFFICIENT_DATA = "insufficient_data"


class RejectionCode(str, Enum):
    """Hard failures. Terminal — no score overrides one."""

    SPREAD_TOO_WIDE = "spread_too_wide"
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    MISSING_CRITICAL_DATA = "missing_critical_data"
    CATALYST_UNVERIFIED = "catalyst_unverified"
    EARNINGS_BLACKOUT = "earnings_blackout"
    PROVIDER_DISAGREEMENT = "provider_disagreement"
    REWARD_RISK_TOO_LOW = "reward_risk_too_low"
    COST_EXCEEDS_RISK_BUDGET = "cost_exceeds_risk_budget"
    EXCESSIVE_THETA = "excessive_theta"
    STRATEGY_NOT_ALLOWED = "strategy_not_allowed"
    NO_VALID_CONTRACT = "no_valid_contract"
    STALE_QUOTE = "stale_quote"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    BELOW_MINIMUM_SCORE = "below_minimum_score"


class Classification(str, Enum):
    EXCEPTIONAL = "EXCEPTIONAL"
    HIGH_CONVICTION = "HIGH_CONVICTION"
    GOOD = "GOOD"
    WATCHLIST = "WATCHLIST"
    REJECT = "REJECT"


class CalibrationStatus(str, Enum):
    """Whether the score has been shown to predict anything.

    `docs/PRODUCT_STANCE.md` is a decision of record: every score displays
    UNCALIBRATED until a feature clears out-of-sample validation, and none has.
    This travels with every score so a number can never be read as a validated
    edge just because it is large.
    """

    UNCALIBRATED = "UNCALIBRATED"
    CALIBRATED = "CALIBRATED"


class DecisionAction(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    WATCHED = "watched"
    ENTERED = "entered"
    SKIPPED = "skipped"


class DataQualityFlag(str, Enum):
    MISSING_FIELD = "missing_field"
    STALE_DATA = "stale_data"
    PROVIDER_DISAGREEMENT = "provider_disagreement"
    PROVIDER_ERROR = "provider_error"
    MODELED_VALUE = "modeled_value"
    UNREFERENCED_AGENT_CLAIM = "unreferenced_agent_claim"
    SCHEMA_VALIDATION_FAILURE = "schema_validation_failure"
    OUT_OF_UNIVERSE_TICKER = "out_of_universe_ticker"


class MeasurementStatus(str, Enum):
    """Why a scoring rule produced the points it did.

    ABSTAINED is the important member. A rule with no input does NOT score zero
    — zero means "measured, and bad". Its weight leaves the denominator instead.
    CLAUDE.md: "Absent stays absent. Never substitute 0.0 for a missing
    measurement."
    """

    MEASURED = "measured"
    ABSTAINED = "abstained"
    NOT_APPLICABLE = "not_applicable"
