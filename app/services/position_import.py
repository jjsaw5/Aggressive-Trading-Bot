"""Import real broker option positions as tracked positions Tier 4 can monitor.

The app's own Robinhood client can't run in every environment, so positions are
ingested through a normalized shape (symbol + legs) — whoever has broker access
(the agent via the connector, or a future working broker provider) maps the raw
account data to `ImportedLeg`s and calls `build_tracked_trade`. The result is a
`PaperTrade` with the REAL entry fill and a mechanical exit plan, stored like any
other tracked position so Tier 4 marks it from the live chain each pass.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.config import settings
from app.domain.enums import (
    Direction,
    OptionAction,
    OptionType,
    PaperTradeStatus,
    StrategyType,
)
from app.domain.trades import ContractLeg, PaperTrade, RiskPlan, TradePlan
from app.risk.exit_plan import for_trade_plan


@dataclass
class ImportedLeg:
    strike: float
    option_type: OptionType
    is_long: bool
    quantity: int
    entry_price_per_share: float  # positive premium per share (0 ok when a net override is given)
    expiration: date


def _infer(legs: list[ImportedLeg]) -> tuple[StrategyType, Direction]:
    calls = [leg for leg in legs if leg.option_type == OptionType.CALL]
    puts = [leg for leg in legs if leg.option_type == OptionType.PUT]
    if len(legs) == 1:
        leg = legs[0]
        if leg.option_type == OptionType.CALL:
            return StrategyType.LONG_CALL, Direction.BULLISH
        return StrategyType.LONG_PUT, Direction.BEARISH
    if len(calls) == 2:  # vertical on calls
        longs = [c for c in calls if c.is_long]
        shorts = [c for c in calls if not c.is_long]
        if longs and shorts and longs[0].strike < shorts[0].strike:
            return StrategyType.BULL_CALL_SPREAD, Direction.BULLISH
    if len(puts) == 2:
        longs = [p for p in puts if p.is_long]
        shorts = [p for p in puts if not p.is_long]
        if longs and shorts and longs[0].strike > shorts[0].strike:
            return StrategyType.BEAR_PUT_SPREAD, Direction.BEARISH
    # Abstain, don't guess: an unrecognized combo used to be silently labeled
    # LONG_CALL — which later crashed the positions board when the breakeven
    # math found no call leg. Refuse with a corrective message instead.
    sides = "/".join(("long" if lg.is_long else "short") + lg.option_type.value[0] for lg in legs)
    raise ValueError(
        f"Unsupported structure ({sides}). Supported: a single long call/put, or a "
        "2-leg vertical with one leg BOUGHT and one SOLD (bull call spread: long the "
        "lower call; bear put spread: long the higher put). If both legs show as "
        "'long', mark the second leg as Sold."
    )


def build_tracked_trade(
    symbol: str,
    legs: list[ImportedLeg],
    *,
    opened_at: datetime | None = None,
    source: str = "broker_import",
    net_per_share: float | None = None,
) -> PaperTrade:
    """Build a tracked position from its legs. `net_per_share` overrides the
    per-leg sum with the structure's net cost (debit > 0, credit < 0) — the way a
    broker quotes a spread — so callers can pass the average cost directly instead
    of reverse-engineering per-leg fills. Only the net drives P&L, max loss/profit,
    and breakevens, so the per-leg entry prices are then purely cosmetic."""
    if not legs:
        raise ValueError("a position needs at least one leg")
    strategy, direction = _infer(legs)
    contracts = min(leg.quantity for leg in legs)

    # Signed net per share (debit > 0, credit < 0) and dollar economics.
    net_per_share = round(
        net_per_share
        if net_per_share is not None
        else sum((1 if leg.is_long else -1) * leg.entry_price_per_share for leg in legs),
        4,
    )
    is_vertical = len(legs) == 2
    width = (
        abs(legs[0].strike - legs[1].strike) if is_vertical else 0.0
    )
    max_loss_usd = round(abs(net_per_share) * 100 * contracts, 2)
    max_profit_usd = (
        round((width - abs(net_per_share)) * 100 * contracts, 2) if is_vertical else None
    )

    plan_legs = [
        ContractLeg(
            symbol=symbol.upper(),
            action=OptionAction.BUY_TO_OPEN if leg.is_long else OptionAction.SELL_TO_OPEN,
            option_type=leg.option_type,
            strike=leg.strike,
            expiration=leg.expiration,
            quantity=1,
            entry_price=leg.entry_price_per_share,
        )
        for leg in legs
    ]
    risk = RiskPlan(
        max_loss_usd=max_loss_usd,
        max_profit_usd=max_profit_usd,
        account_risk_pct=round(max_loss_usd / settings.account_equity_usd, 4),
        profit_target_pct=0.5,
        stop_loss_pct=0.5,
        time_stop_dte=7,
        invalidation_note="Imported broker position.",
    )
    plan = TradePlan(
        symbol=symbol.upper(),
        direction=direction,
        strategy=strategy,
        legs=plan_legs,
        net_debit=round(net_per_share * 100, 2),
        contracts=contracts,
        risk=risk,
        rationale=f"Imported from broker ({source}).",
    )
    plan.exit_plan = for_trade_plan(plan)

    return PaperTrade(
        id=uuid.uuid4().hex[:12],
        scan_id=source,
        symbol=symbol.upper(),
        trade_plan=plan,
        status=PaperTradeStatus.OPEN,
        opened_at=opened_at or datetime.now(UTC),
        entry_fill=net_per_share,
        entry_slippage=0.0,
    )


async def sync_broker_positions() -> tuple[int, str]:
    """Pull open option positions from the configured brokerage and store them as
    tracked positions Tier 4 monitors. Raises a clear error if the broker can't
    read positions (e.g. Robinhood library not installed, or creds/MFA missing)."""
    import asyncio

    from app.db import repository
    from app.providers import registry

    broker = registry.brokerage_provider()
    getter = getattr(broker, "get_option_positions", None)
    if getter is None:
        raise RuntimeError(
            "The configured brokerage can't read positions. Set PROVIDER_BROKERAGE=robinhood."
        )
    groups = await getter()
    if not groups:
        return 0, "No open option positions returned by the broker."
    n = 0
    for symbol, legs in groups:
        if not legs:
            continue
        trade = build_tracked_trade(symbol, legs, source="rh_sync")
        await asyncio.to_thread(repository.save_paper_trade, trade)
        n += 1
    return n, f"Synced {n} position(s) from the broker."


# --- One-line quick entry -----------------------------------------------------
# "TSLA 370/365p 7/24 @2.45 x2"  -> put debit spread: long 370p, short 365p,
#                                   net 2.45/share debit, 2 contracts, exp Jul 24.
# "AAPL 230c 8/15 3.10"          -> long call (qty defaults to 1, @ optional).
# "SPY 645/640c 12/19 @-1.55"    -> long 645c / short 640c for a 1.55 CREDIT.
# Rules: FIRST strike is the leg you BOUGHT, second the leg you SOLD; the net's
# sign carries debit (+) vs credit (−); M/D dates roll to the next occurrence.
_LINE_RE = re.compile(
    r"""^\s*
    (?P<sym>[A-Za-z]{1,6})\s+
    (?P<k1>\d+(?:\.\d+)?)(?P<t1>[cCpP])?
    (?:\s*/\s*(?P<k2>\d+(?:\.\d+)?))?(?P<t2>[cCpP])?\s+
    (?P<date>\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+
    @?\s*(?P<net>-?\d+(?:\.\d+)?)
    (?:\s*[xX]\s*(?P<qty>\d+))?
    \s*$""",
    re.VERBOSE,
)


def _parse_line_date(raw: str, today: date) -> date:
    if "-" in raw:
        return date.fromisoformat(raw)
    parts = [int(p) for p in raw.split("/")]
    if len(parts) == 3:
        yr = parts[2] + 2000 if parts[2] < 100 else parts[2]
        return date(yr, parts[0], parts[1])
    m, d = parts
    exp = date(today.year, m, d)
    return exp if exp >= today else date(today.year + 1, m, d)


def parse_trade_line(line: str, *, today: date | None = None) -> tuple[str, list[ImportedLeg], float, int]:
    """Parse the one-line format into (symbol, legs, net_per_share, contracts).
    Raises ValueError with a human-usable message on anything it can't read."""
    today = today or datetime.now(UTC).date()
    m = _LINE_RE.match(line or "")
    if not m:
        raise ValueError(
            "Couldn't read that. Format: SYMBOL strike[/strike2](c|p) date net [xQty] — "
            "e.g. 'TSLA 370/365p 7/24 @2.45 x1' (first strike = bought, second = sold; "
            "net + = debit, − = credit)."
        )
    sym = m.group("sym").upper()
    t1, t2 = m.group("t1"), m.group("t2")
    tletter = (t2 or t1 or "").lower()
    if tletter not in ("c", "p"):
        raise ValueError("Mark the option type with c or p after the strike(s), e.g. 370/365p.")
    otype = OptionType.CALL if tletter == "c" else OptionType.PUT
    if t1 and t2 and t1.lower() != t2.lower():
        raise ValueError("Mixed call/put verticals aren't supported in quick entry — use the full form.")
    exp = _parse_line_date(m.group("date"), today)
    net = float(m.group("net"))
    qty = int(m.group("qty") or 1)
    if qty < 1:
        raise ValueError("Quantity must be at least 1.")
    legs = [ImportedLeg(strike=float(m.group("k1")), option_type=otype, is_long=True,
                        quantity=qty, entry_price_per_share=0.0, expiration=exp)]
    if m.group("k2"):
        legs.append(ImportedLeg(strike=float(m.group("k2")), option_type=otype, is_long=False,
                                quantity=qty, entry_price_per_share=0.0, expiration=exp))
    if len(legs) == 1 and net < 0:
        raise ValueError("A single long option can't have a negative (credit) net.")
    return sym, legs, net, qty


# --- Broker-paste entry (Robinhood position screen) ---------------------------
# Select-copy the position screen and paste it as-is. What we read:
#   leg lines:      "JPM $355 Call" ... "7/24 · 2 Sells"   (Buys = long, Sells = short)
#   entry net:      "Average cost $0.25"  <- the ONLY price used as entry
#   contracts:      "Quantity +2" (sign carries debit/credit when cost has none)
#   opened:         "Date opened 7/23"    (rolls BACKWARD — opens are in the past)
# Per-leg dollar amounts on those screens are CURRENT marks, never the entry —
# they are deliberately ignored.
_PASTE_LEG_RE = re.compile(
    r"(?P<sym>[A-Z]{1,6})\s+\$(?P<strike>\d+(?:\.\d+)?)\s+(?P<type>Call|Put)\b"
    r"[\s\S]{0,40}?(?P<date>\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
    r"[^\n]{0,30}?(?P<qty>\d+)\s+(?P<side>Buys?|Sells?)\b",
    re.IGNORECASE,
)
_PASTE_COST_RE = re.compile(r"Average\s+(?:cost|credit)\s*\(?(?P<neg>[-−(])?\$(?P<amt>\d+(?:\.\d+)?)", re.IGNORECASE)
_PASTE_QTY_RE = re.compile(r"Quantity\s*(?P<sign>[+\-−])?\s*(?P<n>\d+)", re.IGNORECASE)
_PASTE_OPENED_RE = re.compile(r"Date\s+opened\s*(?P<d>\d{1,2}/\d{1,2}(?:/\d{2,4})?)", re.IGNORECASE)


def looks_like_broker_paste(text: str) -> bool:
    return bool(_PASTE_LEG_RE.search(text or ""))


def _parse_past_date(raw: str, today: date) -> date:
    parts = [int(p) for p in raw.split("/")]
    if len(parts) == 3:
        yr = parts[2] + 2000 if parts[2] < 100 else parts[2]
        return date(yr, parts[0], parts[1])
    m, d = parts
    opened = date(today.year, m, d)
    return opened if opened <= today else date(today.year - 1, m, d)


def parse_broker_paste(
    text: str, *, today: date | None = None
) -> tuple[str, list[ImportedLeg], float, int, date | None]:
    """Parse a pasted broker position screen into (symbol, legs, net_per_share,
    contracts, opened_date). Raises ValueError with a usable message."""
    today = today or datetime.now(UTC).date()
    matches = list(_PASTE_LEG_RE.finditer(text or ""))
    if not matches:
        raise ValueError(
            "Couldn't find option legs in the paste. Copy the position screen "
            "including the Options section (e.g. 'JPM $355 Call / 7/24 · 2 Sells') "
            "and the 'Average cost' line."
        )
    if len(matches) > 2:
        raise ValueError("More than two legs found — quick entry supports a single "
                         "option or a 2-leg vertical. Use the full form.")
    syms = {m.group("sym").upper() for m in matches}
    if len(syms) != 1:
        raise ValueError(f"Legs are on different symbols ({', '.join(sorted(syms))}).")
    sym = syms.pop()

    cost = _PASTE_COST_RE.search(text)
    if not cost:
        raise ValueError(
            "Couldn't find the entry cost. Include the 'Average cost $X.XX' line — "
            "the per-leg prices on that screen are CURRENT marks, not your entry."
        )
    net = float(cost.group("amt"))
    if cost.group("neg"):
        net = -net

    qty_m = _PASTE_QTY_RE.search(text)
    leg_qtys = [int(m.group("qty")) for m in matches]
    qty = int(qty_m.group("n")) if qty_m else min(leg_qtys)
    if qty < 1:
        raise ValueError("Quantity must be at least 1.")
    # A negative Quantity means the structure was SOLD: the cost shown is a credit.
    if qty_m and qty_m.group("sign") in ("-", "−") and net > 0:
        net = -net

    legs: list[ImportedLeg] = []
    for m in matches:
        exp = _parse_line_date(m.group("date"), today)  # expiries roll FORWARD
        is_long = m.group("side").lower().startswith("buy")
        legs.append(ImportedLeg(
            strike=float(m.group("strike")),
            option_type=OptionType.CALL if m.group("type").lower() == "call" else OptionType.PUT,
            is_long=is_long, quantity=qty, entry_price_per_share=0.0, expiration=exp,
        ))
    if len(legs) == 1 and not legs[0].is_long:
        raise ValueError("A lone short option is undefined-risk — not supported. "
                         "If this is one wing of a spread, include both legs.")
    if len(legs) == 2 and legs[0].is_long == legs[1].is_long:
        raise ValueError("Both legs parsed as the same side — a vertical needs one "
                         "Buys leg and one Sells leg. Check the paste includes both lines.")

    opened_m = _PASTE_OPENED_RE.search(text)
    opened = _parse_past_date(opened_m.group("d"), today) if opened_m else None
    return sym, legs, net, qty, opened
