"""Build the frozen `MarketContext` for a decision, from what the scan already read.

Phase 1 of the remediation directive. Almost nothing here is a new measurement:
the scanner already fetches the chain, the IV context and the earnings date, and
already computes cost drag. The audit's finding was that all of it was thrown
away between the scan and the warehouse. This module is the join.

The two genuinely new computations are Greeks (Black-Scholes, because no provider
in the stack supplies them) and the regime tag.

**Nothing built here may be read by a scoring component.** See
`docs/CAPTURE_WINDOW_PREREGISTRATION.md` §2 — data persistence is permitted
during the capture window *because* it does not change what the scorer computes.
"""

from __future__ import annotations

import math
import subprocess
from datetime import date, datetime
from functools import lru_cache

from app.domain.enums import OptionAction, OptionType
from app.domain.market_context import GREEKS_MODELED, LegQuote, MarketContext
from app.logging_config import get_logger
from app.quant.pricing import black_scholes_greeks

log = get_logger(__name__)

# --- Regime thresholds (item 1.11) -------------------------------------------
# Fixed here, once. Three incompatible volatility classifiers already existed in
# this codebase when Phase 1 started:
#
#   analytics/calibration.py::_vol_regime   cheap/fair/rich/extreme @ .25/.50/.70
#   backtest/real_mark_seed.py::vol_regime  low/mid/high            @ .35/IV_HIGH
#   events/detectors.py::classify_...       low_vol/mid_vol/high_vol@ .33/.66
#
# Only the first feeds the conviction gate's `per_regime` criterion. The
# pre-registration §3(b) requires ">=2 distinct regime tags per the regime
# pipeline" — a phrase with no single referent while three pipelines disagree.
# This is that referent. The existing three are left alone (calibration's is
# load-bearing for the gate and changing it would move the gate); this tag is
# recorded alongside them and reconciling the four is a Phase 3 question.
_VOL_LOW = 0.30
_VOL_HIGH = 0.70

# Tape thresholds: the underlying's own position against its 20-day mean, in
# realized-vol units, so "strong" means the same thing on SPY as on NVDA.
_TAPE_Z = 0.5


def _cent(x: float | None) -> float | None:
    return round(x, 4) if x is not None else None


@lru_cache(maxsize=1)
def build_sha() -> str:
    """Git SHA of the running build (item 1.10).

    Cached for process lifetime: it cannot change without a restart. Returns
    `""` when git is unavailable (a container without the .git directory), never
    a placeholder that could be mistaken for a real commit.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env
        log.warning("build_sha_unavailable", error=str(exc))
        return ""
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else ""


def vol_regime(iv_rank: float | None) -> str:
    """Volatility half of the regime tag. Accepts 0-1 or 0-100."""
    if iv_rank is None:
        return "unknown"
    r = iv_rank if iv_rank <= 1.0 else iv_rank / 100.0
    if r < _VOL_LOW:
        return "lowvol"
    if r >= _VOL_HIGH:
        return "highvol"
    return "midvol"


def tape_regime(closes: list[float] | None) -> str:
    """Directional half of the regime tag, from the underlying's own daily closes.

    The audit's central unanswerable question was whether four losing sessions
    reflected a bad model or a bad week — 52 of 67 signals were bearish into a
    strengthening tape. A volatility label cannot separate those. This one can,
    because it records which way the tape was actually going.

    Measured as the last close's distance from its 20-day mean, scaled by the
    20-day standard deviation of closes. Needs 20 closes; fewer is `unknown`
    rather than a short-window estimate wearing the same label.
    """
    if not closes or len(closes) < 20:
        return "unknown"
    window = closes[-20:]
    mean = sum(window) / len(window)
    var = sum((c - mean) ** 2 for c in window) / len(window)
    sd = math.sqrt(var)
    if sd <= 0:
        return "flat"
    z = (window[-1] - mean) / sd
    if z >= _TAPE_Z:
        return "uptape"
    if z <= -_TAPE_Z:
        return "downtape"
    return "flat"


def regime_tag(iv_rank: float | None, closes: list[float] | None) -> tuple[str, str, str]:
    """The canonical composite tag: `(tag, vol, tape)`.

    Composite because either half alone is degenerate for the pre-registration's
    per-regime requirement — a corpus can span two IV-rank buckets and still be
    one continuous bull tape.
    """
    v = vol_regime(iv_rank)
    t = tape_regime(closes)
    if v == "unknown" and t == "unknown":
        return "unknown", v, t
    return f"{v}/{t}", v, t


def _signed_quantity(leg) -> int:
    sign = 1 if leg.action in (OptionAction.BUY_TO_OPEN, OptionAction.BUY_TO_CLOSE) else -1
    return sign * int(leg.quantity or 0)


def _leg_quote(leg, contract, *, spot: float | None, now: datetime, source: str) -> LegQuote:
    """One leg's frozen market state. Every field degrades to None on its own."""
    bid = getattr(contract, "bid", None) if contract is not None else None
    ask = getattr(contract, "ask", None) if contract is not None else None
    iv = getattr(contract, "implied_volatility", None) if contract is not None else None

    mid = spread = spread_pct = None
    if bid is not None and ask is not None and ask >= bid:
        mid = round((bid + ask) / 2.0, 4)
        spread = round(ask - bid, 4)
        # A zero mid would make the percentage meaningless, not infinite.
        spread_pct = round(spread / mid, 4) if mid > 0 else None

    # Greeks: modeled, never claimed as measured. Needs spot, IV and a positive
    # time to expiry; any missing input leaves all four None rather than
    # substituting a default vol.
    d = g = t = v = None
    dte_years = (leg.expiration - now.date()).days / 365.0
    if spot and iv and iv > 0 and dte_years > 0:
        try:
            gk = black_scholes_greeks(
                spot, leg.strike, dte_years, iv,
                OptionType(leg.option_type),
            )
            d, g, t, v = (
                _cent(gk["delta"]), _cent(gk["gamma"]),
                _cent(gk["theta"]), _cent(gk["vega"]),
            )
        except (ValueError, ZeroDivisionError, OverflowError) as exc:
            log.warning("greeks_model_failed", strike=leg.strike, error=str(exc))

    return LegQuote(
        strike=leg.strike,
        option_type=leg.option_type,
        expiration=leg.expiration,
        signed_quantity=_signed_quantity(leg),
        bid=_cent(bid), ask=_cent(ask), mid=mid,
        spread=spread, spread_pct_of_mid=spread_pct,
        volume=getattr(contract, "volume", None) if contract is not None else None,
        open_interest=(
            getattr(contract, "open_interest", None) if contract is not None else None
        ),
        implied_volatility=_cent(iv),
        delta=d, gamma=g, theta=t, vega=v,
        greeks_source=GREEKS_MODELED if d is not None else "",
        quote_source=source,
    )


