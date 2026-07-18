"""Enumerations shared across the domain."""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    """Directional / volatility bias of a thesis."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    VOL_LONG = "vol_long"  # expect volatility expansion (long gamma/vega)
    VOL_SHORT = "vol_short"  # expect volatility contraction (short premium)


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class OptionAction(str, Enum):
    BUY_TO_OPEN = "buy_to_open"
    SELL_TO_OPEN = "sell_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_CLOSE = "sell_to_close"


class StrategyType(str, Enum):
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"
    BULL_PUT_SPREAD = "bull_put_spread"  # credit
    BEAR_CALL_SPREAD = "bear_call_spread"  # credit
    LONG_STRADDLE = "long_straddle"
    LONG_STRANGLE = "long_strangle"
    IRON_CONDOR = "iron_condor"


class CandidateStatus(str, Enum):
    RANKED = "ranked"
    REJECTED = "rejected"
    PROPOSED = "proposed"


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FILLED = "filled"
    CANCELLED = "cancelled"


class PaperTradeStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ExitReason(str, Enum):
    PROFIT_TARGET = "profit_target"
    STOP_LOSS = "stop_loss"
    INVALIDATION = "invalidation"
    TIME_STOP = "time_stop"
    MANUAL = "manual"
    EXPIRY = "expiry"


class RejectReason(str, Enum):
    ILLIQUID_OPTION = "illiquid_option"
    WIDE_SPREAD = "wide_spread"
    LOW_OPEN_INTEREST = "low_open_interest"
    LOW_VOLUME = "low_volume"
    PENNY_STOCK = "penny_stock"
    LOW_FLOAT = "low_float"
    BINARY_EVENT = "binary_event"
    UNRELIABLE_PRICING = "unreliable_pricing"
    RISK_UNMANAGEABLE = "risk_unmanageable"
    NOT_IN_UNIVERSE = "not_in_universe"
    WEAK_SIGNAL = "weak_signal"
    NO_VALID_CONTRACT = "no_valid_contract"
    PORTFOLIO_LIMIT = "portfolio_limit"
    STALE_QUOTE = "stale_quote"
    RESTRICTED_EVENT_WINDOW = "restricted_event_window"
    TIME_OF_DAY_BLOCKED = "time_of_day_blocked"
    DAILY_LOSS_LIMIT = "daily_loss_limit"


class DTECategory(str, Enum):
    """Short-duration bucket. Kept distinct from a raw DTE integer because the
    two categories have different data priorities, scoring, and risk rules."""

    ZERO_DTE = "0dte"
    SHORT_DTE = "1-5dte"


class ShortDurationStrategy(str, Enum):
    """Setup archetypes for the short-duration module (independent modules).

    The strategy names an already-confirmed market setup; the option is only the
    expression. Implemented incrementally — Phase 2 lands the first four."""

    # 0DTE
    OPENING_RANGE_BREAKOUT = "opening_range_breakout"
    VWAP_TREND_CONTINUATION = "vwap_trend_continuation"
    FAILED_BREAKOUT = "failed_breakout"
    NEWS_MOMENTUM = "news_momentum"
    MACRO_EVENT_REACTION = "macro_event_reaction"
    # 1-5DTE
    TREND_CONTINUATION = "trend_continuation"
    BREAKOUT_FOLLOW_THROUGH = "breakout_follow_through"
    PULLBACK_IN_TREND = "pullback_in_trend"
    POST_EARNINGS_CONTINUATION = "post_earnings_continuation"
    POST_EARNINGS_REVERSAL = "post_earnings_reversal"
    CATALYST_CONTINUATION = "catalyst_continuation"
    MULTI_SESSION_FLOW = "multi_session_flow"


class CandidateState(str, Enum):
    """Short-duration candidate lifecycle. Every transition is recorded with
    the previous/new state, trigger, actor, reason, and score-at-transition."""

    DETECTED = "detected"
    EVALUATING = "evaluating"
    WATCHLIST = "watchlist"
    ARMED = "armed"
    TRIGGERED = "triggered"
    PROPOSED = "proposed"
    APPROVED = "approved"
    OPEN = "open"
    MANAGING = "managing"
    CLOSED = "closed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ShortDurationRegime(str, Enum):
    """Intraday market regimes for short-duration decisions."""

    BULL_TREND = "bull_trend"
    BEAR_TREND = "bear_trend"
    RANGE_BOUND = "range_bound"
    HIGH_VOL_TREND = "high_vol_trend"
    HIGH_VOL_CHOP = "high_vol_chop"
    LOW_VOL_COMPRESSION = "low_vol_compression"
    NEWS_DRIVEN = "news_driven"
    MACRO_EVENT_DRIVEN = "macro_event_driven"
    UNSTABLE = "unstable"  # restricted — new trades discouraged/blocked
