"""Signal-audit export: the decision record, with its gaps left visible.

Produces the four deliverables of the signal-audit spec from the decision
warehouse. The governing rule is the spec's own: anything the system cannot
produce is reported as a gap, never approximated. Every unavailable field emits
an explicit sentinel — NA_not_implemented (the system has no such concept),
NA_no_data (the concept exists but this row lacks it), or NA_unresolved — so a
reader can always tell a missing measurement from a zero.

Two structural facts a reader needs before interpreting anything:

  * There are no "high conviction" signals to export. Conviction is gated (see
    shortduration.conviction_gate) and the gate is RED — 3 of 5 criteria fail.
    Every decision here carries conviction_status=UNCALIBRATED. This export is
    therefore the highest-SCORING signals, which is a different claim.

  * Market context at signal time (§1C) was largely never captured. The
    warehouse froze the prediction, not the order book. NBBO, realized vol, VRP,
    term slope, volume/OI and MFE/MAE have no stored values for any historical
    row, and no amount of recomputation can recover a point-in-time quote.

Usage:
    python scripts/export_signal_audit.py --out audit_export/
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.analytics.calibration import eligible_for_calibration, select_pnl_outcomes
from app.db import repository

_ET = ZoneInfo("America/New_York")

NOT_IMPL = "NA_not_implemented"
NO_DATA = "NA_no_data"
UNRESOLVED = "NA_unresolved"

# The spec's §1B asks for per-component name/raw/weight/direction. The scorer
# carries at most 8 factors per model, so 8 fixed slots cover every row.
MAX_COMPONENTS = 8


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 — a missing SHA is itself reportable
        return NO_DATA


def _bucket(dte: int | None) -> str:
    if dte is None:
        return NO_DATA
    return "0DTE" if dte <= 0 else ("1-5DTE" if dte <= 5 else "LONG")


def _tod_bucket(ts: datetime) -> str:
    """ET session segment. Signals fire against the ET session, so the bucket is
    computed in ET regardless of how the timestamp is stored."""
    et = ts.astimezone(_ET)
    m = et.hour * 60 + et.minute
    if m < 570 or m >= 960:  # outside 09:30-16:00
        return "outside_rth"
    if m < 600:
        return "open"          # first 30m
    if m < 720:
        return "morning"
    if m < 900:
        return "midday"
    if m < 945:
        return "power_hour"
    return "close"             # last 15m


def _legs_str(plan) -> str:
    if plan is None or not plan.legs:
        return NO_DATA
    parts = []
    for lg in plan.legs:
        side = "B" if lg.action.value.startswith("buy") else "S"
        r = "C" if lg.option_type.value == "call" else "P"
        parts.append(f"{side}{lg.quantity}x{lg.strike:g}{r}@{lg.expiration}")
    return " / ".join(parts)


def _f(v, nd: int = 6):
    return NO_DATA if v is None else round(float(v), nd)


def _primary_leg(mc):
    """The long leg the flat market-context columns describe.

    A debit structure's identity sits in the leg it is long; the short leg is a
    financing choice. Falls back to the first leg so a row is never silently
    empty when the signs are unexpected.
    """
    if mc is None or not mc.legs:
        return None
    longs = [lg for lg in mc.legs if lg.signed_quantity > 0]
    return longs[0] if longs else mc.legs[0]


def _mcf(mc, field: str, *, leg: bool = False):
    """One market-context field, or NA_no_data. Never blank, never zero-filled.

    `leg=True` reads from the primary long leg instead of the structure. A row
    predating the MarketContext returns NO_DATA, not NOT_IMPL — the capability
    exists, that row just never captured it.
    """
    src = _primary_leg(mc) if leg else mc
    if src is None:
        return NO_DATA
    v = getattr(src, field, None)
    if v is None or v == "":
        return NO_DATA
    return round(v, 6) if isinstance(v, float) else v


def _legs_json(mc) -> str:
    """Every leg's full quote (item 1.1 is per LEG, not per structure)."""
    if mc is None or not mc.legs:
        return NO_DATA
    return json.dumps([
        {
            "strike": lg.strike, "type": lg.option_type.value,
            "expiration": lg.expiration.isoformat(), "qty": lg.signed_quantity,
            "bid": lg.bid, "ask": lg.ask, "mid": lg.mid, "spread": lg.spread,
            "volume": lg.volume, "open_interest": lg.open_interest,
            "iv": lg.implied_volatility,
            "delta": lg.delta, "gamma": lg.gamma, "theta": lg.theta, "vega": lg.vega,
            "greeks_source": lg.greeks_source or None,
            "quote_source": lg.quote_source or None,
        }
        for lg in mc.legs
    ], separators=(",", ":"))


