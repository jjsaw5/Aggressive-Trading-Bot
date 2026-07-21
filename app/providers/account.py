"""Account-state providers — paper and configured-fallback.

Both are UNVERIFIED (`verified=False`): a live, authenticated broker feed lands
later and is the only source that may set `verified=True`. Neither of these can
make a trade live-executable — they only inform sizing so it draws on a real
capital picture (equity minus committed risk) instead of a bare constant.

- **PaperAccountState** — equity = configured base + cumulative realized paper P&L;
  open risk = summed defined worst-case loss of OPEN short-duration paper positions.
- **ConfiguredFallbackAccountState** — the configured account equity as a pure
  constant, no committed risk. The safe floor when nothing better is available.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.config import settings
from app.domain.account import AccountState
from app.providers.base import AccountStateProvider, ProviderMeta


def _base_equity() -> float:
    return float(settings.account_equity_usd)


class ConfiguredFallbackAccountState(AccountStateProvider):
    """Account equity straight from configuration — no committed risk, buying power
    equal to equity (a defined-risk cash account). Unverified by construction."""

    meta = ProviderMeta(
        name="fallback_account", requires_auth=False, typical_delay="static (configured)",
        rate_limit=None, licensing="internal", docs_url=None, verified=False,
    )

    async def get_account_state(self, *, now: datetime | None = None) -> AccountState:
        eq = _base_equity()
        return AccountState(
            source="fallback", verified=False, equity_usd=eq, buying_power_usd=eq,
            open_risk_usd=0.0, pending_risk_usd=0.0, cash_usd=eq,
            as_of=now or datetime.now(UTC),
            note="Configured account equity (no live feed); unverified.",
        )


class PaperAccountState(AccountStateProvider):
    """Equity = configured base + cumulative realized paper P&L; open risk = the
    summed defined-risk of open short-duration paper positions. Buying power tracks
    available equity (defined-risk cash account). Unverified — it is simulated."""

    meta = ProviderMeta(
        name="paper_account", requires_auth=False, typical_delay="realtime (simulated)",
        rate_limit=None, licensing="internal", docs_url=None, verified=False,
    )

    def _snapshot(self) -> tuple[float, float]:
        """(cumulative realized P&L, open defined-risk) from the paper book."""
        from app.db import repository

        trades = repository.list_short_duration_trades(limit=2000)
        realized = sum(t.realized_pnl_usd or 0.0 for t in trades if t.realized_pnl_usd is not None)
        open_risk = sum(
            (t.max_loss_usd or 0.0) for t in trades if t.status == "open"
        )
        return round(realized, 2), round(open_risk, 2)

    async def get_account_state(self, *, now: datetime | None = None) -> AccountState:
        realized, open_risk = await asyncio.to_thread(self._snapshot)
        equity = round(_base_equity() + realized, 2)
        # Buying power on a small defined-risk cash account: equity less what open
        # positions already tie up. Floored at zero.
        bp = round(max(0.0, equity - open_risk), 2)
        return AccountState(
            source="paper", verified=False, equity_usd=equity, buying_power_usd=bp,
            open_risk_usd=open_risk, pending_risk_usd=0.0, cash_usd=equity,
            as_of=now or datetime.now(UTC),
            note="Paper book: configured base + realized paper P&L; unverified.",
        )
