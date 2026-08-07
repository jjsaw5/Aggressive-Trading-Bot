"""Flow interpretation and the "absent stays absent" contract.

The flow tests exist because the single most common error in this domain is
reading a large print as directional. The measurement tests exist because
`CLAUDE.md` §4 traces 67 of 67 bad signals to one `or 0.0`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import OptionType
from app.domain.options import FlowAlert
from app.multiagent.analysis.flow import build_flow_snapshot
from app.multiagent.models.enums import BiasDirection, Direction, ValidationVerdict
from app.multiagent.models.measurements import (
    AbsenceReason,
    Measurement,
    MeasurementSet,
    Provenance,
)

NOW = datetime(2026, 8, 5, 16, 0, tzinfo=UTC)

FLOW_KWARGS = {
    "now": NOW,
    "lookback_hours": 24,
    "min_premium_usd": 50_000.0,
    "net_premium_ratio_strong": 0.60,
    "ask_side_share_strong": 0.60,
    "size_over_oi_ratio": 1.0,
}


def _alert(otype, premium, *, at_ask=True, oi=100, size=500, sweep=False, strike=100.0, hours=1):
    return FlowAlert(
        symbol="NVDA",
        option_type=otype,
        strike=strike,
        premium=premium,
        size=size,
        open_interest=oi,
        is_sweep=sweep,
        at_ask=at_ask,
        ts=NOW - timedelta(hours=hours),
        source="test",
    )


# --- direction is a conclusion, never assumed -------------------------------


def test_a_near_even_split_is_ambiguous_not_directional():
    snap = build_flow_snapshot(
        "NVDA",
        [_alert(OptionType.CALL, 500_000.0), _alert(OptionType.PUT, 450_000.0)],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    assert snap.direction_ambiguous
    assert snap.implied_bias is BiasDirection.NEUTRAL
    assert snap.verdict is ValidationVerdict.MIXED
    assert "not evidence for either direction" in snap.interpretation


def test_a_decisive_lean_agreeing_with_the_thesis_confirms():
    snap = build_flow_snapshot(
        "NVDA",
        [_alert(OptionType.CALL, 900_000.0), _alert(OptionType.PUT, 100_000.0)],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    assert not snap.direction_ambiguous
    assert snap.implied_bias is BiasDirection.BULLISH
    assert snap.verdict is ValidationVerdict.CONFIRMS


def test_a_decisive_lean_against_the_thesis_contradicts():
    snap = build_flow_snapshot(
        "NVDA",
        [_alert(OptionType.CALL, 900_000.0), _alert(OptionType.PUT, 100_000.0)],
        Direction.BEARISH,
        **FLOW_KWARGS,
    )
    assert snap.verdict is ValidationVerdict.CONTRADICTS


def test_the_net_premium_ratio_is_oriented_to_the_candidates_direction():
    calls_heavy = [_alert(OptionType.CALL, 900_000.0), _alert(OptionType.PUT, 100_000.0)]
    bullish = build_flow_snapshot("NVDA", calls_heavy, Direction.BULLISH, **FLOW_KWARGS)
    bearish = build_flow_snapshot("NVDA", calls_heavy, Direction.BEARISH, **FLOW_KWARGS)
    b = bullish.measurements.value("flow_net_premium_ratio")
    r = bearish.measurements.value("flow_net_premium_ratio")
    assert b == pytest.approx(-r)
    assert b > 0 and r < 0


def test_premium_below_the_significance_threshold_abstains_entirely():
    """Reading thin flow manufactures confirmation out of noise."""
    snap = build_flow_snapshot(
        "NVDA",
        [_alert(OptionType.CALL, 900.0), _alert(OptionType.PUT, 100.0)],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    assert snap.verdict is ValidationVerdict.INSUFFICIENT_DATA
    assert snap.direction_ambiguous
    assert "below significance threshold" in " ".join(snap.caveats)


# --- missing data is missing ------------------------------------------------


def test_unknown_execution_side_is_not_counted_as_bid_side():
    """`at_ask=None` means the vendor did not say."""
    snap = build_flow_snapshot(
        "NVDA",
        [
            _alert(OptionType.CALL, 900_000.0, at_ask=None),
            _alert(OptionType.PUT, 100_000.0, at_ask=None),
        ],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    ask_share = snap.measurements.get("flow_ask_side_share")
    assert not ask_share.present
    assert ask_share.absence_reason is AbsenceReason.NO_DATA
    assert snap.ask_side_premium is None
    assert "aggression is unknown, not absent" in " ".join(snap.caveats)


def test_ask_side_share_excludes_side_unknown_prints_from_the_denominator():
    snap = build_flow_snapshot(
        "NVDA",
        [
            _alert(OptionType.CALL, 600_000.0, at_ask=True),
            _alert(OptionType.CALL, 400_000.0, at_ask=False),
            _alert(OptionType.PUT, 500_000.0, at_ask=None),  # excluded entirely
        ],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    # 600k of 1.0m side-known premium, not 600k of 1.5m total.
    assert snap.measurements.value("flow_ask_side_share") == pytest.approx(0.6)


def test_missing_open_interest_leaves_opening_versus_closing_unknown():
    snap = build_flow_snapshot(
        "NVDA",
        [_alert(OptionType.CALL, 900_000.0, oi=None), _alert(OptionType.PUT, 100_000.0, oi=None)],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    assert snap.likely_opening is None  # not False
    assert snap.max_size_over_oi is None
    assert "opening versus closing cannot be inferred" in " ".join(snap.caveats)


def test_a_print_with_no_premium_is_excluded_rather_than_estimated():
    snap = build_flow_snapshot(
        "NVDA",
        [
            _alert(OptionType.CALL, 900_000.0),
            FlowAlert(
                symbol="NVDA", option_type=OptionType.CALL, strike=100.0,
                premium=None, size=10_000, open_interest=50, ts=NOW, source="test",
            ),
        ],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    # The unpriced print does not contribute a reconstructed premium.
    assert snap.measurements.value("flow_total_premium") == pytest.approx(900_000.0)
    assert "carried no premium and were excluded" in " ".join(snap.caveats)


def test_a_provider_error_is_reported_not_treated_as_no_flow():
    """'The provider was down' and 'there was no flow' are different findings."""
    snap = build_flow_snapshot(
        "NVDA", None, Direction.BULLISH, provider_error="503 upstream", **FLOW_KWARGS
    )
    assert snap.verdict is ValidationVerdict.INSUFFICIENT_DATA
    assert snap.measurements.get("flow_total_premium").absence_reason is AbsenceReason.PROVIDER_ERROR
    assert "503 upstream" in " ".join(snap.caveats)


def test_no_data_and_no_prints_are_distinguishable():
    no_data = build_flow_snapshot("NVDA", None, Direction.BULLISH, **FLOW_KWARGS)
    no_prints = build_flow_snapshot("NVDA", [], Direction.BULLISH, **FLOW_KWARGS)

    assert "no flow data retrieved" in " ".join(no_data.caveats)
    assert "no flow prints in the last" in " ".join(no_prints.caveats)
    # An empty-but-successful fetch knows its count is genuinely zero.
    assert no_prints.measurements.value("flow_alert_count") == 0.0
    assert not no_data.measurements.get("flow_alert_count").present


def test_prints_outside_the_lookback_window_are_excluded():
    snap = build_flow_snapshot(
        "NVDA",
        [_alert(OptionType.CALL, 900_000.0, hours=100)],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    assert snap.alerts_considered == 0


def test_the_flow_caveats_carry_the_repositorys_own_evidence():
    """The at-ask caveat cites the pre-registered experiment that found it weak."""
    snap = build_flow_snapshot(
        "NVDA",
        [_alert(OptionType.CALL, 900_000.0), _alert(OptionType.PUT, 100_000.0)],
        Direction.BULLISH,
        **FLOW_KWARGS,
    )
    assert any("FLOW_EXPERIMENT_DISPOSITION" in c for c in snap.caveats)


# --- Measurement: absent stays absent ---------------------------------------


def test_a_none_value_becomes_an_absent_measurement_not_zero():
    m = Measurement.of("spot", None, provenance=Provenance.PROVIDER)
    assert m.value is None
    assert not m.present
    assert m.absence_reason is AbsenceReason.NO_DATA
    assert m.export() == "NA_no_data"


def test_requiring_an_absent_measurement_raises_rather_than_defaulting():
    with pytest.raises(ValueError, match="absent"):
        Measurement.absent("spot").require()


def test_absence_reasons_are_distinguishable_in_an_export():
    """`NA_not_implemented` and `NA_no_data` mean different things."""
    never = Measurement.absent("x", AbsenceReason.NOT_IMPLEMENTED)
    missing = Measurement.absent("y", AbsenceReason.NO_DATA)
    stale = Measurement.absent("z", AbsenceReason.STALE)
    assert never.export() == "NA_not_implemented"
    assert missing.export() == "NA_no_data"
    assert stale.export() == "NA_stale"


def test_an_export_never_produces_a_blank_cell():
    ms = MeasurementSet()
    ms.add(Measurement.of("a", 1.0))
    ms.add(Measurement.absent("b"))
    exported = ms.export()
    assert exported == {"a": 1.0, "b": "NA_no_data"}
    assert all(v != "" and v is not None for v in exported.values())


def test_an_unknown_measurement_name_reads_as_not_implemented():
    ms = MeasurementSet()
    m = ms.get("never_computed")
    assert not m.present
    assert m.absence_reason is AbsenceReason.NOT_IMPLEMENTED


def test_coverage_is_none_for_an_empty_set_rather_than_zero():
    """Zero coverage and no measurements at all are different statements."""
    assert MeasurementSet().coverage() is None
    ms = MeasurementSet()
    ms.add(Measurement.absent("a"))
    assert ms.coverage() == 0.0


def test_modeled_values_carry_their_provenance():
    m = Measurement.of("theta", -0.05, provenance=Provenance.MODELED)
    assert m.provenance is Provenance.MODELED


def test_evidence_age_is_none_when_publication_time_is_unknown(ledger, now):
    """Substituting retrieval time would make every undated item look fresh."""
    from app.multiagent.models.enums import EvidenceKind

    undated = next(i for i in ledger.items.values() if i.kind is EvidenceKind.EARNINGS_EVENT)
    assert undated.published_at is None
    assert undated.age_days(now) is None

    dated = next(i for i in ledger.items.values() if i.kind is EvidenceKind.NEWS)
    assert dated.age_days(now) == pytest.approx(1.0, abs=0.01)
