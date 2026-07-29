"""Run both short-duration scans and print the engine's committed picks.

Built for the away-from-desk case: this runs the scans itself rather than
reading whatever a home machine happened to leave behind, so the picks are
current even if nothing else is running. It writes to the same warehouse the
dashboard reads, so the boards are populated when you get back.

Output is two parts, in this order:
  PUSH: <one line, <=200 chars>   — ready to send as a phone notification
  then a fuller block for the session transcript.

Picks are the engine's ENGINE PICK commitments (see shortduration.ranking):
top-ranked setups with a sized defined-risk structure and no risk-gate block.
UNCALIBRATED — a recorded prediction, not a recommendation.

Usage:
    python scripts/top_picks.py             # scan both boards, then report
    python scripts/top_picks.py --no-scan   # report the existing boards only
"""

from __future__ import annotations

import argparse
import asyncio

from app.db import repository
from app.domain.enums import DTECategory
from app.logging_config import get_logger

log = get_logger(__name__)

_BOARDS = [("0DTE", DTECategory.ZERO_DTE, "0dte"), ("1-5DTE", DTECategory.SHORT_DTE, "1-5dte")]


def _picks(dte_key: str) -> list:
    rows = repository.list_short_duration_candidates(dte_category=dte_key, limit=200, ranked=True)
    picks = [c for c in rows if c.engine_pick]
    return sorted(picks, key=lambda c: c.pick_rank or 99)[:3]


def _short(c) -> str:
    d = {"bullish": "bull", "bearish": "bear"}.get(c.direction.value, c.direction.value)
    gate = "" if c.entry_allowed else "*"
    return f"{c.symbol} {d}{gate}"


def _detail(c) -> str:
    risk = f"${c.max_risk_usd:,.0f}" if c.max_risk_usd is not None else "—"
    rr = f"{c.reward_to_risk:.2f}:1" if c.reward_to_risk is not None else "—"
    pop = f"{c.probability_of_profit * 100:.0f}%" if c.probability_of_profit is not None else "—"
    struct = c.contract.description if c.contract else "—"
    gate = "ENTRY CLEAR" if c.entry_allowed else "entry gated: " + "; ".join(
        (c.entry_notes or ["blocked"])[:1])
    return (f"  #{c.pick_rank} {c.symbol} {c.direction.value} · {c.strategy.value if c.strategy else '?'}\n"
            f"      {struct} · risk {risk} · R:R {rr} · POP {pop} (uncalibrated)\n"
            f"      score {c.score:.2f} · {gate}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-scan", action="store_true", help="report existing boards without scanning")
    args = ap.parse_args()

    if not args.no_scan:
        from app.shortduration.detection import run_detection

        for label, dte, _key in _BOARDS:
            try:
                found = await run_detection(dte)
                log.info("top_picks_scanned", board=label, detected=len(found))
            except Exception as exc:  # noqa: BLE001 — one board failing must not kill the other
                log.warning("top_picks_scan_failed", board=label, error=str(exc))

    compact, detail = [], []
    for label, _dte, key in _BOARDS:
        picks = _picks(key)
        if picks:
            compact.append(f"{label}: " + " · ".join(_short(c) for c in picks))
            detail.append(f"{label} — engine picks:\n" + "\n".join(_detail(c) for c in picks))
        else:
            compact.append(f"{label}: none")
            detail.append(f"{label} — no picks (nothing cleared a tradeable structure + risk gates).")

    push = " | ".join(compact)
    if any("*" in c for c in compact):
        push += "  (*=entry gated)"
    print("PUSH: " + push[:200])
    print()
    print("\n\n".join(detail))
    print("\nUNCALIBRATED — the engine's recorded picks, not validated advice. "
          "Your thesis and your sizing.")


if __name__ == "__main__":
    asyncio.run(main())
