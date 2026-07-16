"""Command-line entrypoint for one-shot operations.

    python -m app.cli scan          # run a research scan, print ranked candidates
    python -m app.cli scan --json   # machine-readable output
    python -m app.cli providers     # show configured provider status

Kept dependency-light (argparse) so it runs anywhere the package installs.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.logging_config import configure_logging
from app.services.scan_service import run_scan


async def _scan(as_json: bool, actionable_only: bool) -> None:
    candidates = await run_scan()
    shown = [c for c in candidates if c.is_actionable] if actionable_only else candidates

    if as_json:
        print(json.dumps([c.model_dump(mode="json") for c in shown], indent=2))
        return

    print(f"\nRanked candidates ({len(shown)} shown of {len(candidates)}):\n")
    for c in shown:
        tag = "ACTIONABLE" if c.is_actionable else c.status.value.upper()
        print(f"  {c.composite_score:5.2f}  {c.symbol:6s} {c.direction.value:8s} [{tag}]")
        if c.trade_plan:
            r = c.trade_plan.risk
            print(
                f"         {c.trade_plan.strategy.value} x{c.trade_plan.contracts} "
                f"| risk ${r.max_loss_usd:.0f} ({r.account_risk_pct:.1%}) "
                f"| TP +{r.profit_target_pct:.0%} / SL -{r.stop_loss_pct:.0%}"
            )
        elif c.reject_reasons:
            print(f"         rejected: {', '.join(r.value for r in c.reject_reasons)}")
    print()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="atb", description="Aggressive Trading Bot CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Run a research scan")
    scan_p.add_argument("--json", action="store_true", help="JSON output")
    scan_p.add_argument("--actionable", action="store_true", help="Only actionable candidates")

    sub.add_parser("providers", help="Show provider configuration status")

    args = parser.parse_args()

    if args.command == "scan":
        asyncio.run(_scan(args.json, args.actionable))
    elif args.command == "providers":
        from app.providers import registry

        resolvers = {
            "market_data": registry.market_data_provider,
            "fundamentals": registry.fundamentals_provider,
            "options_chain": registry.options_chain_provider,
            "options_flow": registry.options_flow_provider,
            "calendar": registry.calendar_provider,
            "brokerage": registry.brokerage_provider,
        }
        for cap, resolve in resolvers.items():
            try:
                m = resolve().meta
                print(f"  {cap:14s} -> {m.name:16s} verified={m.verified}")
            except Exception as exc:
                print(f"  {cap:14s} -> UNRESOLVED ({exc})")


if __name__ == "__main__":
    main()