def _net_greek(mc, name: str):
    """Structure-level Greek from the modeled per-leg values.

    All-or-nothing: one unpriced leg makes the sum NO_DATA rather than a partial
    total that looks like a complete measurement.
    """
    if mc is None or not mc.legs:
        return NO_DATA
    vals = [getattr(lg, name, None) for lg in mc.legs]
    if any(v is None for v in vals):
        return NO_DATA
    return round(
        sum(v * lg.signed_quantity for v, lg in zip(vals, mc.legs, strict=True)), 6
    )


def _greeks_source(mc) -> str:
    """Provenance of the Greeks. Never blank: an unlabeled Greek is the failure
    mode this column exists to prevent."""
    if mc is None or not mc.legs:
        return NO_DATA
    sources = {lg.greeks_source for lg in mc.legs if lg.greeks_source}
    if not sources:
        return NO_DATA
    return ";".join(sorted(sources))


def _signal_row(s, o, cand, sha: str) -> dict:
    plan = s.trade_plan
    risk = plan.risk if plan else None
    bucket = _bucket(s.dte_at_entry)
    is_live = s.source.value == "live"
    mc = getattr(s, "market_context", None)

    row = {
        # --- 1A identification ---
        "signal_id": s.decision_id,
        "signal_ts": s.generated_at.astimezone(_ET).isoformat(),
        "dte_bucket": bucket,
        "underlying": s.symbol,
        "strategy_type": s.strategy.value,
        "contract_details": _legs_str(plan),
        "dte_actual": s.dte_at_entry if s.dte_at_entry is not None else NO_DATA,
        # The build that PRODUCED the signal was not recorded; this is the build
        # that produced the EXPORT. scoring_model_version is the real lineage.
        "scanner_version": s.scoring_model_version or NO_DATA,
        "export_git_sha": sha,
        "signal_source": s.source.value,
        "conviction_status": "UNCALIBRATED",  # gate is red for every row; see memo §7
        # --- 1B score breakdown ---
        "composite_score": _f(s.composite_score, 4),
        "predicted_pop": _f(s.probability_of_profit, 4),
        "predicted_pop_source": s.pop_source or NO_DATA,
        "weights_sum": NO_DATA,
        # --- 1C market context ---
        # Populated from the frozen MarketContext (Phase 1). Rows captured before
        # that field existed report NA_no_data, NOT NA_not_implemented: the
        # concept exists now, those rows simply never recorded it. Keeping the
        # distinction is the point — it makes the capture boundary visible.
        "spot_price": _f(s.entry_spot, 4),
        "option_bid": _mcf(mc, "bid", leg=True),
        "option_ask": _mcf(mc, "ask", leg=True),
        # No provider in the stack supplies a consolidated mark. Mid is the
        # midpoint of a real two-sided book; a "mark" would be an invention.
        "option_mark": NOT_IMPL,
        "option_mid": _mcf(mc, "mid", leg=True),
        "spread_pct": _mcf(mc, "spread_pct_of_mid", leg=True),
        "cost_drag_ratio": _mcf(mc, "cost_drag_ratio"),
        "round_trip_cost_usd": _mcf(mc, "round_trip_cost_usd"),
        "iv": _f(s.entry_iv, 6) if s.entry_iv is not None else NO_DATA,
        "iv_rank_252d": _f(s.iv_rank, 6) if s.iv_rank is not None else NO_DATA,
        "iv_percentile": _mcf(mc, "iv_percentile"),
        "iv_rank_source": _mcf(mc, "iv_rank_source"),
        "iv_skew": _mcf(mc, "iv_skew"),
        "implied_move_to_expiry": _mcf(mc, "implied_move_pct"),
        "implied_move_usd": _mcf(mc, "implied_move_usd"),
        "realized_vol_20d": _mcf(mc, "realized_vol_20d"),
        # "VRP" is ambiguous in the literature, so both conventions ship named.
        "vrp": _mcf(mc, "vrp_points"),
        "vrp_ratio": _mcf(mc, "vrp_ratio"),
        "term_slope": _mcf(mc, "term_structure_slope"),
        "volume": _mcf(mc, "volume", leg=True),
        "open_interest": _mcf(mc, "open_interest", leg=True),
        "earnings_days_away": _mcf(mc, "earnings_days_away"),
        "time_of_day_bucket": _tod_bucket(s.generated_at),
        # --- 1C(ii) per-leg detail (item 1.1) ---
        # The flat columns above describe the PRIMARY LONG leg. A spread has more
        # than one book and collapsing them loses the thing being measured, so
        # every leg's full quote ships as JSON alongside.
        "legs_nbbo_json": _legs_json(mc),
        # --- 1C(iii) Greeks — MODELED, and labeled as such (item 1.8) ---
        "net_delta_modeled": _f(mc.net_delta, 4) if mc and mc.net_delta is not None else NO_DATA,
        "greeks_source": _greeks_source(mc),
        # --- 1C(iv) regime (item 1.11) ---
        "regime_tag": mc.regime_tag if mc else NO_DATA,
        "regime_vol": mc.regime_vol if mc else NO_DATA,
        "regime_tape": mc.regime_tape if mc else NO_DATA,
        # --- 1C(v) provenance (item 1.10) ---
        # The build that PRODUCED this signal, as opposed to export_git_sha which
        # is the build that produced the CSV.
        "signal_build_sha": (mc.signal_build_sha or NO_DATA) if mc else NO_DATA,
        # --- 1D entry/exit assumptions ---
        "entry_price_basis": "actual_fill" if is_live else "modeled_mid",
        "entry_price": _f(s.entry_net_per_share, 4),
        "slippage_model": (
            "none_real_fill" if is_live
            else "commission_only; mid-to-mid marks, no synthetic slippage added"
        ),
        "profit_definition": (
            "realized_close" if o.outcome_source == "live_close"
            else "managed_exit_first_trigger" if o.outcome_source == "managed_policy"
            else o.outcome_source
        ),
        "exit_rule": (
            f"target +{risk.profit_target_pct:.0%} of debit OR stop "
            f"-{risk.stop_loss_pct:.0%} of debit OR time stop at "
            f"{risk.time_stop_dte} DTE, whichever first; stop checked BEFORE target"
            if risk and o.outcome_source == "managed_policy"
            else "discretionary human close" if is_live else NO_DATA
        ),
        # --- 1E resolved outcome ---
        "outcome": o.result.value,
        # The MEASURED exit instant when the grade replayed minute bars;
        # otherwise the moment the grade was computed, which is not the same
        # thing and must not be read as one.
        "exit_ts": (o.exit_ts or o.resolved_at).astimezone(_ET).isoformat(),
        "exit_ts_is_measured": bool(o.exit_ts),
        "exit_price_basis": "actual_fill" if is_live else "modeled_mid",
        "exit_price": _f(o.exit_price_per_share, 4) if o.exit_price_per_share is not None else NO_DATA,
        "exit_reason": o.exit_reason or NO_DATA,
        "pnl_usd_net": _f(o.realized_pnl_usd, 2),
        "pnl_usd_gross": _f(o.realized_pnl_gross_usd, 2),
        "costs_usd": _f(o.costs_usd, 2),
        "pnl_pct": NO_DATA,
        "r_multiple": NO_DATA,
        # Excursion BOUNDS, per share as a fraction of entry. A minute bar's
        # high and low have no ordering, so these are the best/worst the
        # structure could have shown, not prices that were certainly available.
        "mfe": _f(o.mfe_per_share, 4) if o.mfe_per_share is not None else NO_DATA,
        "mae": _f(o.mae_per_share, 4) if o.mae_per_share is not None else NO_DATA,
        "mfe_ts": o.mfe_ts.astimezone(_ET).isoformat() if o.mfe_ts else NO_DATA,
        "mae_ts": o.mae_ts.astimezone(_ET).isoformat() if o.mae_ts else NO_DATA,
        "bars_observed": o.bars_observed if o.bars_observed else NO_DATA,
        "hold_minutes": NO_DATA,
        "elapsed_days": o.elapsed_days if o.elapsed_days is not None else NO_DATA,
        "outcome_source": o.outcome_source,
        # --- bucket-specific ---
        "session_segment_score": NOT_IMPL,
        "gex_proxy": NOT_IMPL,
        # H4's headline figure (pre-registration sec 5.4/6): expectancy must
        # survive this before live capital is permitted.
        "pnl_at_1tick_worse": (
            _f(o.pnl_at_1tick_worse_usd, 2)
            if o.pnl_at_1tick_worse_usd is not None else NO_DATA
        ),
        "pnl_at_half_spread_worse": (
            _f(o.pnl_at_half_spread_worse_usd, 2)
            if o.pnl_at_half_spread_worse_usd is not None else NO_DATA
        ),
        "cost_stress_source": o.cost_stress_source or NO_DATA,
        # Structure-level theta/vega, summed from the MODELED per-leg Greeks (see
        # greeks_source). Sum is None-safe by construction: net_theta/net_vega
        # report NO_DATA unless every leg priced.
        "theta_at_entry": _net_greek(mc, "theta"),
        "vega_at_entry": _net_greek(mc, "vega"),
    }

    # pnl_pct / r_multiple: both are P&L over defined risk for a debit structure,
    # so they coincide here. Reported separately anyway so the spec's columns are
    # answerable rather than silently merged.
    denom = abs(s.max_loss_usd) if s.max_loss_usd else None
    if denom and o.realized_pnl_usd is not None:
        row["pnl_pct"] = round(o.realized_pnl_usd / denom, 4)
        row["r_multiple"] = round(o.realized_pnl_usd / denom, 4)

    # hold_minutes: only elapsed DAYS were stored, so minutes are recoverable
    # exactly for same-session resolutions and not at all otherwise.
    # A measured hold beats a derived one: exit_ts is when the position actually
    # closed, resolved_at is merely when the grader ran.
    if o.hold_minutes is not None:
        row["hold_minutes"] = o.hold_minutes
    else:
        span = ((o.exit_ts or o.resolved_at) - s.generated_at).total_seconds() / 60.0
        if span >= 0:
            row["hold_minutes"] = round(span)

    # --- component breakdown, where a scorecard survives ---
    factors = (cand.scorecard.factors if cand and cand.scorecard else None) or []
    weights = (cand.scorecard.weights if cand and cand.scorecard else None) or {}
    if weights:
        row["weights_sum"] = round(sum(float(w) for w in weights.values()), 4)
    for i in range(1, MAX_COMPONENTS + 1):
        f = factors[i - 1] if i <= len(factors) else None
        row[f"component_{i}_name"] = f.key if f else (NO_DATA if is_live else NO_DATA)
        row[f"component_{i}_raw"] = _f(f.raw, 4) if f else NO_DATA
        row[f"component_{i}_weight"] = _f(f.weight, 4) if f else NO_DATA
        # Every scoring component is constructed so a HIGHER raw value is more
        # favourable — points = raw * weight with no sign flips anywhere in
        # scoring/components.py. Direction is therefore a property of the model,
        # not of the row; it is emitted per row so the inverted-scoring class of
        # bug stays checkable from the CSV alone.
        row[f"component_{i}_direction"] = "higher_raw_increases_score" if f else NO_DATA
    return row


