"""Contract selection.

Answers: *which contract best expresses the thesis?* For the initial build we
select a single long option (defined risk = debit) appropriate for a small
account. The interface returns a scored, liquidity-gated best contract so the
trade-plan builder can size it.

Selection criteria for a long directional thesis:
  * Correct right (call for bullish, put for bearish).
  * Target expiration window (default 20-45 DTE) to balance theta vs cost.
  * Target delta band (default 0.35-0.55) — meaningful directional exposure
    without paying deep-ITM prices a $2k account can't afford.
  * Must pass the option liquidity gate; among survivors, maximize a blended
    score of liquidity and delta-fit.

Spreads (defined-risk verticals) are a planned extension; the selection API is
shaped to accommodate returning multiple legs later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.domain.enums import Direction, OptionType
from app.domain.options import OptionChain, OptionContract
from app.engine.liquidity import (
    OptionLiquidityConfig,
    gate_option,
    option_liquidity_score,
)


@dataclass(frozen=True)
class SelectionConfig:
    min_dte: int = 20
    max_dte: int = 45
    target_delta: float = 0.45
    min_delta: float = 0.30
    max_delta: float = 0.60


@dataclass(frozen=True)
class ContractChoice:
    contract: OptionContract
    fit_score: float


@dataclass(frozen=True)
class SpreadChoice:
    """A defined-risk vertical: long the nearer-the-money leg, short the wing.

    net_debit_per_share is positive for a debit spread. max_loss_per_contract is
    the defined risk in dollars (debit * 100). This is the primary structure for
    a small account — it makes mega-cap options affordable within a tight
    per-trade risk cap where a single long option cannot fit.
    """

    long_leg: OptionContract
    short_leg: OptionContract
    net_debit_per_share: float
    width: float
    max_loss_per_contract: float
    max_profit_per_contract: float
    fit_score: float

    @property
    def reward_to_risk(self) -> float | None:
        if self.max_loss_per_contract > 0:
            return round(self.max_profit_per_contract / self.max_loss_per_contract, 3)
        return None


def select_long_contract(
    chain: OptionChain,
    direction: Direction,
    as_of: date,
    sel: SelectionConfig | None = None,
    liq: OptionLiquidityConfig | None = None,
) -> ContractChoice | None:
    sel = sel or SelectionConfig()
    liq = liq or OptionLiquidityConfig()

    if direction == Direction.BULLISH:
        want = OptionType.CALL
    elif direction == Direction.BEARISH:
        want = OptionType.PUT
    else:
        return None  # neutral/vol theses use different structures (future work)

    best: ContractChoice | None = None
    for c in chain.contracts:
        if c.option_type != want:
            continue
        dte = c.dte(as_of)
        if not (sel.min_dte <= dte <= sel.max_dte):
            continue
        if gate_option(c, liq):
            continue  # failed hard liquidity gate

        delta = abs(c.greeks.delta) if c.greeks.delta is not None else None
        if delta is None or not (sel.min_delta <= delta <= sel.max_delta):
            continue

        delta_fit = 1.0 - abs(delta - sel.target_delta) / sel.target_delta
        liq_score = option_liquidity_score(c, liq)
        # DTE fit: prefer the middle of the window.
        mid_dte = (sel.min_dte + sel.max_dte) / 2
        dte_fit = 1.0 - abs(dte - mid_dte) / mid_dte
        fit = round(0.5 * liq_score + 0.35 * delta_fit + 0.15 * dte_fit, 4)

        if best is None or fit > best.fit_score:
            best = ContractChoice(contract=c, fit_score=fit)

    return best


def select_vertical_spread(
    chain: OptionChain,
    direction: Direction,
    as_of: date,
    *,
    max_debit_usd: float,
    sel: SelectionConfig | None = None,
    liq: OptionLiquidityConfig | None = None,
) -> SpreadChoice | None:
    """Select a defined-risk debit vertical whose per-contract risk (debit*100)
    is <= `max_debit_usd`.

    Bullish -> bull call spread (long lower-strike call, short higher-strike call).
    Bearish -> bear put spread (long higher-strike put, short lower-strike put).
    Neutral/vol -> not handled here (future work).

    Both legs must independently pass the option liquidity gate. Among feasible
    spreads we prefer the best reward-to-risk that still fits the debit cap, then
    tighter/liquid legs.
    """
    sel = sel or SelectionConfig()
    liq = liq or OptionLiquidityConfig()

    if direction == Direction.BULLISH:
        want = OptionType.CALL
    elif direction == Direction.BEARISH:
        want = OptionType.PUT
    else:
        return None

    # Candidate long legs: in the target delta band, right DTE, liquid.
    legs = [
        c
        for c in chain.contracts
        if c.option_type == want
        and sel.min_dte <= c.dte(as_of) <= sel.max_dte
        and not gate_option(c, liq)
        and c.greeks.delta is not None
        and sel.min_delta <= abs(c.greeks.delta) <= sel.max_delta
        and c.mid is not None
    ]
    if not legs:
        return None

    best: SpreadChoice | None = None
    for long_leg in legs:
        # Short leg is further OTM on the same expiration.
        for short_leg in chain.contracts:
            if (
                short_leg.option_type != want
                or short_leg.expiration != long_leg.expiration
                or short_leg.mid is None
                or gate_option(short_leg, liq)
            ):
                continue

            if want == OptionType.CALL and short_leg.strike <= long_leg.strike:
                continue
            if want == OptionType.PUT and short_leg.strike >= long_leg.strike:
                continue

            width = abs(short_leg.strike - long_leg.strike)
            debit = long_leg.mid - short_leg.mid  # type: ignore[operator]
            if debit <= 0 or width <= 0:
                continue

            max_loss = round(debit * 100, 2)
            if max_loss > max_debit_usd:
                continue

            max_profit = round((width - debit) * 100, 2)
            rr = max_profit / max_loss if max_loss > 0 else 0.0
            # Prefer good reward-to-risk and a width that isn't trivially thin.
            fit = round(min(1.0, rr / 2.0) * 0.7 + min(1.0, width / long_leg.strike * 20) * 0.3, 4)

            if best is None or fit > best.fit_score:
                best = SpreadChoice(
                    long_leg=long_leg,
                    short_leg=short_leg,
                    net_debit_per_share=round(debit, 4),
                    width=round(width, 2),
                    max_loss_per_contract=max_loss,
                    max_profit_per_contract=max_profit,
                    fit_score=fit,
                )

    return best
