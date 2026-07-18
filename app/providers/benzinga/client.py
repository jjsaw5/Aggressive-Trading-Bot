"""Benzinga news provider — low-latency headlines for short-duration trading.

Grounded on Benzinga's public v2 Newsfeed API:
  * Base:  https://api.benzinga.com
  * GET /api/v2/news
  * Auth:  `token` query param OR `Authorization: token <KEY>` header (header used
           here so the key never lands in a URL or cache key).
  * IMPORTANT: the endpoint defaults to XML — `Accept: application/json` is
    required to get JSON back.
  * Key params: `tickers` (comma list), `pageSize`, `page`, `displayOutput`
    ("full"|"abstract"|"headline"), `updatedSince` (unix seconds).
  * Item fields: id, author, created, updated (RFC-2822), title, teaser, body,
    url, stocks:[{name}], channels:[{name}], tags:[{name}], image:[...].

`meta.verified=False`: the mapping is grounded on Benzinga's published docs but
is not yet exercised against a live key in this environment. Field access is
defensive; missing fields degrade to None rather than being fabricated. Activated
only when `BENZINGA_API_KEY` is set — otherwise the registry routes news to FMP.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from app.config import settings
from app.domain.shortduration import NewsItem
from app.providers._http import AsyncHTTP
from app.providers.base import NewsProvider, ProviderMeta

_META = ProviderMeta(
    name="benzinga",
    requires_auth=True,
    typical_delay="marketed real-time / low-latency newswire; confirm for your plan.",
    rate_limit="per-plan; unpublished. Back off on 429 (handled by AsyncHTTP).",
    licensing="Commercial license required; personal/eval per your Benzinga agreement.",
    docs_url="https://docs.benzinga.com",
    verified=False,  # Grounded on published v2 docs; needs a live smoke test.
)


def _parse_rfc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _first_name(items: Any) -> str | None:
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("name"):
                return str(it["name"])
    return None


def _all_names(items: Any) -> list[str]:
    out: list[str] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("name"):
                out.append(str(it["name"]).upper())
    return out


class BenzingaProvider(NewsProvider):
    meta = _META

    def __init__(self) -> None:
        key = settings.benzinga_api_key or ""
        self._http = AsyncHTTP(
            provider="benzinga",
            base_url=settings.benzinga_base_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"token {key}",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_news(
        self,
        symbols: list[str] | None = None,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[NewsItem]:
        params: dict[str, Any] = {"pageSize": min(limit, 100), "displayOutput": "full"}
        if symbols:
            params["tickers"] = ",".join(s.upper() for s in symbols)
        if since is not None:
            params["updatedSince"] = int(since.timestamp())
        data = await self._http.get_json("/api/v2/news", params)
        rows = data if isinstance(data, list) else []
        now = datetime.now(UTC)
        out: list[NewsItem] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            title = r.get("title") or ""
            if not title:
                continue
            created = _parse_rfc(r.get("created"))
            tickers = _all_names(r.get("stocks"))
            out.append(
                NewsItem(
                    id=f"benzinga:{r.get('id') or r.get('url') or title}"[:180],
                    symbol=tickers[0] if tickers else None,
                    headline=title,
                    summary=(r.get("teaser") or "")[:2000],
                    source="benzinga",
                    category=_first_name(r.get("channels")),
                    url=r.get("url"),
                    source_ts=created,
                    provider_ts=created,
                    received_ts=now,
                    raw_ref=str(r.get("id")) if r.get("id") is not None else r.get("url"),
                )
            )
        return out[:limit]
