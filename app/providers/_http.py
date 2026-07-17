"""Shared async HTTP helper for live providers.

Centralizes retry/backoff, timeout, connection pooling, and rate-limit-aware
error handling so each provider client stays focused on endpoint + response
mapping. Uses httpx + tenacity.

Retry policy: HTTP 429 (rate limited), 5xx (server error), and transport errors
are retried with exponential backoff; 4xx (auth/bad request) fail fast.

Each instance owns ONE pooled `httpx.AsyncClient` whose keep-alive connections
are reused across requests, so a provider should be constructed once and reused
for the life of the process — the registry caches live providers as singletons
for exactly this reason. Constructing a fresh provider per call would leak a
connection pool each time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.logging_config import get_logger
from app.providers.cache import MISS, get_response_cache
from app.providers.ratelimit import current_priority, get_limiter

log = get_logger(__name__)


class ProviderHTTPError(RuntimeError):
    def __init__(self, provider: str, status: int, url: str, body: str = "") -> None:
        super().__init__(f"[{provider}] HTTP {status} for {url}: {body[:200]}")
        self.provider = provider
        self.status = status
        self.url = url


class RateLimitedError(ProviderHTTPError):
    """HTTP 429 — the provider throttled us. Retried with backoff."""


class ServerError(ProviderHTTPError):
    """HTTP 5xx — transient server-side failure. Retried with backoff."""


@dataclass
class RateLimitSnapshot:
    """Latest rate-limit budget parsed from a provider's response headers.

    Populated opportunistically when a provider declares which headers carry its
    remaining/limit budget (e.g. Unusual Whales' x-uw-* headers). This is the
    observability hook the Phase-2 request budgeter will build on; for now it is
    captured and logged so budget exhaustion is visible before a 429 hits.
    """

    remaining: int | None = None
    limit: int | None = None
    observed_at: datetime | None = None


# Shared pool sizing. Keep-alive connections are retained and reused across
# requests (and across scans) instead of being torn down each call.
_POOL_LIMITS = httpx.Limits(
    max_keepalive_connections=20, max_connections=100, keepalive_expiry=30.0
)


class AsyncHTTP:
    """Thin async client wrapper with one pooled connection set.

    One instance per provider — construct once and reuse (the registry keeps
    live providers as singletons). Not safe to share a base_url/auth across
    providers, so pooling is per-provider rather than process-global.
    """

    def __init__(
        self,
        provider: str,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        default_params: dict[str, str] | None = None,
        timeout: float = 15.0,
        rate_limit_headers: dict[str, str] | None = None,
    ) -> None:
        self.provider = provider
        self._default_params = default_params or {}
        # Logical name -> response header name (e.g. {"remaining":
        # "x-uw-req-per-minute-remaining"}). Empty = provider exposes no budget.
        self._rl_headers = rate_limit_headers or {}
        self.rate_limit = RateLimitSnapshot()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers or {},
            timeout=timeout,
            limits=_POOL_LIMITS,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncHTTP:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _capture_rate_limit(self, resp: httpx.Response) -> None:
        if not self._rl_headers:
            return

        def _hdr(name: str) -> int | None:
            raw = resp.headers.get(name)
            try:
                return int(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        remaining = _hdr(self._rl_headers.get("remaining", ""))
        limit = _hdr(self._rl_headers.get("limit", ""))
        if remaining is None and limit is None:
            return
        self.rate_limit = RateLimitSnapshot(
            remaining=remaining, limit=limit, observed_at=datetime.now(UTC)
        )
        # Surface budget pressure BEFORE a 429 forces backoff.
        low = (limit and remaining is not None and remaining <= max(1, int(limit * 0.1)))
        if low or (remaining is not None and remaining <= 3):
            log.warning(
                "provider_rate_limit_low",
                provider=self.provider,
                remaining=remaining,
                limit=limit,
            )

    @retry(
        retry=retry_if_exception_type(
            (RateLimitedError, ServerError, httpx.TransportError)
        ),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        # 1) Serve from cache when fresh (keyed on non-auth params only).
        cache = get_response_cache()
        key = cache.key(self.provider, path, params) if cache.enabled else None
        if key is not None:
            hit = await cache.get(key)
            if hit is not MISS:
                return hit

        # 2) Acquire rate-limit/budget permission for a REAL request. Priority
        #    comes from the calling context (open positions preempt broad scans).
        await get_limiter().acquire(self.provider, current_priority.get())

        merged = {**self._default_params, **(params or {})}
        resp = await self._client.get(path, params=merged)
        self._capture_rate_limit(resp)
        if resp.status_code == 429:
            log.warning("rate_limited", provider=self.provider, path=path)
            raise RateLimitedError(self.provider, 429, str(resp.url), resp.text)
        if resp.status_code >= 500:
            # Transient server-side failure — retry with backoff.
            raise ServerError(self.provider, resp.status_code, str(resp.url), resp.text)
        if resp.status_code >= 400:
            # Client error (auth, bad params) — do not retry.
            raise ProviderHTTPError(self.provider, resp.status_code, str(resp.url), resp.text)

        data = resp.json()
        # 3) Cache successful responses with a per-data-type TTL.
        if key is not None:
            await cache.set(key, data, path)
        return data
