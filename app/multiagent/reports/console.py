"""Plain-text ranked trade report.

No dependencies beyond the standard library, because the report is the product
surface for this milestone and it should render in a terminal, a log, a file, or
a paste into a message without anything installed.

Three rendering rules the code enforces:

* **Absent renders as a sentinel, never as a blank or a zero.** `_num` returns
  `NA_no_data` rather than an empty cell, so a reader can tell "we did not
  measure this" from "this is small".
* **Modeled values are marked.** Greeks and probability of profit carry
  `(modeled)` wherever they appear.
* **Rejections are a section, not a footnote.** Hard rejections and low scores
  are listed separately, because "disqualified" and "did not score well enough"
  are different findings, and the second group is where the interesting
  post-hoc questions live.
"""

from __future__ import annotations

from app.multiagent.models.contracts import ProposedStructure
from app.multiagent.models.measurements import Provenance
from app.multiagent.models.report import RankedReport, RankedTrade, RejectedTrade

_WIDTH = 84
_RULE = "=" * _WIDTH
_THIN = "-" * _WIDTH


def _num(value: float | None, fmt: str = ".2f", prefix: str = "", suffix: str = "") -> str:
    if value is None:
        return "NA_no_data"
    return f"{prefix}{value:{fmt}}{suffix}"


def _pct(value: float | None, fmt: str = ".1f") -> str:
    return "NA_no_data" if value is None else f"{value:{fmt}}%"


def _frac_as_pct(value: float | None, fmt: str = ".1f") -> str:
    return "NA_no_data" if value is None else f"{value * 100:{fmt}}%"


def render_report(report: RankedReport, *, show_audit: bool = True) -> str:
    out: list[str] = []
    a = out.append

    a(_RULE)
    a("MULTI-AGENT OPTIONS RESEARCH — RANKED TRADE REPORT")
    a(_RULE)
    a(f"Run:            {report.run_id}")
    a(f"Generated:      {report.generated_at.isoformat()}")
    a(f"Stage:          {report.stage.value}")
    a(f"Methodology:    {report.methodology_version}")
    a(f"Agent runner:   {report.diagnostics.agent_runner}")
    a(f"Score status:   {report.calibration_status.value}")
    a("")
    a(
        "  Scores are a deterministic rubric, not a validated predictor. No feature in this"
    )
    a(
        "  repository has cleared out-of-sample validation (docs/PRODUCT_STANCE.md), so every"
    )
    a("  score reads UNCALIBRATED. A high score means 'scores well on the rubric', not")
    a("  'likely to make money'. THIS SYSTEM PLACES NO ORDERS.")
    if not report.contracts_finalised:
        a("")
        a("  !! CONTRACTS NOT FINALISED")
        for line in _wrap(report.stage_note, 78):
            a(f"     {line}")
    a("")

    out.extend(_market_summary(report))
    out.extend(_top_trades(report, show_audit=show_audit))
    out.extend(_rejected(report))
    out.extend(_diagnostics(report))
    return "\n".join(out)


def _market_summary(report: RankedReport) -> list[str]:
    b = report.brief
    out = [_RULE, "MARKET SUMMARY", _RULE]
    out.append(f"Regime:         {b.market_regime.value}")
    out.append(f"Volatility:     {b.volatility_regime.value}")

    for label, idx in (("SPY", b.spy), ("QQQ", b.qqq), ("IWM", b.iwm)):
        if idx is None:
            out.append(f"{label}:            NA_no_data (not retrieved)")
            continue
        out.append(
            f"{label}:            {_num(idx.price, prefix='$')}  "
            f"chg {_pct(idx.change_pct, '+.2f')}  "
            f"20d {_pct(idx.trailing_20d_return_pct, '+.2f')}  "
            f"bias={idx.bias.value}"
        )
    vix = b.vix
    out.append(
        f"VIX:            {_num(vix.level)}  regime={vix.regime.value}"
        + (f"  ({vix.commentary})" if vix.commentary else "")
    )
    out.append("")

    if b.summary:
        out.append("Summary:")
        out.extend(f"  {line}" for line in _wrap(b.summary, 80))
        out.append("")

    if b.upcoming_scheduled_events:
        out.append("Upcoming scheduled events:")
        for e in b.upcoming_scheduled_events[:8]:
            when = e.scheduled_at.isoformat() if e.scheduled_at else "NA_no_data"
            out.append(
                f"  - {e.name} ({e.catalyst_type.value}) {when} importance={e.importance.value}"
            )
            if e.consensus is None and e.previous is None:
                out.append("      consensus/previous: NA_no_data (not published)")
            else:
                out.append(
                    f"      consensus={_num(e.consensus, '.4g')} previous={_num(e.previous, '.4g')}"
                )
        out.append("")

    if b.risk_events:
        out.append("Major event risks:")
        for r in b.risk_events[:6]:
            out.append(f"  - {r.name}: {r.description}")
        out.append("")

    if b.company_catalysts:
        out.append(f"Company catalysts identified: {len(b.company_catalysts)}")
        for c in b.company_catalysts[:8]:
            when = c.published_at.date().isoformat() if c.published_at else "undated"
            out.append(
                f"  - {c.ticker:<6} {c.catalyst_type.value:<18} {c.expected_direction.value:<8} "
                f"{c.evidence_quality.value:<15} {when}"
            )
            out.append(f"      {c.headline[:74]}")
            out.append(f"      source: {c.source} {c.source_url or ''}")
        out.append("")
    return out


