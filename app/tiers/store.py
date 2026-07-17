"""Async facade over tier-membership persistence (Turso).

The funnel is async; the repository is synchronous (libSQL), so DB work is
offloaded to threads. Membership of each tier is replaced atomically on every
funnel pass, which is how demotion happens — a symbol not re-promoted simply
isn't in the new set.
"""

from __future__ import annotations

import asyncio

from app.db import repository
from app.tiers.models import Tier, TierMember


class TierStore:
    async def replace(self, tier: Tier, members: list[TierMember]) -> None:
        await asyncio.to_thread(repository.replace_tier, int(tier), members)

    async def members(self, tier: Tier) -> list[TierMember]:
        return await asyncio.to_thread(repository.list_tier, int(tier))

    async def symbols(self, tier: Tier) -> list[str]:
        return [m.symbol for m in await self.members(tier)]

    async def all(self) -> list[TierMember]:
        return await asyncio.to_thread(repository.list_all_tiers)
