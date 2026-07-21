"""Alert routing and candidate → alert construction.

Alerts fire only for ACTIONABLE candidates at or above `ALERTS_MIN_SCORE`, and
only when `ALERTS_ENABLED` is true. Disabled by default → NoopNotifier.
"""

from __future__ import annotations

from app.alerts.base import Alert, Notifier, Severity
from app.alerts.notifiers import ConsoleNotifier, NoopNotifier, SlackNotifier
from app.config import settings
from app.domain.candidates import TradeCandidate
from app.logging_config import get_logger

log = get_logger(__name__)


def get_notifier() -> Notifier:
    if not settings.alerts_enabled:
        return NoopNotifier()
    channel = settings.alerts_channel.lower()
    if channel == "slack":
        if not settings.slack_webhook_url:
            log.warning("alerts_slack_no_webhook_fallback_console")
            return ConsoleNotifier()
        return SlackNotifier(settings.slack_webhook_url)
    if channel == "console":
        return ConsoleNotifier()
    return NoopNotifier()


def candidate_to_alert(candidate: TradeCandidate) -> Alert:
    plan = candidate.trade_plan
    parts: list[str] = [candidate.thesis.why_now]
    if plan is not None:
        r = plan.risk
        parts.append(
            f"{plan.strategy.display_name} x{plan.contracts} | risk ${r.max_loss_usd:.0f} "
            f"({r.account_risk_pct:.1%})"
        )
        # Entry economics as a mini-ticket so the alert is actionable on its own.
        ep = plan.exit_plan
        if ep is not None:
            net = ep.entry_net_per_share
            kind = "credit" if net < 0 else "debit"
            parts.append(f"{kind} ${abs(net):.2f}")
            tp = next((lv for lv in ep.levels if lv.kind == "take_profit"), None)
            if tp is not None and tp.net_price is not None:
                parts.append(f"TP ${tp.net_price:.2f}")
        if plan.analytics and plan.analytics.probability_of_profit is not None:
            parts.append(f"POP {plan.analytics.probability_of_profit:.0%}")
        if plan.analytics and plan.analytics.breakevens:
            parts.append("BE " + "/".join(f"{b:g}" for b in plan.analytics.breakevens))
    return Alert(
        title=f"{candidate.direction.value.upper()} setup",
        message=" — ".join(parts),
        severity=Severity.INFO,
        symbol=candidate.symbol,
        score=candidate.composite_score,
    )


def build_candidate_alerts(
    candidates: list[TradeCandidate], min_score: float
) -> list[Alert]:
    return [
        candidate_to_alert(c)
        for c in candidates
        if c.is_actionable and c.composite_score >= min_score
    ]


async def alert_candidates(candidates: list[TradeCandidate]) -> int:
    """Build and dispatch alerts for qualifying candidates. Returns count sent."""
    notifier = get_notifier()
    alerts = build_candidate_alerts(candidates, settings.alerts_min_score)
    if not alerts:
        return 0
    sent = await notifier.send_all(alerts)
    log.info("alerts_sent", channel=notifier.name, count=sent)
    return sent
