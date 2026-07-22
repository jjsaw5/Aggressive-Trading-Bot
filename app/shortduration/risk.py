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
from datetime import datetime, time

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