def implied_move(spot: float | None, iv: float | None, dte: int | None) -> tuple[float | None, float | None]:
    """(fraction of spot, dollars) expected move to expiry: iv * sqrt(dte/365).

    Uses the TRADED expiry's IV when the caller supplies it — a 30-day IV applied
    to a 2-day horizon overstates the move badly, which is the same horizon error
    the POP construct was fixed for.
    """
    if not spot or not iv or iv <= 0 or dte is None or dte < 0:
        return None, None
    pct = iv * math.sqrt(max(dte, 0) / 365.0)
    return round(pct, 4), round(pct * spot, 4)


def build_market_context(
    *,
    plan,
    chain,
    iv_context,
    spot: float | None,
    now: datetime,
    next_earnings: date | None = None,
    daily_closes: list[float] | None = None,
    cost_drag_ratio: float | None = None,
    traded_expiry_iv: float | None = None,
    scoring_model_version: str = "",
) -> MarketContext:
    """Freeze the market state this decision was made in.

    Pure: no I/O, no provider calls. Everything is read from objects the scan
    already fetched, so the record reflects exactly what the engine saw. Missing
    inputs produce missing fields — never defaults.
    """
    source = getattr(chain, "source", "") if chain is not None else ""

    legs: list[LegQuote] = []
    if plan is not None and plan.legs:
        by_key = {}
        if chain is not None and getattr(chain, "contracts", None):
            by_key = {
                (round(c.strike, 4), c.option_type, c.expiration): c
                for c in chain.contracts
            }
        for lg in plan.legs:
            contract = by_key.get((round(lg.strike, 4), lg.option_type, lg.expiration))
            legs.append(_leg_quote(lg, contract, spot=spot, now=now, source=source))

    # Round-trip cost in dollars, so the ratio's denominator is auditable. Only
    # computed when EVERY leg has a real book — a partial sum understates the tax.
    round_trip = None
    if legs and all(lg.two_sided for lg in legs):
        half = sum((lg.spread or 0.0) / 2.0 for lg in legs)
        contracts = getattr(plan, "contracts", 1) or 1
        round_trip = round(half * 2.0 * 100.0 * contracts, 2)

    iv30 = getattr(iv_context, "iv30", None) if iv_context is not None else None
    hv20 = getattr(iv_context, "hv20", None) if iv_context is not None else None
    vrp_pts = round(iv30 - hv20, 4) if iv30 is not None and hv20 else None
    vrp_ratio = round(iv30 / hv20, 4) if iv30 is not None and hv20 else None

    expiration = min((lg.expiration for lg in plan.legs), default=None) if plan else None
    dte = (expiration - now.date()).days if expiration else None
    # Prefer the traded expiry's own IV; fall back to iv30 only if that is all
    # there is, and the horizon error is then visible in the dte column.
    move_pct, move_usd = implied_move(spot, traded_expiry_iv or iv30, dte)

    earn_days = (next_earnings - now.date()).days if next_earnings else None
    tag, rvol, rtape = regime_tag(
        getattr(iv_context, "iv_rank", None) if iv_context is not None else None,
        daily_closes,
    )

    return MarketContext(
        legs=legs,
        cost_drag_ratio=cost_drag_ratio,
        round_trip_cost_usd=round_trip,
        iv30=_cent(iv30),
        iv_rank=_cent(getattr(iv_context, "iv_rank", None) if iv_context else None),
        iv_percentile=_cent(
            getattr(iv_context, "iv_percentile", None) if iv_context else None
        ),
        iv_rank_source=(
            getattr(iv_context, "iv_rank_source", None) if iv_context else None
        ),
        term_structure_slope=_cent(
            getattr(iv_context, "term_structure_slope", None) if iv_context else None
        ),
        iv_skew=_cent(getattr(iv_context, "iv_skew", None) if iv_context else None),
        implied_move_pct=move_pct,
        implied_move_usd=move_usd,
        earnings_date=next_earnings,
        earnings_days_away=earn_days,
        realized_vol_20d=_cent(hv20),
        vrp_points=vrp_pts,
        vrp_ratio=vrp_ratio,
        signal_build_sha=build_sha(),
        scoring_model_version=scoring_model_version,
        chain_source=source,
        regime_tag=tag,
        regime_vol=rvol,
        regime_tape=rtape,
    )
