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
