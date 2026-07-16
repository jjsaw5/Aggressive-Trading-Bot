"""Alerting: notify on high-quality candidates via a swappable notifier.

Disabled by default. Channels (console/slack/noop) sit behind a `Notifier`
interface — the same abstraction discipline as data providers — so adding a new
destination (email, Discord, PagerDuty) is a new implementation, not a rewrite.
"""
