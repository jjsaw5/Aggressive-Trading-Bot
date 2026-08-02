"""Timestamps render in exchange time, or the build fails.

Reviewer Rulings #2, R3.3. Eleven `toLocaleString` calls rendered in the
viewer's zone. On a product whose entire subject is same-session and 1-5DTE
expiry, "09:31" meaning different instants on different machines is a
correctness hazard, not a polish item — session boundaries, RTH coverage
windows, quote ages and expiries are all exchange-clock concepts.

The fix is a shared formatter. This test is what stops the next one being added
by hand: any `toLocale*(` in the dashboard must pass an explicit `timeZone`.

Why grep rather than a DOM test: the dashboard is a single 2,000-line HTML file
with no build step and no module boundary, so there is nothing to import. The
failure mode being guarded is a developer typing `new Date(x).toLocaleString()`,
and that is a lexical property of the file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "app" / "web" / "dashboard.html"

# Matches a toLocale* call and captures its argument list up to the closing
# paren. Locale/option arguments never themselves contain parens, so a
# non-greedy scan to the first ")" is sufficient here.
_CALL = re.compile(r"\.toLocale(?:String|TimeString|DateString)\(([^)]*)\)")


def _source() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def test_the_dashboard_exists_where_this_test_thinks_it_does() -> None:
    # A moved file must fail loudly rather than vacuously passing every
    # assertion below against an empty string.
    assert DASHBOARD.is_file(), f"dashboard not found at {DASHBOARD}"


def test_every_locale_call_pins_a_timezone() -> None:
    """The rule. A bare toLocale* call renders in the VIEWER's zone."""
    offenders = [
        args.strip() for args in _CALL.findall(_source()) if "timeZone" not in args
    ]
    assert not offenders, (
        "Timestamps must render in exchange time. Use etDateTime() / etTime() / "
        "etDate() from dashboard.html rather than calling toLocale* directly.\n"
        "Calls missing an explicit timeZone:\n  "
        + "\n  ".join(offenders)
    )


def test_the_shared_formatters_are_present_and_pinned_to_the_exchange() -> None:
    src = _source()
    assert 'const ET = "America/New_York";' in src
    for helper in ("etDateTime", "etTime", "etDate"):
        assert f"const {helper} = " in src, f"{helper} helper is missing"


def test_the_reader_is_told_which_clock_they_are_on() -> None:
    """A pinned zone the user cannot see is still an ambiguous timestamp."""
    src = _source()
    assert src.count('+ " ET"') >= 2, (
        "etDateTime and etTime must label their output ET — pinning the zone "
        "silently just moves the ambiguity."
    )


@pytest.mark.parametrize("bare", [".toLocaleString()", ".toLocaleTimeString()", ".toLocaleDateString()"])
def test_no_zero_argument_locale_calls_at_all(bare: str) -> None:
    # Belt and braces: the regex above would catch these, but this asserts the
    # exact string form a developer is most likely to type.
    assert bare not in _source(), f"bare {bare} renders in the viewer's timezone"
