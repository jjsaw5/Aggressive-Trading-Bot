"""The market state a decision was made in, frozen at decision time.

Phase 1 of the remediation directive. The audit of build `7afa098` found that
the scanner *computed* NBBO, IV term structure, Greeks, open interest and cost
drag on every scan and then **discarded all of it** — the warehouse kept a score
and an outcome with no market context between them. That makes the corpus
unanalysable after the fact: you cannot ask "did it lose because the spread ate
it, or because the direction was wrong?" of a row that never recorded the
spread.

Three rules govern everything in this module:

1. **Absent stays absent.** Every field is nullable and every builder returns
   `None` rather than a plausible number. B1 was a required float plus an
   `or 0.0` fallback, which reported a spot price of zero on 67 of 67 signals.
2. **Modeled is labeled.** Greeks here are OUR Black-Scholes computation — no
   provider in the stack supplies them (`unusual_whales/client.py`: "UW does not
   supply greeks"). `greeks_source` says so on every leg, so a modeled number can
   never be mistaken for a measured one.
3. **Recorded, never scored.** Nothing in this record may enter the composite
   score. `docs/CAPTURE_WINDOW_PREREGISTRATION.md` §2 freezes
   `sd-scoring-2026.07-v3` for the capture window and permits data persistence
   precisely *because* persistence does not change what the scorer computes.
   Adding any of these fields to a scoring component would break that freeze.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.domain.enums import OptionType

# Greeks provenance. There is exactly one source today and it is a model, not a
# feed; the constant exists so the export column is never a free-form string.
GREEKS_MODELED = "black_scholes_modeled"


class LegQuote(BaseModel):
    """One leg's market state at the moment the structure was priced.

    Directive items 1.1 (NBBO), 1.7 (volume/OI) and 1.8 (Greeks). Keyed by the
    contract's own identity so a leg can be re-joined to a chain later without
    trusting list order.
    """

    strike: float
    option_type: OptionType
    expiration: date
    # Signed position: +1 long, -1 short, scaled by quantity. Lets the cost and
    # Greek aggregation run without re-deriving intent from the action enum.
    signed_quantity: int = 0

    # --- 1.1 NBBO, exactly as quoted ---
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    # ask - bid. Kept alongside mid so a one-sided or crossed book stays visible
    # rather than being smoothed into a midpoint.
    spread: float | None = None
    spread_pct_of_mid: float | None = None

    # --- 1.7 depth ---
    volume: int | None = None
    open_interest: int | None = None

    # --- 1.3 per-leg vol ---
    implied_volatility: float | None = None

    # --- 1.8 Greeks (MODELED — see module docstring) ---
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    greeks_source: str = ""

    # Which provider the quote came from, so a mixed-source corpus stays legible.
    quote_source: str = ""

    @property
    def two_sided(self) -> bool:
        """A real, uncrossed book. The precondition for trusting mid."""
        return (
            self.bid is not None
            and self.ask is not None
            and self.ask >= self.bid
            and self.ask > 0
        )


class MarketContext(BaseModel):
    """Everything about the market at decision time that grading later needs.

    Frozen onto the decision alongside the prediction. Immutable by convention:
    `DecisionSnapshot` is never rewritten after the fact (see docs/OUTCOMES.md).
    """

    # --- 1.1 / 1.7 / 1.8 ---
    legs: list[LegQuote] = Field(default_factory=list)

    # --- 1.2 cost drag: round-trip spread tax as a share of defined max loss ---
    # Already computed at scan time by detection._apply_cost_drag and, until now,
    # dropped on the floor between the recommendation and the warehouse.
    cost_drag_ratio: float | None = None
    # The same tax in dollars, so the ratio's denominator is auditable.
    round_trip_cost_usd: float | None = None

    # --- 1.3 volatility state ---
    iv30: float | None = None
    iv_rank: float | None = None
    iv_percentile: float | None = None
    # "iv_history" (true rank) | "hv_proxy" | "provider". A rank computed from a
    # realized-vol proxy is not the same measurement as one from real IV history,
    # and pooling them would silently mix two constructs.
    iv_rank_source: str | None = None
    term_structure_slope: float | None = None
    iv_skew: float | None = None
    # Expected move to the traded expiry as a fraction of spot, from the traded
    # expiry's own IV: iv * sqrt(dte/365). None when either input is missing.
    implied_move_pct: float | None = None
    implied_move_usd: float | None = None

    # --- 1.6 event distance ---
    earnings_date: date | None = None
    # Calendar days from decision to the next report. NEGATIVE is meaningful (the
    # report already happened); it is not clamped.
    earnings_days_away: int | None = None

    # --- 1.9 realized vol and the variance risk premium ---
    realized_vol_20d: float | None = None
    # Two conventions, both named, because "VRP" is ambiguous in the literature
    # and a single unlabeled column would be unusable at analysis time.
    vrp_points: float | None = None  # iv30 - hv20, in vol points
    vrp_ratio: float | None = None  # iv30 / hv20

    # --- 1.10 provenance ---
    # Git SHA of the build that produced this signal. Without it a corpus spanning
    # a code change cannot be split at the change.
    signal_build_sha: str = ""
    scoring_model_version: str = ""
    # Provider that supplied the option chain this context was read from.
    chain_source: str = ""

    # --- 1.11 regime ---
    # THE canonical tag (see app/analytics/market_context.regime_tag). Composite
    # by design: a volatility label alone cannot distinguish "a bad model" from
    # "a bad week", which is the exact ambiguity the audit could not resolve.
    regime_tag: str = "unknown"
    regime_vol: str = "unknown"
    regime_tape: str = "unknown"

    @property
    def has_full_nbbo(self) -> bool:
        """Every leg has a real two-sided quote. The precondition for a cost
        figure that means anything."""
        return bool(self.legs) and all(lg.two_sided for lg in self.legs)

    @property
    def net_delta(self) -> float | None:
        """Structure delta per contract, from the modeled per-leg deltas.

        None if ANY leg is unpriced — a partial sum would understate the
        structure's exposure while looking like a complete measurement.
        """
        if not self.legs or any(lg.delta is None for lg in self.legs):
            return None
        return round(
            sum((lg.delta or 0.0) * lg.signed_quantity for lg in self.legs), 4
        )
