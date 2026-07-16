"""Shared async HTTP helper for live providers.

Centralizes retry/backoff, timeout, and rate-limit-aware error handling so each
provider client stays focused on endpoint + response mapping. Uses httpx +
tenacity. 429/5xx are retried with exponential backoff; 4xx (auth/bad request)
fail fast.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.logging_config import get_logger

log = get_logger(__name__)


class ProviderHTTPError(RuntimeError):
    def __init__(self, provider: str, status: int, url: str, body: str = "") -> None:
        super().__init__(f"[{provider}] HTTP {status} for {url}: {body[:200]}")
        self.provider = provider
        self.status = status
        self.url = url


class RateLimitedError(ProviderHTTPError):
    pass


class AsyncHTTP:
    """Thin async client wrapper. One instance per provider."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        default_params: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.provider = provider
        self._default_params = default_params or {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers or {},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncHTTP:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @retry(
        retry=retry_if_exception_type((RateLimitedError, httpx.TransportError)),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        merged = {**self._default_params, **(params or {})}
        resp = await self._client.get(path, params=merged)
        if resp.status_code == 429:
            log.warning("rate_limited", provider=self.provider, path=path)
            raise RateLimitedError(self.provider, 429, str(resp.url), resp.text)
        if resp.status_code >= 500:
            raise ProviderHTTPError(self.provider, resp.status_code, str(resp.url), resp.text)
        if resp.status_code >= 400:
            # Client error (auth, bad params) — do not retry.
            raise ProviderHTTPError(self.provider, resp.status_code, str(resp.url), resp.text)
        return resp.json()
