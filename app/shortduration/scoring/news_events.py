"""Structured news-catalyst classification.

An INFORMATIONAL layer that sits alongside the keyword `NewsScore` (it never
approves a trade or bypasses a deterministic gate — setups come from the engines).
It turns a stream of raw headlines into typed `NewsCatalyst` events:

- **Event type** — earnings / guidance / rating-change / M&A / legal-regulatory /
  FDA-clinical / product / macro / other, from a keyword taxonomy.
- **Direction + mixed-outcome** — bullish / bearish / neutral, flagged `mixed` when
  a single event carries conflicting signals (e.g. "beats on revenue, misses on EPS").
- **Confidence** — reliability of the *classification*, from source authority + how
  cleanly the type and direction resolved + whether numeric values were parsed.
- **Before/after values** — light extraction of "actual vs estimate" numeric pairs
  and beat/miss language.
- **Source hierarchy + dedup grouping** — near-duplicate headlines are grouped into
  one event whose primary is the highest-authority, freshest member.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.domain.enums import Direction
from app.domain.shortduration import CatalystValue, NewsCatalyst, NewsItem
from app.shortduration.scoring.news import (
    _BEARISH_KW,
    _BULLISH_KW,
    _jaccard,
    _source_authority,
    _tokens,
)

# Event-type taxonomy — first match wins, most-specific first. Lowercased search.
_EVENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("earnings", re.compile(r"\b(earnings|eps|quarter\w*|q[1-4]\b|reported (?:results|profit)|results)\b", re.I)),
    ("guidance", re.compile(r"\b(guidance|outlook|forecast\w*|guides?|full[- ]year view)\b", re.I)),
    ("rating_change", re.compile(r"\b(upgrad\w*|downgrad\w*|initiat\w*|price target|\bpt\b|overweight|underweight|reiterat\w*|buy rating|sell rating|outperform|underperform)\b", re.I)),
    ("m_and_a", re.compile(r"\b(acqui\w*|merger\w*|m&a|buyout|takeover|to buy|stake|tender offer)\b", re.I)),
    ("legal_regulatory", re.compile(r"\b(lawsuit\w*|\bsec\b|\bdoj\b|prob\w*|investigat\w*|settlement|subpoena|antitrust|fine[ds]?|charg\w*)\b", re.I)),
    ("fda_clinical", re.compile(r"\b(fda|approval|phase [123i]+|clinical|trial\w*|drug|therap\w*|indication|efficacy)\b", re.I)),
    ("product", re.compile(r"\b(launch\w*|unveil\w*|release\w*|partnership|contract\w*|deal|award\w*|new product|rollout)\b", re.I)),
    ("macro", re.compile(r"\b(fed\b|fomc|cpi|inflation|jobs report|nonfarm|gdp|rate (?:hike|cut|decision)|tariff\w*)\b", re.I)),
]

_MIN = datetime.min.replace(tzinfo=UTC)  # sort/merge fallback for a missing timestamp

_BEAT_KW = re.compile(r"\b(beat\w*|top\w*|exceed\w*|surpass\w*|above (?:estimate|expectation|consensus))\b", re.I)
_MISS_KW = re.compile(r"\b(miss\w*|below (?:estimate|expectation|consensus)|short of|fell short)\b", re.I)
# "EPS of $1.23 vs $1.10" / "revenue of $5.2B vs. $5.0B (est)" — optional label + two numbers.
_VS_VALUE = re.compile(
    r"(?P<label>eps|earnings per share|revenue|sales|profit)?\s*(?:of|:)?\s*"
    r"\$?(?P<actual>\d+(?:\.\d+)?)\s*(?P<unit>[bmk%]?)\b[^.]*?"
    r"(?:vs\.?|versus|against|estimate[sd]?|est\.?|expected|consensus|forecast)[^\d\-]*"
    r"\$?(?P<estimate>\d+(?:\.\d+)?)",
    re.I,
)


def classify_event_type(text: str) -> str:
    for name, pat in _EVENT_PATTERNS:
        if pat.search(text):
            return name
    return "other"


def classify_direction_mixed(text: str) -> tuple[Direction, bool]:
    """(direction, mixed). Both bull + bear cues, or an explicit beat+miss, mark the
    event `mixed` (the outcome cuts both ways) and leave direction NEUTRAL."""
    bull = bool(_BULLISH_KW.search(text))
    bear = bool(_BEARISH_KW.search(text))
    beat = bool(_BEAT_KW.search(text))
    miss = bool(_MISS_KW.search(text))
    if (bull and bear) or (beat and miss):
        return Direction.NEUTRAL, True
    if bull or beat:
        return Direction.BULLISH, False
    if bear or miss:
        return Direction.BEARISH, False
    return Direction.NEUTRAL, False


def extract_values(text: str) -> list[CatalystValue]:
    out: list[CatalystValue] = []
    for m in _VS_VALUE.finditer(text):
        actual = float(m.group("actual"))
        estimate = float(m.group("estimate"))
        label = (m.group("label") or "value").strip().lower()
        label = {"earnings per share": "EPS", "eps": "EPS"}.get(label, label)
        unit = (m.group("unit") or "").upper()
        out.append(CatalystValue(
            label=label, actual=actual, estimate=estimate, unit=unit,
            beat=actual >= estimate,
        ))
    return out


def _classify_one(item: NewsItem) -> NewsCatalyst:
    text = f"{item.headline} {item.summary}".strip()
    event_type = classify_event_type(text)
    direction, mixed = classify_direction_mixed(text)
    values = extract_values(text)
    authority = _source_authority(item.source)
    # Confidence = how reliable the CLASSIFICATION is (not a trade signal).
    conf = 0.35 + 0.4 * authority
    if event_type != "other":
        conf += 0.12
    if values:
        conf += 0.1
    if direction == Direction.NEUTRAL and not mixed:
        conf -= 0.1  # couldn't read a clear direction
    confidence = round(max(0.0, min(1.0, conf)), 3)
    notes: list[str] = []
    if mixed:
        notes.append("mixed outcome — conflicting bullish/bearish signals")
    for v in values:
        if v.beat is not None:
            notes.append(f"{v.label} {v.actual:g}{v.unit} vs {v.estimate:g}{v.unit} est ({'beat' if v.beat else 'miss'})")
    ts = item.source_ts or item.received_ts
    return NewsCatalyst(
        symbol=item.symbol, event_type=event_type, direction=direction, mixed=mixed,
        confidence=confidence, headline=item.headline, source=item.source,
        source_authority=round(authority, 3), member_count=1,
        sources=[item.source] if item.source else [], values=values,
        first_seen=ts, last_seen=ts, notes=notes,
        explanation=f"{event_type} · {'mixed' if mixed else direction.value} · src {authority:.2f}",
    )


def _merge(primary: NewsCatalyst, other: NewsCatalyst) -> NewsCatalyst:
    """Fold a near-duplicate into the group, keeping the highest-authority/freshest
    headline as the primary and unioning sources, values, and time span."""
    keep_other_primary = (
        other.source_authority > primary.source_authority
        or (other.source_authority == primary.source_authority
            and (other.last_seen or _MIN) > (primary.last_seen or _MIN))
    )
    base = other if keep_other_primary else primary
    merged = base.model_copy(deep=True)
    merged.member_count = primary.member_count + other.member_count
    merged.sources = sorted({*primary.sources, *other.sources})
    # Prefer a decisive direction / a non-"other" type from either member.
    if merged.event_type == "other" and (primary.event_type != "other" or other.event_type != "other"):
        merged.event_type = next(e for e in (primary.event_type, other.event_type) if e != "other")
    merged.mixed = primary.mixed or other.mixed
    merged.confidence = max(primary.confidence, other.confidence)
    merged.first_seen = min(x for x in (primary.first_seen, other.first_seen) if x) if (primary.first_seen or other.first_seen) else None
    merged.last_seen = max(x for x in (primary.last_seen, other.last_seen) if x) if (primary.last_seen or other.last_seen) else None
    # Union values by label.
    by_label = {v.label: v for v in merged.values}
    for v in (*primary.values, *other.values):
        by_label.setdefault(v.label, v)
    merged.values = list(by_label.values())
    return merged


def build_news_catalysts(
    items: list[NewsItem], *, for_symbol: str | None = None, similarity: float = 0.6,
) -> list[NewsCatalyst]:
    """Classify + dedup-group a batch of headlines into typed catalysts, ranked by
    classification confidence (freshest as the tiebreak). Optionally filter to one
    symbol. Informational only — never gates or approves a trade."""
    pool = [
        it for it in items
        if not for_symbol or (it.symbol and it.symbol.upper() == for_symbol.upper())
    ]
    groups: list[tuple[frozenset[str], NewsCatalyst]] = []
    for it in sorted(pool, key=lambda n: n.source_ts or n.received_ts, reverse=True):
        toks = _tokens(it.headline)
        cat = _classify_one(it)
        placed = False
        for i, (gt, gc) in enumerate(groups):
            if toks and gt and _jaccard(toks, gt) >= similarity:
                groups[i] = (gt | toks, _merge(gc, cat))
                placed = True
                break
        if not placed:
            groups.append((toks, cat))
    out = [gc for _, gc in groups]
    out.sort(key=lambda c: (c.confidence, c.last_seen or _MIN), reverse=True)
    return out
