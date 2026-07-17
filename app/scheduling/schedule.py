"""Declarative per-session × per-tier cadence schedule.

Loads `config/scheduling.yaml` (editable without code changes) and answers
`cadence(session, tier) -> seconds | None`, where None means the tier does not
run in that session. Falls back to a built-in default table if the file is
missing or unparseable, so the system always has a valid schedule.
"""

from __future__ import annotations

from pathlib import Path

from app.logging_config import get_logger
from app.scheduling.clock import MarketSession
from app.tiers.models import Tier

log = get_logger(__name__)

_TIER_BY_NAME = {
    "broad": Tier.BROAD,
    "watchlist": Tier.WATCHLIST,
    "candidates": Tier.CANDIDATES,
    "positions": Tier.POSITIONS,
}

# Built-in default (seconds). Mirrors config/scheduling.yaml; used when the file
# is absent/unreadable so tests and fresh checkouts always have a schedule.
DEFAULT_SCHEDULE: dict[MarketSession, dict[Tier, int]] = {
    MarketSession.OVERNIGHT: {Tier.BROAD: 3600, Tier.POSITIONS: 1800},
    MarketSession.PRE_MARKET: {Tier.BROAD: 900, Tier.WATCHLIST: 900, Tier.POSITIONS: 300},
    MarketSession.FINAL_PRE_OPEN: {Tier.BROAD: 300, Tier.WATCHLIST: 300, Tier.POSITIONS: 300},
    MarketSession.OPENING: {Tier.WATCHLIST: 60, Tier.CANDIDATES: 60, Tier.POSITIONS: 45},
    MarketSession.PRIMARY: {Tier.BROAD: 300, Tier.WATCHLIST: 60, Tier.CANDIDATES: 30, Tier.POSITIONS: 20},
    MarketSession.MIDDAY: {Tier.BROAD: 720, Tier.WATCHLIST: 240, Tier.CANDIDATES: 45, Tier.POSITIONS: 45},
    MarketSession.AFTERNOON: {Tier.BROAD: 300, Tier.WATCHLIST: 120, Tier.CANDIDATES: 30, Tier.POSITIONS: 20},
    MarketSession.POWER_HOUR: {Tier.BROAD: 600, Tier.WATCHLIST: 60, Tier.CANDIDATES: 30, Tier.POSITIONS: 15},
    MarketSession.POST_CLOSE: {Tier.POSITIONS: 120},
    MarketSession.EARNINGS: {Tier.BROAD: 300, Tier.WATCHLIST: 300, Tier.POSITIONS: 300},
    MarketSession.CLOSED: {Tier.BROAD: 3600},
}


class TierSchedule:
    def __init__(self, table: dict[MarketSession, dict[Tier, int]]) -> None:
        self._table = table

    def cadence(self, session: MarketSession, tier: Tier) -> int | None:
        secs = self._table.get(session, {}).get(tier)
        return secs if secs and secs > 0 else None

    def active_tiers(self, session: MarketSession) -> list[Tier]:
        return [t for t in Tier if self.cadence(session, t) is not None]


def _parse(raw: dict) -> dict[MarketSession, dict[Tier, int]]:
    table: dict[MarketSession, dict[Tier, int]] = {}
    for sess_name, tiers in (raw or {}).items():
        try:
            session = MarketSession(sess_name)
        except ValueError:
            log.warning("schedule_unknown_session", session=sess_name)
            continue
        entry: dict[Tier, int] = {}
        for tier_name, secs in (tiers or {}).items():
            tier = _TIER_BY_NAME.get(tier_name)
            if tier is None:
                log.warning("schedule_unknown_tier", tier=tier_name)
                continue
            try:
                entry[tier] = int(secs)
            except (TypeError, ValueError):
                continue
        table[session] = entry
    return table


def load_schedule(path: str | Path | None = None) -> TierSchedule:
    p = Path(path) if path else Path(__file__).resolve().parents[2] / "config" / "scheduling.yaml"
    if p.exists():
        try:
            import yaml

            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
            table = _parse(raw)
            if table:
                return TierSchedule(table)
            log.warning("schedule_empty_using_default", path=str(p))
        except Exception as exc:  # noqa: BLE001 - bad file must not break scheduling
            log.warning("schedule_load_failed_using_default", path=str(p), error=str(exc))
    return TierSchedule(DEFAULT_SCHEDULE)
