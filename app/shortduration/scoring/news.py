"""News scoring + duplicate/stale detection for short-duration decisions.

A configurable weighted model (source authority, novelty, materiality, symbol
relevance, and price/volume/flow confirmation). Recycled headlines must not
create new candidates, so novelty is gated by headline-similarity dedup. News can
raise a candidate's score but — by the module's core principle — confirmation
(price/volume/flow) is required before it counts for much.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.enums import Direction
from app.domain.shortduration import NewsItem
from app.shortduration.scoring.flow_decay import FlowAnalysis
from app.shortduration.scoring.models import NewsScore

# Source authority tiers (extend as providers are added). Lowercased contains-match.
_SOURCE_TIER: dict[str, float] = {
    "benzinga": 0.9, "reuters": 1.0, "bloomberg": 1.0, "dow jones": 0.95,
    "associated press": 0.95, "cnbc": 0.8, "the wall street journal": 0.95,
    "seeking alpha": 0.5, "motley fool": 0.4, "globenewswire": 0.6,
    "business wire": 0.7, "pr newswire": 0.6,
}
_BULLISH_KW = re.compile(
    r"\b(upgrad\w*|rais\w*|beat\w*|surg\w*|soar\w*|jump\w*|rall\w*|record|approv\w*|win\w*|"
    r"award\w*|buyback|outperform\w*|breakout|boost\w*|top\w*)\b", re.I,
)
_BEARISH_KW = re.compile(
    r"\b(downgrad\w*|cut\w*|miss\w*|plung\w*|sink\w*|slump\w*|fall\w*|lawsuit\w*|prob\w*|"
    r"recall\w*|halt\w*|warn\w*|underperform\w*|investigat\w*|delist\w*|slash\w*)\b", re.I,
)
_MATERIAL_KW = re.compile(
    r"\b(earnings|guidance|fda|acquisition\w*|merger\w*|m&a|buyout|lawsuit\w*|sec|bankruptc\w*|"
    r"downgrad\w*|upgrad\w*|contract\w*|approv\w*|recall\w*|halt\w*|split|dividend\w*|ceo|resign\w*)\b",
    re.I,
)
_STOP = frozenset("a an the of to in on for and or with at is are as by from this that its".split())


@dataclass(frozen=True)
class NewsWeights:
    source_authority: float = 20.0
    novelty: float = 20.0
    materiality: float = 25.0
    relevance: float = 15.0
    price_confirmation: float = 10.0
    volume_confirmation: float = 5.0
    flow_confirmation: float = 5.0

    @property
    def total(self) -> float:
        return (self.source_authority + self.novelty + self.materiality + self.relevance
                + self.price_confirmation + self.volume_confirmation + self.flow_confirmation)


@dataclass
class DedupState:
    """Rolling set of recently-seen headline token-sets for novelty checks."""

    seen: list[frozenset[str]] = field(default_factory=list)
    threshold: float = 0.6

    def is_duplicate(self, headline: str) -> bool:
        toks = _tokens(headline)
        if not toks:
            return False
        for prev in self.seen:
            if _jaccard(toks, prev) >= self.threshold:
                return True
        return False

    def add(self, headline: str) -> None:
        self.seen.append(_tokens(headline))


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def classify_direction(headline: str) -> Direction:
    bull = bool(_BULLISH_KW.search(headline))
    bear = bool(_BEARISH_KW.search(headline))
    if bull and not bear:
        return Direction.BULLISH
    if bear and not bull:
        return Direction.BEARISH
    return Direction.NEUTRAL


def _source_authority(source: str) -> float:
    s = (source or "").lower()
    for name, tier in _SOURCE_TIER.items():
        if name in s:
            return tier
    return 0.35  # unknown source — not zero, but low


def score_news(
    item: NewsItem,
    *,
    for_symbol: str | None = None,
    change_pct: float | None = None,
    rel_volume: float | None = None,
    flow: FlowAnalysis | None = None,
    dedup: DedupState | None = None,
    weights: NewsWeights | None = None,
) -> NewsScore:
    w = weights or NewsWeights()
    direction = item.direction or classify_direction(item.headline)

    authority = _source_authority(item.source)
    is_dup = dedup.is_duplicate(item.headline) if dedup else False
    novelty = 0.15 if is_dup else 1.0
    material_hits = len(_MATERIAL_KW.findall(f"{item.headline} {item.summary}"))
    materiality = min(1.0, 0.3 + 0.35 * material_hits) if material_hits else 0.3
    relevance = 1.0 if (for_symbol and item.symbol and item.symbol.upper() == for_symbol.upper()) else (
        0.6 if item.symbol else 0.3
    )

    price_conf = 0.0
    if change_pct is not None and direction != Direction.NEUTRAL:
        aligned = (change_pct > 0) if direction == Direction.BULLISH else (change_pct < 0)
        price_conf = min(1.0, abs(change_pct) / 2.0) if aligned else 0.0
    vol_conf = min(1.0, max(0.0, (rel_volume - 1.0))) if rel_volume is not None else 0.0
    flow_conf = 0.0
    if flow is not None and flow.decayed_sentiment is not None and direction != Direction.NEUTRAL:
        aligned = (flow.decayed_sentiment > 0) if direction == Direction.BULLISH else (flow.decayed_sentiment < 0)
        flow_conf = flow.confidence if aligned else 0.0

    points = (
        authority * w.source_authority + novelty * w.novelty + materiality * w.materiality
        + relevance * w.relevance + price_conf * w.price_confirmation
        + vol_conf * w.volume_confirmation + flow_conf * w.flow_confirmation
    )
    total = round(points / w.total, 4)
    expl = (
        f"source {authority:.2f}, novelty {novelty:.2f}, materiality {materiality:.2f}, "
        f"relevance {relevance:.2f}, price {price_conf:.2f}, vol {vol_conf:.2f}, flow {flow_conf:.2f}"
        + (" [DUPLICATE]" if is_dup else "")
    )
    return NewsScore(
        total=total, source_authority=round(authority, 3), novelty=round(novelty, 3),
        materiality=round(materiality, 3), relevance=round(relevance, 3),
        price_confirmed=round(price_conf, 3), volume_confirmed=round(vol_conf, 3),
        flow_confirmed=round(flow_conf, 3), is_duplicate=is_dup, direction=direction,
        explanation=expl,
    )


def best_news_score(
    items: list[NewsItem], *, for_symbol: str | None = None, change_pct: float | None = None,
    rel_volume: float | None = None, flow: FlowAnalysis | None = None,
    weights: NewsWeights | None = None,
) -> NewsScore | None:
    """Score the most material fresh headline for a symbol (dedup across the set)."""
    if not items:
        return None
    dedup = DedupState()
    best: NewsScore | None = None
    for it in sorted(items, key=lambda n: n.source_ts or n.received_ts, reverse=True):
        sc = score_news(
            it, for_symbol=for_symbol, change_pct=change_pct, rel_volume=rel_volume,
            flow=flow, dedup=dedup, weights=weights,
        )
        dedup.add(it.headline)
        if best is None or sc.total > best.total:
            best = sc
    return best
