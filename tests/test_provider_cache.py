"""Response cache: TTL rules, expiry, key hygiene, resilient fallback, and the
AsyncHTTP integration (cache hit avoids a second HTTP call)."""

from __future__ import annotations

import httpx

from app.providers import _http
from app.providers.cache import (
    MISS,
    InMemoryCache,
    ResilientBackend,
    ResponseCache,
    default_ttl_seconds,
)
from app.providers.ratelimit import ProviderLimiter


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_ttl_rules_by_path() -> None:
    assert default_ttl_seconds("/stable/quote?x") == 10
    assert default_ttl_seconds("/api/stock/AAPL/flow-alerts") == 30
    assert default_ttl_seconds("/stable/profile") == 86_400
    assert default_ttl_seconds("/api/stock/AAPL/iv-rank") == 3_600
    assert default_ttl_seconds("/something/unknown") == 15  # default


async def test_inmemory_get_set_and_expiry() -> None:
    clock = _Clock()
    c = InMemoryCache(clock=clock)
    assert await c.get("k") is MISS
    await c.set("k", {"v": 1}, ttl=10)
    assert await c.get("k") == {"v": 1}
    clock.t += 9
    assert await c.get("k") == {"v": 1}  # still fresh
    clock.t += 2
    assert await c.get("k") is MISS  # expired at +11 > +10


def test_key_excludes_auth_params() -> None:
    c = ResponseCache(InMemoryCache())
    k1 = c.key("fmp", "/stable/quote", {"symbol": "AAPL", "apikey": "SECRET"})
    k2 = c.key("fmp", "/stable/quote", {"symbol": "AAPL"})
    assert k1 == k2  # apikey excluded
    assert "SECRET" not in k1
    # Different symbol -> different key.
    assert c.key("fmp", "/stable/quote", {"symbol": "MSFT"}) != k1


async def test_hit_miss_counters() -> None:
    c = ResponseCache(InMemoryCache())
    key = c.key("uw", "/x", {})
    assert await c.get(key) is MISS
    await c.set(key, [1, 2, 3], "/x")
    assert await c.get(key) == [1, 2, 3]
    assert c.stats()["hits"] == 1 and c.stats()["misses"] == 1


async def test_disabled_cache_is_always_miss() -> None:
    c = ResponseCache(InMemoryCache(), enabled=False)
    key = c.key("uw", "/x", {})
    await c.set(key, {"v": 1}, "/x")
    assert await c.get(key) is MISS


async def test_resilient_backend_degrades_to_fallback() -> None:
    class Broken:
        async def get(self, key):
            raise RuntimeError("redis down")

        async def set(self, key, value, ttl):
            raise RuntimeError("redis down")

        async def clear(self):
            raise RuntimeError("redis down")

    fallback = InMemoryCache()
    b = ResilientBackend(Broken(), fallback)
    await b.set("k", {"v": 1}, ttl=10)  # primary fails -> fallback
    assert b._degraded is True
    assert await b.get("k") == {"v": 1}  # served from fallback


async def test_asynchttp_cache_hit_skips_second_request(monkeypatch) -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    # Enabled cache + no-op limiter, injected into the choke point.
    cache = ResponseCache(InMemoryCache(), enabled=True)
    monkeypatch.setattr(_http, "get_response_cache", lambda: cache)
    monkeypatch.setattr(_http, "get_limiter", lambda: ProviderLimiter(enabled=False))

    h = _http.AsyncHTTP("fmp", "http://x")
    h._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    )
    first = await h.get_json("/stable/quote", {"symbol": "AAPL"})
    second = await h.get_json("/stable/quote", {"symbol": "AAPL"})
    assert first == second == {"n": 1}
    assert calls["n"] == 1  # second call served from cache
    # A different symbol is a cache miss -> real request.
    await h.get_json("/stable/quote", {"symbol": "MSFT"})
    assert calls["n"] == 2
    await h.aclose()
