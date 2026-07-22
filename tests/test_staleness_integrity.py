"""Phase 3.3: unknown timestamps read as STALE, never silently backfilled to now."""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.market import Quote
from app.providers.fmp.client import _epoch_to_dt
from app.shortduration.scoring.data_quality import quote_is_stale

_NOW = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)


def test_fmp_epoch_parse_failure_is_none_not_now() -> None:
    # The masking bug: an unparseable epoch used to become datetime.now(), so a
    # quote of unknown age looked seconds-fresh. It must be None instead.
    assert _epoch_to_dt(None) is None
    assert _epoch_to_dt("garbage") is None
    assert _epoch_to_dt(1_800_000_000) is not None  # a real epoch still parses


def test_quote_without_timestamp_is_stale() -> None:
    from app.domain.enums import ShortDurationRegime
    from app.domain.shortduration import ShortDurationRegimeState
    from app.shortduration.strategies.base import SetupContext

    regime = ShortDurationRegimeState(regime=ShortDurationRegime.RANGE_BOUND, confidence=0.5,
                                      allow_new_trades=True, as_of=_NOW)
    q = Quote(symbol="AAA", price=100.0, as_of=None, source="fmp")  # no usable timestamp
    ctx = SetupContext(symbol="AAA", now=_NOW, regime=regime, quote=q)
    assert quote_is_stale(ctx) is True

    fresh = Quote(symbol="AAA", price=100.0, as_of=_NOW, source="fmp")
    ctx_fresh = SetupContext(symbol="AAA", now=_NOW, regime=regime, quote=fresh)
    assert quote_is_stale(ctx_fresh) is False


def test_configured_delay_marks_quote_stale() -> None:
    from app.domain.enums import ShortDurationRegime
    from app.domain.shortduration import ShortDurationRegimeState
    from app.shortduration.strategies.base import SetupContext

    regime = ShortDurationRegimeState(regime=ShortDurationRegime.RANGE_BOUND, confidence=0.5,
                                      allow_new_trades=True, as_of=_NOW)
    # A fresh-looking timestamp but the provider tier is 15-min delayed -> stale.
    q = Quote(symbol="AAA", price=100.0, as_of=_NOW, delayed_minutes=15, source="fmp")
    ctx = SetupContext(symbol="AAA", now=_NOW, regime=regime, quote=q)
    assert quote_is_stale(ctx) is True
