"""Short-duration risk controls.

A tighter, DTE-specific risk policy plus the entry gates that decide whether a
scored, sized candidate may actually be entered right now: time-of-day windows,
the 0DTE cutoff, event/regime blackout, stale data, daily-loss and
consecutive-loss halts, and concurrency. These are HARD gates evaluated
independently of the score — a great setup in a blackout window is still blocked.

Everything is configurable and applies to paper trading first. Live execution
still passes the existing ExecutionGuard; these gates never place an order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time

from app.config import settings
from app.domain.enums import Direction, DTECategory, RejectReason
from app.domain.shortduration import ShortDurationRegimeState
from app.risk.policy import RiskPolicy
from app.scheduling.clock import MarketClock


def short_duration_policy(
    dte: DTECategory, *, equity: float | None = None, constrained: bool = False
) -> RiskPolicy:
    """A per-DTE risk policy: tighter per-trade % than the core scanner, same
    absolute $ cap, and a time-stop that matches the horizon (same-day for 0DTE).

    ``constrained=True`` forces the REAL account caps even when paper-verification
    mode is on — used by the Book B (account-executable) check, which must always
    measure against the true account, not the lifted signal-book cap."""
    pct = (
        settings.short_duration_0dte_risk_pct
        if dte == DTECategory.ZERO_DTE
        else settings.short_duration_1_5dte_risk_pct
    )
    # Paper verification: lift the per-trade $ cap and size a single contract so
    # every setup is expressible and comparable. Research/paper only — the live
    # ExecutionGuard is unaffected and still denies by default.
    unconstrained = settings.short_duration_paper_unconstrained and not constrained
    # In unconstrained mode the effective cap is min(equity×pct, absolute$). Raise
    # the equity too so neither term clamps a single expensive leg back out.
    eq = 10_000_000.0 if unconstrained else (equity or settings.account_equity_usd)
    return RiskPolicy(
        account_equity_usd=eq,
        max_account_risk_pct=1.0 if unconstrained else settings.max_account_risk_pct,
        max_trade_risk_pct=1.0 if unconstrained else pct,
        max_concurrent_positions=settings.short_duration_max_concurrent,
        max_defined_risk_per_trade_usd=(
            1_000_000.0 if unconstrained else settings.max_defined_risk_per_trade_usd
        ),
        max_contracts_per_trade=1 if unconstrained else settings.max_contracts_per_trade,
        default_profit_target_pct=0.5,
        default_stop_loss_pct=0.5,
        default_time_stop_dte=0 if dte == DTECategory.ZERO_DTE else 1,
    )


@dataclass
class DailyRiskState:
    """Today's short-duration risk posture. Populated from paper trades once
    Phase 5 lands; until then it is empty and the loss/halt gates pass."""

    realized_pnl_usd: float = 0.0
    consecutive_losses: int = 0
    open_positions: int = 0
    # (symbol, direction) of the currently-open book, for correlation concentration.
    open_book: list[tuple[str, str]] = field(default_factory=list)


# Names in a cluster move together, so a second same-direction position in the same
# cluster is a concentrated re-bet, not diversification (Phase 3.4).
_CORRELATION_GROUP = {
    "SPY": "index", "QQQ": "index", "IWM": "index", "DIA": "index",
    "NVDA": "semis", "AMD": "semis", "AVGO": "semis", "MU": "semis", "SMH": "semis",
    "AAPL": "megatech", "MSFT": "megatech", "META": "megatech", "GOOGL": "megatech",
    "AMZN": "megatech", "NFLX": "megatech",
}


def correlation_group(symbol: str) -> str:
    """Correlation cluster for a ticker (its own symbol if unclustered)."""
    return _CORRELATION_GROUP.get((symbol or "").upper(), (symbol or "").upper())


@dataclass
class RiskGateConfig:
    no_entry_first_minutes: int = 5
    cutoff_0dte_et: time = time(15, 0)
    daily_loss_pct: float = 0.05
    consecutive_loss_halt: int = 2
    max_concurrent: int = 2
    max_correlated_same_dir: int = 1  # same-cluster same-direction open positions

    @classmethod
    def from_settings(cls) -> RiskGateConfig:
        hh, _, mm = settings.short_duration_0dte_cutoff_et.partition(":")
        return cls(
            no_entry_first_minutes=settings.short_duration_no_entry_first_minutes,
            cutoff_0dte_et=time(int(hh), int(mm or 0)),
            daily_loss_pct=settings.short_duration_daily_loss_pct,
            consecutive_loss_halt=settings.short_duration_consecutive_loss_halt,
            max_concurrent=settings.short_duration_max_concurrent,
            max_correlated_same_dir=settings.short_duration_max_correlated_same_dir,
        )


@dataclass
class EntryGate:
    allowed: bool
    size_modifier: float  # 1.0 normal, 0.5 reduce, 0.0 blocked
    reasons: list[str] = field(default_factory=list)
    reject_reasons: list[RejectReason] = field(default_factory=list)


def evaluate_entry_gates(
    *,
    dte: DTECategory,
    direction: Direction,
    regime: ShortDurationRegimeState,
    now: datetime,
    quote_stale: bool,
    daily: DailyRiskState,
    equity: float,
    symbol: str | None = None,
    clock: MarketClock | None = None,
    config: RiskGateConfig | None = None,
) -> EntryGate:
    clock = clock or MarketClock()
    cfg = config or RiskGateConfig.from_settings()
    reasons: list[str] = []
    rejects: list[RejectReason] = []
    size = 1.0

    now_et = clock.now_et(now)
    if not clock.is_market_open(now):
        return EntryGate(False, 0.0, ["Market is closed."], [RejectReason.TIME_OF_DAY_BLOCKED])

    open_dt = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if (now_et - open_dt).total_seconds() < cfg.no_entry_first_minutes * 60:
        reasons.append(f"Within the first {cfg.no_entry_first_minutes}m of the open.")
        rejects.append(RejectReason.TIME_OF_DAY_BLOCKED)
    if dte == DTECategory.ZERO_DTE and now_et.time() >= cfg.cutoff_0dte_et:
        reasons.append(f"Past the 0DTE entry cutoff ({cfg.cutoff_0dte_et.strftime('%H:%M')} ET).")
        rejects.append(RejectReason.TIME_OF_DAY_BLOCKED)

    if quote_stale:
        reasons.append("Quote data is stale.")
        rejects.append(RejectReason.STALE_QUOTE)

    daily_loss_cap = equity * cfg.daily_loss_pct
    if daily.realized_pnl_usd <= -daily_loss_cap:
        reasons.append(f"Daily loss limit hit (-${daily_loss_cap:g}).")
        rejects.append(RejectReason.DAILY_LOSS_LIMIT)
    if daily.consecutive_losses >= cfg.consecutive_loss_halt:
        reasons.append(f"{daily.consecutive_losses} consecutive losses — halted.")
        rejects.append(RejectReason.DAILY_LOSS_LIMIT)
    if daily.open_positions >= cfg.max_concurrent:
        reasons.append(f"At the max {cfg.max_concurrent} concurrent short-duration positions.")
        rejects.append(RejectReason.PORTFOLIO_LIMIT)
    if symbol and daily.open_book:
        grp = correlation_group(symbol)
        same = sum(1 for s, d in daily.open_book
                   if correlation_group(s) == grp and d == direction.value)
        if same >= cfg.max_correlated_same_dir:
            reasons.append(
                f"Already holding {same} correlated {direction.value} position(s) in the "
                f"{grp} cluster — concentration limit."
            )
            rejects.append(RejectReason.PORTFOLIO_LIMIT)

    if not regime.allow_new_trades:
        reasons.append("Regime blocks new trades (event/volatility).")
        rejects.append(RejectReason.RESTRICTED_EVENT_WINDOW)
    elif regime.reduce_size:
        size = 0.5
        reasons.append("Regime says reduce size.")

    allowed = not rejects
    if allowed and not reasons:
        reasons.append("All entry gates clear.")
    return EntryGate(allowed=allowed, size_modifier=(size if allowed else 0.0),
                     reasons=reasons, reject_reasons=rejects)


# --- Timing gates are re-evaluated on READ ------------------------------------
# `evaluate_entry_gates` runs once, at scan time, and its verdict is frozen onto
# the candidate (detection.py:350/353). For most gates that is right: whether a
# structure is illiquid or unsizeable is a property of the setup and does not
# change because a reader refreshed the page.
#
# Timing is different. It is a property of the CLOCK, not of the setup, and it
# goes stale in both directions:
#
#   - a row scanned pre-market or inside the opening window keeps
#     `time_of_day_blocked` for the rest of the session, showing BLOCKED long
#     after entry became fine;
#   - a 0DTE row scanned at 14:00 keeps ALLOWED past the 15:00 cutoff.
#
# The second is the dangerous direction, which is why this recomputes rather
# than merely un-blocking. `ranking.py` already separates these as
# `_TIMING_ONLY_BLOCKS` ("about WHEN, not about whether the setup is any good").

# Reject reasons produced by `evaluate_entry_gates` — as opposed to the contract
# builder, whose reasons are merged into the same stored list. Only these bear
# on `entry_allowed`; `illiquid_option` and friends do not.
GATE_REJECTS: frozenset[str] = frozenset({
    RejectReason.TIME_OF_DAY_BLOCKED.value,
    RejectReason.STALE_QUOTE.value,
    RejectReason.DAILY_LOSS_LIMIT.value,
    RejectReason.PORTFOLIO_LIMIT.value,
    RejectReason.RESTRICTED_EVENT_WINDOW.value,
})

_TIMING = RejectReason.TIME_OF_DAY_BLOCKED.value


def timing_gate_now(
    dte: DTECategory,
    now: datetime,
    *,
    clock: MarketClock | None = None,
    config: RiskGateConfig | None = None,
) -> tuple[bool, str]:
    """Is entry timing-blocked *right now*? Returns (blocked, reason).

    Pure function of the clock and the bucket — no market data, no I/O — which
    is what makes it cheap enough to run on every read.
    """
    clock = clock or MarketClock()
    cfg = config or RiskGateConfig.from_settings()
    now_et = clock.now_et(now)

    if not clock.is_market_open(now):
        return True, "Market is closed."
    open_dt = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if (now_et - open_dt).total_seconds() < cfg.no_entry_first_minutes * 60:
        return True, f"Within the first {cfg.no_entry_first_minutes}m of the open."
    if dte == DTECategory.ZERO_DTE and now_et.time() >= cfg.cutoff_0dte_et:
        return True, f"Past the 0DTE entry cutoff ({cfg.cutoff_0dte_et.strftime('%H:%M')} ET)."
    return False, ""


@dataclass(frozen=True)
class RefreshedGate:
    """A stored gate verdict with its timing component brought up to date."""

    entry_allowed: bool
    reject_reasons: list[str]
    timing_blocked: bool
    timing_reason: str
    # The instant the NON-timing gates were evaluated. Timing is current as of
    # the read; everything else is as old as this. Surfaced so a stale verdict
    # can be shown as stale rather than passed off as current.
    gates_evaluated_at: datetime | None


def refresh_timing_gate(
    *,
    dte: DTECategory,
    stored_reject_reasons: list[str] | None,
    evaluated_at: datetime | None,
    now: datetime,
    clock: MarketClock | None = None,
    config: RiskGateConfig | None = None,
) -> RefreshedGate:
    """Recompute the timing component of a frozen gate verdict.

    Non-timing rejects are preserved exactly as stored — they are properties of
    the setup and of account state at scan time, and this function has neither
    the account state nor the chain to re-derive them. Only `time_of_day_blocked`
    is added or removed.

    `entry_allowed` is therefore: no non-timing GATE reject was recorded, and
    timing is clear now. Contract-level rejects (illiquid_option, ...) are left
    in the list but do not bear on `entry_allowed`, matching scan-time semantics
    where `entry_allowed` is `gate.allowed` alone.
    """
    stored = list(stored_reject_reasons or [])
    blocked, reason = timing_gate_now(dte, now, clock=clock, config=config)

    others = [r for r in stored if r != _TIMING]
    reasons = ([*others, _TIMING] if blocked else others)
    blocking_now = {r for r in others if r in GATE_REJECTS}

    return RefreshedGate(
        entry_allowed=(not blocking_now and not blocked),
        reject_reasons=reasons,
        timing_blocked=blocked,
        timing_reason=reason,
        gates_evaluated_at=evaluated_at,
    )


def apply_live_timing(
    candidates: list,
    *,
    now: datetime | None = None,
    clock: MarketClock | None = None,
    config: RiskGateConfig | None = None,
) -> list:
    """Bring each candidate's timing gate up to the reading clock, in place.

    Called from the read path so the board cannot show a 09:31 verdict at 09:54.
    Cheap by construction: no market data, no DB, no I/O — the timing gate is a
    function of the clock and the DTE bucket alone.
    """
    now = now or datetime.now(UTC)
    clock = clock or MarketClock()
    config = config or RiskGateConfig.from_settings()
    for c in candidates:
        r = refresh_timing_gate(
            dte=c.dte_category,
            stored_reject_reasons=c.reject_reasons,
            evaluated_at=c.detected_at,
            now=now, clock=clock, config=config,
        )
        c.entry_allowed = r.entry_allowed
        c.reject_reasons = r.reject_reasons
        c.entry_gates_evaluated_at = r.gates_evaluated_at
        c.entry_timing_is_live = True
        # Keep the human-readable notes consistent with the recomputed verdict:
        # drop any stale timing prose, then add the current reason if blocked.
        notes = [n for n in (c.entry_notes or [])
                 if "first" not in n.lower() and "cutoff" not in n.lower()
                 and "market is closed" not in n.lower()]
        if r.timing_blocked:
            notes.append(r.timing_reason)
        c.entry_notes = notes
    return candidates
