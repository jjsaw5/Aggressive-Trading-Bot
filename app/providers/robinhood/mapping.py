"""Pure mapping from robin_stocks response dicts to domain models.

Kept separate from the client so the parsing logic is unit-testable without the
robin_stocks library, live auth, or network. Robinhood returns numeric fields as
STRINGS (e.g. "105.230000"), so everything is parsed defensively via `_f`.

Field names are grounded in robin_stocks 3.4.0 (module `robin_stocks.robinhood`);
see docs/providers/ROBINHOOD.md. Missing/None fields degrade to None rather than
being fabricated.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.domain.enums import OptionType
from app.domain.market import Candle, Quote
from app.domain.options import Greeks, OptionContract


def _f(row: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _i(row: dict[str, Any], *keys: str) -> int | None:
    v = _f(row, *keys)
    return int(v) if v is not None else None


def _dt(value: Any) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def parse_quote(row: dict[str, Any], symbol: str, now: datetime) -> Quote:
    price = _f(row, "last_trade_price", "last_extended_hours_trade_price") or 0.0
    return Quote(
        symbol=symbol.upper(),
        price=price,
        bid=_f(row, "bid_price"),
        ask=_f(row, "ask_price"),
        volume=None,  # get_quotes does not carry volume
        prev_close=_f(row, "previous_close", "adjusted_previous_close"),
        as_of=now,
        delayed_minutes=0,  # Confirm actual freshness for your account tier.
        source="robinhood",
    )


def parse_candles(rows: list[dict[str, Any]]) -> list[Candle]:
    candles: list[Candle] = []
    for r in rows or []:
        close = _f(r, "close_price")
        if close is None:
            continue
        candles.append(
            Candle(
                ts=_dt(r.get("begins_at")),
                open=_f(r, "open_price") or close,
                high=_f(r, "high_price") or close,
                low=_f(r, "low_price") or close,
                close=close,
                volume=_i(r, "volume") or 0,
            )
        )
    return candles


def parse_option_contract(
    row: dict[str, Any], symbol: str, now: datetime
) -> OptionContract | None:
    raw_type = str(row.get("type", "")).lower()
    if raw_type.startswith("c"):
        otype = OptionType.CALL
    elif raw_type.startswith("p"):
        otype = OptionType.PUT
    else:
        return None

    strike = _f(row, "strike_price")
    exp_raw = row.get("expiration_date")
    if strike is None or not exp_raw:
        return None
    try:
        expiration = date.fromisoformat(str(exp_raw)[:10])
    except ValueError:
        return None

    return OptionContract(
        symbol=symbol.upper(),
        option_symbol=row.get("occ_symbol") or row.get("id"),
        expiration=expiration,
        strike=strike,
        option_type=otype,
        bid=_f(row, "bid_price"),
        ask=_f(row, "ask_price"),
        mark=_f(row, "mark_price", "adjusted_mark_price"),
        last=_f(row, "last_trade_price"),
        volume=_i(row, "volume"),
        open_interest=_i(row, "open_interest"),
        implied_volatility=_f(row, "implied_volatility"),
        greeks=Greeks(
            delta=_f(row, "delta"),
            gamma=_f(row, "gamma"),
            theta=_f(row, "theta"),
            vega=_f(row, "vega"),
            rho=_f(row, "rho"),
        ),
        as_of=now,
        delayed_minutes=0,
        source="robinhood",
    )
