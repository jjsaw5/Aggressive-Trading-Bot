"""Pipeline stages, and why the distinction is enforced rather than advised.

Option contracts do not have useful quotes before the options market opens. A
premarket chain returns yesterday's marks, a one-sided book, or nothing. A
structure selected against that is priced against a fiction — and worse, it
looks exactly like a real one in a report.

So the pipeline has two stages:

* **PREMARKET** — Agents 1 and 2 run. Market context, catalysts, candidate
  tickers, directional theses and preliminary strategy types. **No chain is
  retrieved and no contract is selected.** The report says so.
* **MARKET_OPEN** — Agent 3 retrieves fresh underlying prices, chains, bid/ask,
  volume, open interest, IV, Greeks and flow. Only now are contracts finalised
  and scores produced.

`FULL` runs both back to back, which is what the CLI does by default.

`resolve_stage` reads the platform's existing `MarketClock` rather than a second
schedule, so this subsystem and the rest of the platform can never disagree
about whether the market is open.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.multiagent.models.enums import PipelineStage
from app.scheduling.clock import MarketClock, MarketSession

# Sessions in which the options market is open and quotes are actionable.
_LIVE_SESSIONS = frozenset(
    {
        MarketSession.OPENING,
        MarketSession.PRIMARY,
        MarketSession.MIDDAY,
        MarketSession.AFTERNOON,
        MarketSession.POWER_HOUR,
    }
)


def market_session(now: datetime | None = None) -> MarketSession:
    return MarketClock().session(now or datetime.now(UTC))


def options_market_open(now: datetime | None = None) -> bool:
    return market_session(now) in _LIVE_SESSIONS


def resolve_stage(requested: PipelineStage, now: datetime | None = None) -> tuple[PipelineStage, str]:
    """Resolve the stage actually runnable, with a note explaining any downgrade.

    A MARKET_OPEN or FULL run requested while the options market is closed is
    **downgraded to PREMARKET**, not failed and not run anyway. Running it anyway
    would produce contracts priced off stale quotes; failing would make the
    research half unavailable outside market hours for no reason.
    """
    when = now or datetime.now(UTC)
    session = market_session(when)
    live = session in _LIVE_SESSIONS

    if requested is PipelineStage.PREMARKET:
        return PipelineStage.PREMARKET, (
            "Premarket stage requested: research and candidate generation only. No option chain "
            "is retrieved and no contract is selected."
        )

    if live:
        return requested, f"Options market is open (session={session.value}); contracts are live-quoted."

    return PipelineStage.PREMARKET, (
        f"{requested.value} was requested but the options market is closed "
        f"(session={session.value}), so the run was downgraded to premarket. Contracts are NOT "
        "selected: option quotes outside market hours are stale or absent, and a structure "
        "priced against them would be priced against a fiction. Re-run after the open to finalise."
    )


def stage_finalises_contracts(stage: PipelineStage) -> bool:
    return stage in (PipelineStage.MARKET_OPEN, PipelineStage.FULL)