def _structure_block(s: ProposedStructure | None, indent: str = "  ") -> list[str]:
    if s is None:
        return [f"{indent}No contract selected at this stage."]
    out: list[str] = []
    modeled = " (modeled)" if s.greeks_source is Provenance.MODELED else ""
    out.append(f"{indent}Structure:      {s.describe()}")
    out.append(f"{indent}Underlying:     {_num(s.underlying_price, prefix='$')}")
    for leg in s.legs:
        out.append(
            f"{indent}  {leg.action.value:<13} {leg.option_type.value:<4} "
            f"{leg.strike:<9g} exp {leg.expiration.isoformat()}  "
            f"bid {_num(leg.bid)} / ask {_num(leg.ask)}  "
            f"vol {leg.volume if leg.volume is not None else 'NA_no_data'}  "
            f"OI {leg.open_interest if leg.open_interest is not None else 'NA_no_data'}  "
            f"IV {_frac_as_pct(leg.implied_volatility)}  "
            f"delta {_num(leg.greeks.delta, '.3f')}{modeled}"
        )
    out.append(
        f"{indent}Debit:          {_num(s.net_debit_per_share, prefix='$')}/share  "
        f"({_num(s.total_cost, prefix='$')} for {s.contracts} contract(s))"
    )
    out.append(
        f"{indent}Max loss:       {_num(s.total_max_loss, prefix='$')}    "
        f"Max profit: {_num(s.total_max_profit, prefix='$') if s.max_profit_per_contract is not None else 'unbounded (long option)'}"
    )
    out.append(
        f"{indent}Breakeven:      {_num(s.breakeven, prefix='$')}    "
        f"Reward/risk: {_num(s.reward_to_risk, '.2f')}"
    )
    out.append(
        f"{indent}Net greeks:     delta {_num(s.net_delta, '.3f')}  gamma {_num(s.net_gamma, '.4f')}  "
        f"theta {_num(s.net_theta, '.4f')}  vega {_num(s.net_vega, '.4f')}{modeled}"
    )
    out.append(
        f"{indent}Spread (worst): {_frac_as_pct(s.worst_leg_spread_pct)}    "
        f"Cost drag: {_frac_as_pct(s.cost_drag_pct)} of max loss"
    )
    out.append(
        f"{indent}P(profit):      {_frac_as_pct(s.probability_of_profit)} (modeled from IV, "
        "not a forecast)"
    )
    return out


def _top_trades(report: RankedReport, *, show_audit: bool) -> list[str]:
    out = [_RULE, f"TOP TRADES ({len(report.ranked)})", _RULE]
    if not report.ranked:
        out.append("")
        out.append("No candidate cleared both the hard rules and the minimum score.")
        out.append("This is a valid outcome, not a failure. See the rejected section for why.")
        out.append("")
        return out

    for t in report.ranked:
        out.append("")
        out.append(
            f"#{t.rank} {t.candidate.ticker} {t.candidate.strategy_type.value.replace('_', ' ').title()}"
        )
        out.append(
            f"    Score: {t.score.score:.0f}/100   Classification: {t.classification_name}   "
            f"[{report.calibration_status.value}]   Input coverage: {t.score.input_coverage:.0%}"
        )
        out.append(_THIN)
        out.extend(_structure_block(t.validation.selected_structure(), indent="    "))
        out.append("")
        out.append(f"    Direction:      {t.candidate.direction.value}")
        out.append(f"    Hold:           {t.candidate.expected_holding_period.value}")
        out.append("    Catalyst:")
        for line in _wrap(t.candidate.primary_catalyst, 72):
            out.append(f"      {line}")
        cv = t.validation.catalyst
        if cv:
            out.append(
                f"      [validated: {cv.verdict.value}, {len(cv.resolved_evidence_ids)} evidence "
                f"item(s), newest {_num(cv.newest_evidence_age_days, '.1f', suffix='d')} old]"
            )
        out.append("    Thesis:")
        for line in _wrap(t.candidate.thesis, 72):
            out.append(f"      {line}")

        tech = t.validation.technical
        if tech:
            out.append(
                f"    Technical:      trend={tech.trend_bias.value}  "
                f"price={_num(tech.price, prefix='$')}  "
                f"relvol={_num(tech.measurements.value('relative_volume'), '.2f', suffix='x')}  "
                f"ATR%={_num(tech.measurements.value('atr_pct'), '.2f', suffix='%')}"
            )
        fl = t.validation.flow
        if fl:
            out.append(f"    Options flow:   {fl.verdict.value} — {fl.interpretation or 'no reading'}")
            for c in fl.caveats[:2]:
                for line in _wrap(f"caveat: {c}", 68):
                    out.append(f"      {line}")

        if t.entry_conditions:
            out.append("    Entry conditions:")
            for e in t.entry_conditions:
                for line in _wrap(e, 70):
                    out.append(f"      - {line}" if line == _wrap(e, 70)[0] else f"        {line}")
        if t.profit_targets:
            out.append("    Profit targets:")
            for p in t.profit_targets:
                out.append(f"      - {p}")
        if t.invalidation:
            out.append("    Invalidation:")
            for line in _wrap(t.invalidation, 70):
                out.append(f"      {line}")
        if t.risks:
            out.append("    Risks:")
            for r in t.risks[:8]:
                for i, line in enumerate(_wrap(r, 70)):
                    out.append(f"      - {line}" if i == 0 else f"        {line}")
        if t.warnings:
            out.append("    Warnings:")
            for w in t.warnings:
                for i, line in enumerate(_wrap(w, 70)):
                    out.append(f"      ! {line}" if i == 0 else f"        {line}")

        out.append("")
        out.append("    Score breakdown:")
        for category, summary in t.score.breakdown().items():
            out.append(f"      {category:<22} {summary}")
        if show_audit:
            out.append("")
            out.append("    Full audit (every point traced to a measurement):")
            for line in t.score.audit_lines():
                out.append(f"      {line}")
        out.append("")
    return out


