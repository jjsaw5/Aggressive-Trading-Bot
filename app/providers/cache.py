"""Response cache for live provider HTTP calls.

Read-only market data is highly cacheable on short horizons: within a few
seconds a quote hasn't moved enough to matter, and fundamentals/IV-history don't
change intraday at all. Caching at the single HTTP choke point (`AsyncHTTP`)
means every provider benefits with zero changes to client or engine code, and
the mock stack (which never touches HTTP) is unaffected.

Backend defaults to in-process memory. Redis is optional (set CACHE_BACKEND=
redis) and degrades gracefully to memory if unreachable — Redis is an
optimization, never a correctness dependency.

TTLs are tuned per data type by matching the request path; a global
`cache_ttl_scale` lets you lengthen/shorten everything at once.
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Any

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

# Sentinel distinguishing "cached value is None/empty" from "not cached".
MISS: Any = object()

# Auth params must never appear in a cache key (secret leakage + they don't
# affect the response shape).
_AUTH_PARAMS = {"apikey", "api_key", "token", "auth_token"}

# Ordered (path-substring -> TTL seconds). First match wins; else DEFAULT_TTL.
# Volatile market data gets seconds; static reference data gets hours/days.
TTL_RULES: list[tuple[str, int]] = [
    ("/historic", 86_400),  # per-contract history: immutable once the day closes
    ("profile", 86_400),  # fundamentals: effectively static intraday
    ("iv-rank", 3_600),  # 1Y IV series
    ("historical-price-eod", 3_600),  # daily bars
    ("earnings-calendar", 3_600),
    ("expiry-breakdown", 300),
    ("option-contracts", 45),  # chain contracts
    ("volatility/stats", 60),
    ("flow-alerts", 30),
    ("stock-state", 10),
    ("quote", 10),
]
DEFAULT_TTL = 15


def default_ttl_seconds(path: str) -> int:
    p = path.lower()
    for needle, ttl in TTL_RULES:
        if needle in p:
            return ttl
    return DEFAULT_TTL


# --- Backends ----------------------------------------------------------------
class InMemoryCache:
    """Process-local TTL cache. Stores native objects (no serialization)."""

    def __init__(self, clock=time.monotonic) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._clock = clock

    async def get(self, key: str) -> Any:
        item = self._store.get(key)
        if item is None:
            return MISS
        expires, val = item
        if self._clock() >= expires:
            self._store.pop(key, None)
            return MISS
        return val

    async def set(self, key: str, value: Any, ttl: int) -> None:
        self._store[key] = (self._clock() + ttl, value)

    async def clear(self) -> None:
        self._store.clear()


class RedisCache:
    """Redis-backed cache (JSON-serialized). Lazily imports redis.asyncio."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis  # lazy: only when CACHE_BACKEND=redis

        self._r = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Any:
        raw = await self._r.get(key)
        return MISS if raw is None else json.loads(raw)

    async def set(self, key: str, value: Any, ttl: int) -> None:
        await self._r.set(key, json.dumps(value, default=str), ex=ttl)

    async def clear(self) -> None:
        await self._r.flushdb()


class ResilientBackend:
    """Wraps a primary backend; on the first error, logs once and degrades to a
    fallback backend permanently for the process. Keeps Redis optional."""

    def __init__(self, primary, fallback) -> None:
        self._primary = primary
        self._fallback = fallback
        self._degraded = False

    def _degrade(self, exc: Exception) -> None:
        if not self._degraded:
            log.warning("cache_backend_degraded_to_fallback", error=str(exc))
            self._degraded = True

    async def get(self, key: str) -> Any:
        if self._degraded:
            return await self._fallback.get(key)
        try:
            return await self._primary.get(key)
        except Exception as exc:  # noqa: BLE001 - any backend error degrades
            self._degrade(exc)
            return await self._fallback.get(key)

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if self._degraded:
            return await self._fallback.set(key, value, ttl)
        try:
            return await self._primary.set(key, value, ttl)
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)
            return await self._fallback.set(key, value, ttl)

    async def clear(self) -> None:
        await self._fallback.clear()
        if not self._degraded:
            try:
                await self._primary.clear()
            except Exception:  # noqa: BLE001
                pass


# --- Facade ------------------------------------------------------------------
class ResponseCache:
    def __init__(self, backend, *, enabled: bool = True, ttl_scale: float = 1.0) -> None:
        self.backend = backend
        self.enabled = enabled
        self.ttl_scale = ttl_scale
        self.hits = 0
        self.misses = 0

    def key(self, provider: str, path: str, params: dict | None) -> str:
        clean = {
            k: v
            for k, v in (params or {}).items()
            if k.lower() not in _AUTH_PARAMS
        }
        blob = json.dumps(clean, sort_keys=True, default=str)
        return f"{provider}:{path}:{blob}"

    def ttl_for(self, path: str) -> int:
        return int(default_ttl_seconds(path) * self.ttl_scale)

    async def get(self, key: str) -> Any:
        if not self.enabled:
            return MISS
        val = await self.backend.get(key)
        if val is MISS:
            self.misses += 1
        else:
            self.hits += 1
        return val

    async def set(self, key: str, value: Any, path: str) -> None:
        if not self.enabled:
            return
        ttl = self.ttl_for(path)
        if ttl <= 0:
            return
        try:
            await self.backend.set(key, value, ttl)
        except Exception as exc:  # noqa: BLE001 - cache set must never break a call
            log.warning("cache_set_failed", error=str(exc))

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else None,
        }


def _build_backend():
    if settings.cache_backend == "redis":
        try:
            return ResilientBackend(RedisCache(settings.redis_url), InMemoryCache())
        except Exception as exc:  # noqa: BLE001 - redis missing/unreachable
            log.warning("redis_cache_unavailable_fallback_memory", error=str(exc))
    return InMemoryCache()


@lru_cache
def get_response_cache() -> ResponseCache:
    return ResponseCache(
        _build_backend(),
        enabled=settings.cache_enabled,
        ttl_scale=settings.cache_ttl_scale,
    )
