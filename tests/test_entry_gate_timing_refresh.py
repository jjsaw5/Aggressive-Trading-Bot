"""The timing gate is a property of the clock, not of the setup.

`evaluate_entry_gates` runs once at scan time and its verdict is frozen onto the
candidate. For liquidity and sizing that is right — those describe the structure
and do not change because someone refreshed a page. Timing does not describe the
structure at all, and freezing it went wrong in both directions:

  - a row scanned pre-market or inside the opening window kept
    `time_of_day_blocked` for the rest of the session (observed in production:
    a 09:31 scan still reading BLOCKED at 09:54);
  - a 0DTE row scanned at 14:00 would keep ALLOWED past the 15:00 cutoff.

The second is the dangerous direction, which is why the read path RECOMPUTES
rather than merely clearing a stale block.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import DTECategory, RejectReason
from app.shortduration.risk import (
    GATE_REJECTS,
    RiskGateConfig,
    refresh_timing_gate,
    timing_gate_now,
)

TIMING = RejectReason.TIME_OF_DAY_BLOCKED.value

# A regular Monday. 13:30Z = 09:30 ET (EDT).
MON = datetime(2026, 8, 3, tzinfo=UTC)


def at(h: int, m: int) -> datetime:
    """A UTC instant expressed as ET wall-clock on the reference Monday."""
    return MON + timedelta(hours=h + 4, minutes=m)


def _refresh(dte, stored, now, evaluated_at=None):
    return refresh_timing_gate(
        dte=dte, stored_reject_reasons=stored,
        evaluated_at=evaluated_at or at(9, 31), now=now,
    )


# --- The clock rule itself ----------------------------------------------------
@pytest.mark.parametrize(
    ("hh", "mm", "blocked", "because"),
    [
        (8, 15, True, "pre-market"),
        (9, 31, True, "inside the opening window"),
        (9, 34, True, "still inside the 5m window"),
        (9, 36, False, "window has passed"),
        (9, 54, False, "the hour the bug was reported at"),
        (14, 30, False, "mid-session"),
        (20, 0, True, "after the close"),
    ],
)
def test_timing_for_1_5dte_follows_the_clock(hh, mm, blocked, because) -> None:
    got, _ = timing_gate_now(DTECategory.SHORT_DTE, at(hh, mm))
    assert got is blocked, because


def test_the_opening_window_is_five_minutes_not_twenty_five() -> None:
    """The reported symptom was a 09:54 block; the rule only covers 09:30-09:35."""
    assert RiskGateConfig.from_settings().no_entry_first_minutes == 5
    assert timing_gate_now(DTECategory.SHORT_DTE, at(9, 36))[0] is False


def test_zero_dte_has_its_own_afternoon_cutoff() -> None:
    assert timing_gate_now(DTECategory.ZERO_DTE, at(14, 59))[0] is False
    assert timing_gate_now(DTECategory.ZERO_DTE, at(15, 1))[0] is True
    # The cutoff is 0DTE-only — a 1-5DTE row is unaffected by it.
    assert timing_gate_now(DTECategory.SHORT_DTE, at(15, 1))[0] is False


def test_a_blocked_verdict_states_its_reason() -> None:
    for when in (at(8, 15), at(9, 31)):
        blocked, reason = timing_gate_now(DTECategory.SHORT_DTE, when)
        assert blocked and reason


# --- THE BUG: a stale block must clear --------------------------------------
def test_a_stale_opening_window_block_clears_by_0954() -> None:
    """The exact production case: scanned 09:31, read at 09:54."""
    r = _refresh(DTECategory.SHORT_DTE, [TIMING], at(9, 54))
    assert r.entry_allowed is True
    assert TIMING not in r.reject_reasons


def test_a_stale_market_closed_block_clears_once_open() -> None:
    r = _refresh(DTECategory.SHORT_DTE, [TIMING], at(10, 30), evaluated_at=at(8, 15))
    assert r.entry_allowed is True


# --- THE DANGEROUS DIRECTION: a stale ALLOW must re-block --------------------
def test_a_0dte_row_scanned_before_the_cutoff_reblocks_after_it() -> None:
    """Scanned 14:00 with gates clear; read at 15:30. Must NOT still say ALLOWED."""
    r = _refresh(DTECategory.ZERO_DTE, [], at(15, 30), evaluated_at=at(14, 0))
    assert r.entry_allowed is False
    assert TIMING in r.reject_reasons
    assert "cutoff" in r.timing_reason


def test_a_clear_row_reblocks_after_the_close() -> None:
    r = _refresh(DTECategory.SHORT_DTE, [], at(20, 0), evaluated_at=at(14, 0))
    assert r.entry_allowed is False and r.timing_blocked


# --- What must NOT be recomputed ---------------------------------------------
def test_non_timing_gate_rejects_survive_and_keep_blocking() -> None:
    """Portfolio limits are account state this function cannot re-derive."""
    r = _refresh(DTECategory.SHORT_DTE, [RejectReason.PORTFOLIO_LIMIT.value], at(14, 0))
    assert r.entry_allowed is False
    assert RejectReason.PORTFOLIO_LIMIT.value in r.reject_reasons


def test_contract_rejects_are_preserved_but_do_not_block_entry() -> None:
    """Matches scan-time semantics: entry_allowed is the GATE verdict alone.

    Production rows carry illiquid_option alongside entry_allowed=true; the
    contract-level reason describes the structure, not the gate.
    """
    r = _refresh(DTECategory.SHORT_DTE, ["illiquid_option"], at(14, 0))
    assert r.entry_allowed is True
    assert "illiquid_option" in r.reject_reasons


def test_a_timing_block_never_duplicates_across_refreshes() -> None:
    r = _refresh(DTECategory.SHORT_DTE, [TIMING, TIMING], at(8, 15))
    assert r.reject_reasons.count(TIMING) == 1


def test_the_evaluation_timestamp_is_carried_through() -> None:
    """The UI needs it to show a stale verdict AS stale."""
    r = _refresh(DTECategory.SHORT_DTE, [], at(14, 0), evaluated_at=at(9, 31))
    assert r.gates_evaluated_at == at(9, 31)


def test_the_gate_reject_set_matches_what_the_gate_can_emit() -> None:
    """If evaluate_entry_gates grows a reason, this set must grow with it —
    otherwise a new blocking reason would be silently downgraded to advisory."""
    import inspect

    from app.shortduration import risk

    src = inspect.getsource(risk.evaluate_entry_gates)
    emitted = {
        r.value for r in RejectReason if f"RejectReason.{r.name}" in src
    }
    assert emitted == set(GATE_REJECTS), (
        f"evaluate_entry_gates emits {sorted(emitted)} but GATE_REJECTS is "
        f"{sorted(GATE_REJECTS)}"
    )
