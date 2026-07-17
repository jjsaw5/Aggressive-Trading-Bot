"""Phase 0 provider-efficiency fixes: retry policy, rate-limit capture, pooling,
and live-provider singleton caching."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.providers import registry
from app.providers._http import (
    AsyncHTTP,
    ProviderHTTPError,
    RateLimitedError,
    ServerError,
)


def _http_with_handler(handler, **kwargs) -> AsyncHTTP:
    h = AsyncHTTP("t", "http://x", **kwargs)
    # Swap the real transport for a mock one; keeps the retry/error logic intact.
    h._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://x"
    )
    return h


async def test_5xx_is_retried_then_raises_server_error(monkeypatch) -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="boom")

    async def _instant(*_a, **_k) -> None:  # neutralize tenacity backoff
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)
    h = _http_with_handler(handler)
    with pytest.raises(ServerError):
        await h.get_json("/y")
    assert calls["n"] == 4  # 1 initial + 3 retries (stop_after_attempt=4)
    await h.aclose()


async def test_429_is_retried(monkeypatch) -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down")

    async def _instant(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)
    h = _http_with_handler(handler)
    with pytest.raises(RateLimitedError):
        await h.get_json("/y")
    assert calls["n"] == 4
    await h.aclose()


async def test_4xx_fails_fast_no_retry() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    h = _http_with_handler(handler)
    with pytest.raises(ProviderHTTPError) as ei:
        await h.get_json("/y")
    assert not isinstance(ei.value, (ServerError, RateLimitedError))
    assert calls["n"] == 1  # fail fast, no retry
    await h.aclose()


async def test_rate_limit_headers_captured() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True},
            headers={
                "x-uw-req-per-minute-remaining": "2",
                "x-uw-token-req-limit": "120",
            },
        )

    h = _http_with_handler(
        handler,
        rate_limit_headers={
            "remaining": "x-uw-req-per-minute-remaining",
            "limit": "x-uw-token-req-limit",
        },
    )
    out = await h.get_json("/y")
    assert out == {"ok": True}
    assert h.rate_limit.remaining == 2
    assert h.rate_limit.limit == 120
    assert h.rate_limit.observed_at is not None
    await h.aclose()


async def test_no_rate_limit_headers_is_noop() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    h = _http_with_handler(handler)  # no rate_limit_headers declared
    await h.get_json("/y")
    assert h.rate_limit.remaining is None and h.rate_limit.limit is None
    await h.aclose()


async def test_live_provider_helpers_are_cached_singletons() -> None:
    """FMP/UW must be singletons so one pooled client is reused (not leaked)."""
    registry._fmp.cache_clear()
    registry._uw.cache_clear()
    try:
        f1, f2 = registry._fmp(), registry._fmp()
        u1, u2 = registry._uw(), registry._uw()
        assert f1 is f2  # same FMP instance -> same connection pool
        assert u1 is u2  # same UW instance
        await f1.aclose()
        await u1.aclose()
    finally:
        registry._fmp.cache_clear()
        registry._uw.cache_clear()