def _calibration_rows(pairs, use_score: bool) -> list[dict]:
    """Deciles of predicted_pop, or of composite_score when POP is absent."""
    bins: dict[tuple[str, str], list] = defaultdict(list)
    for s, o in pairs:
        v = s.composite_score if use_score else s.probability_of_profit
        if v is None:
            continue
        lo = min(9, max(0, int(float(v) * 10)))
        bins[(_bucket(s.dte_at_entry), f"{lo * 10}-{lo * 10 + 10}")].append((s, o, float(v)))

    out = []
    for (bucket, label), rows in sorted(bins.items()):
        n = len(rows)
        wins = [r for r in rows if r[1].result.value == "win"]
        losses = [r for r in rows if r[1].result.value == "loss"]
        decisive = len(wins) + len(losses)

        def _avg_r(subset):
            rs = [
                (o.realized_pnl_usd / abs(s.max_loss_usd))
                for s, o, _v in subset
                if o.realized_pnl_usd is not None and s.max_loss_usd
            ]
            return round(sum(rs) / len(rs), 4) if rs else NO_DATA

        win_rate = round(len(wins) / decisive, 4) if decisive else NO_DATA
        aw, al = _avg_r(wins), _avg_r(losses)
        exp_r = NO_DATA
        if isinstance(win_rate, float) and isinstance(aw, float) and isinstance(al, float):
            exp_r = round(win_rate * aw + (1 - win_rate) * al, 4)
        out.append({
            "dte_bucket": bucket,
            "pop_bin": label,
            "n_signals": n,
            "n_decisive": decisive,
            "predicted_pop_mean": round(sum(r[2] for r in rows) / n, 4),
            "actual_win_rate": win_rate,
            "avg_win_r": aw,
            "avg_loss_r": al,
            "expectancy_r": exp_r,
            "sample_sufficient": "yes" if decisive >= 20 else "NO_insufficient_sample",
        })
    return out


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="audit_export", help="output directory")
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sha = _git_sha()

    snaps, outs = await asyncio.to_thread(repository.fetch_calibration_data, args.limit)
    snaps, n_excluded = eligible_for_calibration(snaps)
    by_id = {s.decision_id: s for s in snaps}
    pairs = [(by_id[i], o) for i, o in select_pnl_outcomes(outs).items() if i in by_id]
    pairs.sort(key=lambda so: so[0].generated_at, reverse=True)

    rows, forward = [], []
    for s, o in pairs:
        cand = None
        if not s.decision_id.startswith("live:"):
            cid = s.decision_id.split(":", 1)[1] if ":" in s.decision_id else s.decision_id
            cand = await asyncio.to_thread(repository.get_short_duration_candidate, cid)
        r = _signal_row(s, o, cand, sha)
        rows.append(r)
        if s.source.value == "live":
            fr = dict(r)
            fr["execution_mode"] = "live"
            fr["actual_fill_price"] = r["entry_price"]
            # The backtest engine cannot re-score a live trade: no scorecard, and
            # no stored point-in-time chain to re-price against.
            fr["backtest_expected_pnl"] = NOT_IMPL
            forward.append(fr)

    _write_csv(out / "signals_export.csv", rows)
    _write_csv(out / "forward_log.csv", forward)

    use_score = sum(1 for s, _ in pairs if s.probability_of_profit is not None) < len(pairs) / 2
    cal = _calibration_rows(pairs, use_score=use_score)
    _write_csv(out / ("calibration_by_composite_score.csv" if use_score else "calibration.csv"),
               cal)

    counts: dict[str, int] = defaultdict(int)
    for s, _o in pairs:
        counts[_bucket(s.dte_at_entry)] += 1
    print(f"--- signal audit export -> {out}/ ---")
    print(f"resolved signals exported: {len(rows)}  (pre-v3 excluded: {n_excluded})")
    for k in ("0DTE", "1-5DTE", "LONG"):
        flag = "" if counts[k] >= 30 else "   << under the 30-row minimum"
        print(f"  {k:8s} {counts[k]:4d}{flag}")
    print(f"forward_log rows (live fills): {len(forward)}")
    print(f"calibration binned on: {'composite_score (POP absent on most rows)' if use_score else 'predicted_pop'}")
    print(f"calibration rows: {len(cal)}")


if __name__ == "__main__":
    asyncio.run(main())
