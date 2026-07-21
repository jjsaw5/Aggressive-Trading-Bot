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

from app.backtest.runner import run_backtest, run_historical_backtest
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
                f"         {c.trade_plan.strategy.display_name} x{c.trade_plan.contracts} "
                f"| risk ${r.max_loss_usd:.0f} ({r.account_risk_pct:.1%}) "
                f"| TP +{r.profit_target_pct:.0%} / SL -{r.stop_loss_pct:.0%}"
            )
        elif c.reject_reasons:
            print(f"         rejected: {', '.join(r.value for r in c.reject_reasons)}")
    print()


async def _backtest(num_paths: int, as_json: bool, historical: bool) -> None:
    if historical:
        report = await run_historical_backtest()
    else:
        report = await run_backtest(num_paths=num_paths)
    if as_json:
        print(json.dumps(report.as_dict(), indent=2))
        return

    o = report.overall
    if report.mode == "historical":
        print(
            f"\nHistorical backtest: {report.num_trades} trades over real "
            f"underlying paths ({report.num_candidates} symbols)"
        )
        print("(trend-following defined-risk verticals; legs repriced by BS at realized vol)\n")
    else:
        print(
            f"\nBacktest: {report.num_candidates} candidates x {report.num_paths} "
            f"simulated paths = {report.num_trades} trades"
        )
        print("(zero-drift GBM simulation — structural edge only; not real option history)\n")
    print(
        f"  OVERALL  win {o.win_rate:.0%} | expectancy ${o.expectancy_usd:+.2f} | "
        f"PF {o.profit_factor if o.profit_factor is None else round(o.profit_factor, 2)} | "
        f"avg MFE ${o.avg_mfe_usd:.0f} / MAE ${o.avg_mae_usd:.0f} | hold {o.avg_days_held:.0f}d"
    )
    print("\n  By strategy:")
    for s in report.by_strategy:
        print(
            f"    {s.group:20s} n={s.trades:4d} win {s.win_rate:.0%} "
            f"exp ${s.expectancy_usd:+.2f}"
        )
    print()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(prog="atb", description="Aggressive Trading Bot CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="Run a research scan")
    scan_p.add_argument("--json", action="store_true", help="JSON output")
    scan_p.add_argument("--actionable", action="store_true", help="Only actionable candidates")

    bt_p = sub.add_parser("backtest", help="Backtest candidates (simulated or historical)")
    bt_p.add_argument("--paths", type=int, default=200, help="Monte-Carlo paths per candidate")
    bt_p.add_argument(
        "--historical",
        action="store_true",
        help="Replay real underlying price history instead of simulating paths",
    )
    bt_p.add_argument("--json", action="store_true", help="JSON output")

    sub.add_parser("providers", help="Show provider configuration status")

    args = parser.parse_args()

    if args.command == "scan":
        asyncio.run(_scan(args.json, args.actionable))
    elif args.command == "backtest":
        asyncio.run(_backtest(args.paths, args.json, args.historical))
    elif args.command == "providers":
        from app.providers import registry

        resolvers = {
            "market_data": registry.market_data_provider,
            "fundamentals": registry.fundamentals_provider,
            "options_chain": registry.options_chain_provider,
            "options_flow": registry.options_flow_provider,
            "iv_history": registry.iv_history_provider,
            "calendar": registry.calendar_provider,
            "brokerage": registry.brokerage_provider,
        }
        for cap, resolve in resolvers.items():
            try:
                provider = resolve()
                if provider is None:
                    print(f"  {cap:14s} -> none (realized-vol proxy)")
                    continue
                m = provider.meta
                print(f"  {cap:14s} -> {m.name:16s} verified={m.verified}")
            except Exception as exc:
                print(f"  {cap:14s} -> UNRESOLVED ({exc})")


if __name__ == "__main__":
    main()
