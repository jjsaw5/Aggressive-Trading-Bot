"""Decision-warehouse models: frozen decisions and their realized outcomes.

The learning loop has two records:

- `DecisionSnapshot` — captured the moment an actionable decision is made. It
  freezes the market state (spot, IV, IV rank) and the *prediction* (composite
  score, probability of profit, breakevens, expected value) so nothing can be
  revised after the fact. Traceability first: what did we believe, and when?

- `DecisionOutcome` — recorded later, once the underlying has moved (or a paper
  trade closed). It is the ground truth we score predictions against. A single
  decision can have several outcomes (e.g. a 21-day check and an at-expiry
  check), each labeled by horizon.

Together they let the platform grade its own suggestions — direction accuracy,
POP calibration, and whether the composite score is actually predictive.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field

from app.domain.enums import Direction, StrategyType
from app.domain.trades import TradePlan


class DecisionSource(str, Enum):
    """Which surface produced the decision that was warehoused."""

    SCAN = "scan"  # an actionable research candidate
    PAPER = "paper"  # a simulated position was opened
    PROPOSAL = "proposal"  # a human-approval order ticket was created


class OutcomeResult(str, Enum):
    WIN = "win"
    LOSS = "loss"
    SCRATCH = "scratch"  # finished ~at breakeven
    UNKNOWN = "unknown"  # cannot be determined from available data


class DecisionSnapshot(BaseModel):
    """A frozen, point-in-time record of one actionable decision."""

    decision_id: str
    scan_id: str
    symbol: str
    source: DecisionSource = DecisionSource.SCAN
    direction: Direction
    strategy: StrategyType
    generated_at: datetime

    # --- Prediction (what we believed would happen) ---
    composite_score: float = Field(ge=0.0, le=1.0)
    probability_of_profit: float | None = None  # risk-neutral POP at plan time
    reward_to_risk: float | None = None
    expected_value_usd: float | None = None
    breakevens: list[float] = Field(default_factory=list)
    is_credit: bool = False

    # --- Frozen market state at decision time ---
    entry_spot: float
    entry_iv: float | None = None
    iv_rank: float | None = None

    # --- Structure economics ---
    entry_net_per_share: float  # debit > 0, credit < 0
    max_profit_usd: float | None = None
    max_loss_usd: float
    contracts: int = Field(ge=1)
    expiration: date | None = None
    dte_at_entry: int | None = None

    # Full plan for faithful replay / audit.
    trade_plan: TradePlan


class DecisionOutcome(BaseModel):
    """The realized result of a decision at a given horizon (ground truth)."""

    decision_id: str
    symbol: str
    horizon_label: str  # "21d" | "expiry" | "trade_close" | free-form
    resolved_at: datetime
    elapsed_days: int | None = None

    # Underlying move (valid at any horizon)
    spot_at_resolution: float | None = None
    underlying_return_pct: float | None = None
    direction_correct: bool | None = None  # None for non-directional structures

    # Trade result
    result: OutcomeResult = OutcomeResult.UNKNOWN
    realized_pnl_usd: float | None = None

    # How the result was determined, for honesty about the proxy used.
    outcome_source: str = "underlying_vs_breakeven"  # or "paper_trade"
    note: str = ""