def _rejected(report: RankedReport) -> list[str]:
    hard = [r for r in report.rejected if r.hard_rejected]
    soft = [r for r in report.rejected if not r.hard_rejected]
    out = [_RULE, f"REJECTED CANDIDATES ({len(report.rejected)})", _RULE]
    out.append("")
    out.append("All rejected candidates are stored with the data that produced them, so the")
    out.append("question 'how often did rejected trades actually work?' stays answerable later.")
    out.append("")

    if hard:
        out.append(f"-- Hard rejections ({len(hard)}) — disqualified regardless of score --")
        out.extend(_rejection_lines(hard))
        out.append("")
    if soft:
        out.append(f"-- Below the score threshold ({len(soft)}) — not disqualified, just not good enough --")
        out.extend(_rejection_lines(soft))
        out.append("")
    if not report.rejected:
        out.append("None.")
        out.append("")
    return out


def _rejection_lines(items: list[RejectedTrade]) -> list[str]:
    out: list[str] = []
    for r in sorted(items, key=lambda x: -(x.score.score if x.score else 0.0)):
        score = f"{r.score.score:.0f}/100" if r.score else "not scored"
        coverage = f" cov {r.score.input_coverage:.0%}" if r.score else ""
        out.append(f"  {r.candidate.ticker:<6} {r.candidate.strategy_type.value:<18} {score:>10}{coverage}")
        for reason in r.rejection_reasons:
            for i, line in enumerate(_wrap(reason, 72)):
                out.append(f"      - {line}" if i == 0 else f"        {line}")
    return out


def _diagnostics(report: RankedReport) -> list[str]:
    d = report.diagnostics
    out = [_RULE, "RUN DIAGNOSTICS", _RULE]
    out.append(f"Duration:              {_num(d.duration_seconds, '.2f', suffix='s')}")
    out.append(f"Evidence items:        {d.evidence_items}")
    out.append(f"Symbols examined:      {d.symbols_examined}")
    out.append(f"Candidates generated:  {d.candidates_generated}")
    out.append(f"Candidates validated:  {d.candidates_validated}")
    out.append(f"Providers used:        {', '.join(d.providers_used) or 'none'}")
    out.append("")

    if d.provider_errors:
        out.append("Provider errors (gaps are reported, never filled):")
        for k, v in d.provider_errors.items():
            out.append(f"  - {k}: {v}")
        out.append("")
    if d.dropped_agent_claims:
        out.append(f"Agent claims dropped for unresolvable evidence ({len(d.dropped_agent_claims)}):")
        for c in d.dropped_agent_claims[:10]:
            out.append(f"  - {c}")
        out.append("")
    if d.data_gaps:
        out.append("Data gaps:")
        for g in d.data_gaps[:12]:
            for i, line in enumerate(_wrap(g, 76)):
                out.append(f"  - {line}" if i == 0 else f"    {line}")
        out.append("")

    out.append(_THIN)
    out.append("No orders were placed. This system has no order-placement code path and no")
    out.append("agent is given an execution tool. Every recommendation requires human review.")
    out.append(_RULE)
    return out


def _wrap(text: str, width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_summary_line(t: RankedTrade) -> str:
    return (
        f"#{t.rank} {t.candidate.ticker} {t.candidate.strategy_type.value} "
        f"{t.score.score:.0f}/100 {t.classification_name}"
    )
