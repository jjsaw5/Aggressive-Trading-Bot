#!/usr/bin/env python3
"""Run one multi-agent market scan and print the ranked trade report.

    python run_market_scan.py                      # full pipeline, mock providers
    python run_market_scan.py --stage premarket    # research only, no contracts
    python run_market_scan.py --symbols NVDA,AMD   # a specific list
    python run_market_scan.py --runner anthropic   # needs ANTHROPIC_API_KEY
    python run_market_scan.py --json out.json      # machine-readable alongside

Runs end to end with no credentials: providers default to the mock stack and
agents to the deterministic runner. Both are stamped on the output so a mocked
run is never mistaken for a live one.

**This script places no orders.** There is no order-placement code path in the
subsystem it drives, and no agent it runs is given an execution tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from app.multiagent.config import get_methodology
from app.multiagent.llm import build_runner
from app.multiagent.models.enums import PipelineStage
from app.multiagent.orchestrator import run_scan
from app.multiagent.reports import render_report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_market_scan",
        description="Multi-agent options research scan (research only — places no orders).",
    )
    p.add_argument(
        "--stage",
        choices=[s.value for s in PipelineStage],
        default=PipelineStage.FULL.value,
        help=(
            "full (default) runs research and, if the options market is open, contract "
            "selection. premarket runs research only. A market_open/full run requested "
            "while the market is closed is downgraded to premarket rather than pricing "
            "contracts off stale quotes."
        ),
    )
    p.add_argument("--symbols", help="Comma-separated tickers. Defaults to the configured universe.")
    p.add_argument(
        "--runner",
        default=None,
        help="Agent runner: 'deterministic' (default, no credentials) or 'anthropic'.",
    )
    p.add_argument("--model", default=None, help="Model id for the anthropic runner.")
    p.add_argument("--methodology", default=None, help="Path to an alternate methodology YAML.")
    p.add_argument("--json", dest="json_path", help="Also write the full report as JSON here.")
    p.add_argument("--no-audit", action="store_true", help="Omit the per-rule score audit.")
    p.add_argument("--no-persist", action="store_true", help="Do not write the run to the database.")
    p.add_argument(
        "--at",
        default=None,
        help=(
            "ISO timestamp to run as-of, for reproducing a scan or exercising the "
            "market-open path outside trading hours."
        ),
    )
    return p.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    cfg = get_methodology(args.methodology) if args.methodology else None
    runner = None
    if args.runner:
        kwargs = {"model": args.model} if (args.model and args.runner != "deterministic") else {}
        runner = build_runner(args.runner, **kwargs)

    now = None
    if args.at:
        parsed = datetime.fromisoformat(args.at)
        now = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    result = await run_scan(
        stage=PipelineStage(args.stage), symbols=symbols, runner=runner, cfg=cfg, now=now
    )

    print(render_report(result.report, show_audit=not args.no_audit))

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(result.report.model_dump(mode="json"), fh, indent=2, default=str)
        print(f"\nJSON report written to {args.json_path}")

    if not args.no_persist:
        from app.multiagent.runtime import get_runtime

        if get_runtime().ma_persist:
            try:
                from app.multiagent.db import save_scan

                run_id = save_scan(result)
                print(f"Run persisted: {run_id}")
            except Exception as exc:  # noqa: BLE001
                # A persistence failure must not discard a report the user is
                # already reading. Say what happened rather than exiting.
                print(f"\n! Persistence failed: {exc}", file=sys.stderr)
                print(
                    "  The report above is unaffected. Run `alembic upgrade head` if the "
                    "ma_* tables are missing.",
                    file=sys.stderr,
                )
                return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
