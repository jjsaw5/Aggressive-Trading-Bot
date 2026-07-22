"""Reconstructed EOD flow proxy (flow_source = proxy_eod).

Historical flow-alerts are not available in our UW tier, so this rebuilds a crude
directional/urgency read of option flow from the per-contract `/historic` daily
fields, aggregated across a near-ATM chain slice per (underlying, day). It is a
PROXY: it lacks the opening-trade and single-vs-multi-leg discrimination of the
live alerts, so a result here motivates but never validates the live signal.

Flow is used only as a boolean gate (CONFIRM / NEUTRAL / OPPOSE) — never a score
pillar — per the experiment spec §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.domain.historic import HistoricOptionBar


@dataclass(frozen=True)
class FlowThresholds:
    lean: float
    sweep: float
    prem: float


@dataclass(frozen=True)
class FlowRaw:
    ask_vol: float
    bid_vol: float
    sweep_vol: float
    volume: float
    open_interest: float
    call_prem: float
    put_prem: float
    total_prem: float
    n_contracts: int


def aggregate_day(bars: list[HistoricOptionBar]) -> FlowRaw:
    """Sum the flow-side fields across a chain slice on a single trading day."""
    def s(attr) -> float:
        return float(sum(getattr(b, attr) or 0 for b in bars))
    call_prem = sum(b.total_premium or 0 for b in bars if b.option_type == "C")
    put_prem = sum(b.total_premium or 0 for b in bars if b.option_type == "P")
    return FlowRaw(
        ask_vol=s("ask_volume"), bid_vol=s("bid_volume"), sweep_vol=s("sweep_volume"),
        volume=s("volume"), open_interest=s("open_interest"),
        call_prem=float(call_prem), put_prem=float(put_prem),
        total_prem=float(call_prem + put_prem), n_contracts=len(bars),
    )


@dataclass(frozen=True)
class FlowFeatures:
    at_ask_lean: float  # (ask−bid)/(ask+bid) volume, [−1, 1]
    sweep_frac: float
    premium_z: float
    net_call_put: float  # (call−put) premium / total, [−1, 1]
    voloi: float

    @property
    def bull_score(self) -> float:
        """>0 bullish flow, <0 bearish (ask-lean + call/put premium skew)."""
        return self.at_ask_lean + self.net_call_put


def features(
    raw_by_date: dict[date, FlowRaw], d: date, *, window: int = 20
) -> FlowFeatures | None:
    """Features for date `d`, with premium_z against the trailing `window` days.
    None when the day has no usable chain read."""
    cur = raw_by_date.get(d)
    if cur is None or cur.n_contracts == 0:
        return None
    denom_lean = cur.ask_vol + cur.bid_vol
    at_ask_lean = (cur.ask_vol - cur.bid_vol) / denom_lean if denom_lean > 0 else 0.0
    sweep_frac = cur.sweep_vol / cur.volume if cur.volume > 0 else 0.0
    net_call_put = (cur.call_prem - cur.put_prem) / cur.total_prem if cur.total_prem > 0 else 0.0
    voloi = cur.volume / cur.open_interest if cur.open_interest > 0 else 0.0

    prior = sorted(dt for dt in raw_by_date if dt < d)[-window:]
    prems = [raw_by_date[dt].total_prem for dt in prior]
    if len(prems) >= 5:
        mean = sum(prems) / len(prems)
        var = sum((p - mean) ** 2 for p in prems) / (len(prems) - 1)
        sd = var ** 0.5
        premium_z = (cur.total_prem - mean) / sd if sd > 0 else 0.0
    else:
        premium_z = 0.0
    return FlowFeatures(
        at_ask_lean=round(at_ask_lean, 4), sweep_frac=round(sweep_frac, 4),
        premium_z=round(premium_z, 4), net_call_put=round(net_call_put, 4),
        voloi=round(voloi, 4),
    )


def flow_arm(feat: FlowFeatures | None, direction: str, thr: FlowThresholds) -> str:
    """CONFIRM / OPPOSE / NEUTRAL for a trade in `direction` ('bullish'/'bearish').

    A missing flow read is NEUTRAL by construction — never guessed."""
    if feat is None:
        return "NEUTRAL"
    magnitude = abs(feat.at_ask_lean) >= thr.lean and (
        feat.sweep_frac >= thr.sweep or feat.premium_z >= thr.prem
    )
    if not magnitude:
        return "NEUTRAL"
    agree = (feat.bull_score > 0 and direction == "bullish") or (
        feat.bull_score < 0 and direction == "bearish"
    )
    return "CONFIRM" if agree else "OPPOSE"
