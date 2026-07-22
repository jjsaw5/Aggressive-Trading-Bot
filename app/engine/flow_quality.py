"""Proprietary flow-quality metric — a *shadow* signal ported from the sibling
scanner (System A's ``_alert_quality``).

The sibling system scores each unusual-options print on conviction markers —
opening (vs closing) trades, sweep aggression, repeated prints at the same
strike, and volume-over-open-interest — then this module premium-weights those
per-print qualities into one number per symbol.

It is deliberately **observational only**. Nothing here feeds the composite
score or changes a single decision (the value lands in a `SignalScore.details`
entry, which the scorer never reads). The point is to record it alongside frozen
decisions so the forward-outcome ledger can measure — on *our* real option marks
— whether the metric actually separates winners from losers before we ever let
it touch scoring. If the ledger never validates it, it stays a footnote.

Faithful port, with two of the sibling's markers dropped because our `FlowAlert`
has no equivalent flag: ``has_multileg`` (−0.15) and ``has_singleleg is False``
(−0.05). Everything else maps onto real fields we already ingest.
"""

from __future__ import annotations

from collections import Counter

from app.domain.options import FlowAlert

# Per-print marker weights, mirroring the sibling scanner so a promoted metric
# stays comparable across the two systems. Base 0.5, clamped to [0, 1].
_BASE = 0.5
_W_OPENING = 0.20
_W_SWEEP = 0.10
_W_REPEATED = 0.15
_W_VOI_HIGH = 0.10  # volume/OI >= 1.0
_W_VOI_MED = 0.05  # volume/OI >= 0.3


def _vol_oi_ratio(alert: FlowAlert) -> float:
    if not alert.size or not alert.open_interest or alert.open_interest <= 0:
        return 0.0
    return alert.size / alert.open_interest


def _alert_quality(alert: FlowAlert, *, repeated: bool) -> float:
    """Conviction score in [0, 1] for a single print."""
    score = _BASE
    if alert.is_opening:
        score += _W_OPENING
    if alert.is_sweep:
        score += _W_SWEEP
    if repeated:
        score += _W_REPEATED
    voi = _vol_oi_ratio(alert)
    if voi >= 1.0:
        score += _W_VOI_HIGH
    elif voi >= 0.3:
        score += _W_VOI_MED
    return max(0.0, min(1.0, score))


def _print_key(alert: FlowAlert) -> tuple:
    return (alert.option_type, alert.strike, alert.expiration)


def proprietary_flow_quality(alerts: list[FlowAlert]) -> float | None:
    """Premium-weighted aggregate conviction of a symbol's flow, in [0, 1].

    A "repeated" print is one whose (type, strike, expiry) appears more than once
    in the batch — the sibling's "repeated" alert rule, reconstructed from prints
    rather than a provider flag. Returns None when there is no flow to grade.
    """
    if not alerts:
        return None

    key_counts = Counter(_print_key(a) for a in alerts)

    weighted_sum = 0.0
    total_weight = 0.0
    plain_sum = 0.0
    for a in alerts:
        repeated = key_counts[_print_key(a)] > 1
        q = _alert_quality(a, repeated=repeated)
        w = a.premium or 0.0
        weighted_sum += w * q
        total_weight += w
        plain_sum += q

    # Premium-weight when premium is known; otherwise fall back to an equal-weight
    # mean so a batch with no premium data still yields a real quality read.
    if total_weight > 0:
        return round(weighted_sum / total_weight, 4)
    return round(plain_sum / len(alerts), 4)
