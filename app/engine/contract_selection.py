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

from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
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
    # Opt-in moneyness fallback: when a liquid contract's delta is missing or
    # degenerate (0.0/1.0 — common for provider 0DTE single-stock greeks), accept
    # it if |strike/spot - 1| <= this band, picking the closest-to-ATM strike.
    # None (default) keeps the strict delta-only behavior for the core scanner.
    moneyness_fallback_pct: float | None = None


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


def _moneyness_fit(c, chain, sel: SelectionConfig) -> float | None:
    """ATM-closeness proxy [0,1] for when delta is missing/degenerate, or None if
    the fallback is disabled or the strike is outside the moneyness band. Only the
    near-ATM strikes (|strike/spot - 1| <= band) qualify; closest to ATM scores 1."""
    band = sel.moneyness_fallback_pct
    spot = chain.underlying_price
    if not band or not spot or spot <= 0 or not c.strike:
        return None
    money = abs(c.strike / spot - 1.0)
    if money > band:
        return None
    return round(1.0 - money / band, 4)


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
        by_delta = delta is not None and 0.0 < delta < 1.0 and sel.min_delta <= delta <= sel.max_delta
        money = _moneyness_fit(c, chain, sel) if not by_delta else None
        if not by_delta and money is None:
            continue

        # Fit uses the real delta when usable; otherwise the ATM-closeness proxy.
        delta_fit = (
            1.0 - abs(delta - sel.target_delta) / sel.target_delta if by_delta else money
        )
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

    # Candidate long legs: in the target delta band (or near-ATM by moneyness when
    # delta is missing/degenerate), right DTE, liquid.
    def _leg_ok(c) -> bool:
        if c.mid is None or gate_option(c, liq):
            return False
        d = abs(c.greeks.delta) if c.greeks.delta is not None else None
        if d is not None and 0.0 < d < 1.0 and sel.min_delta <= d <= sel.max_delta:
            return True
        return _moneyness_fit(c, chain, sel) is not None

    legs = [
        c
        for c in chain.contracts
        if c.option_type == want
        and sel.min_dte <= c.dte(as_of) <= sel.max_dte
        and _leg_ok(c)
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


# ---------------------------------------------------------------------------
# Multi-leg structures (credit verticals, straddles/strangles, iron condors)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructureLeg:
    contract: OptionContract
    action: OptionAction


@dataclass(frozen=True)
class StructureChoice:
    """A generic, sized-later multi-leg structure.

    net_debit_per_share is positive for a net debit, negative for a net credit.
    max_profit_per_contract is None when upside is uncapped (straddle/strangle).
    """

    strategy: StrategyType
    stance: Direction
    legs: list[StructureLeg]
    net_debit_per_share: float
    max_loss_per_contract: float
    max_profit_per_contract: float | None
    fit_score: float


def _choose_expiration(chain: OptionChain, as_of: date, sel: SelectionConfig) -> date | None:
    exps = {c.expiration for c in chain.contracts if sel.min_dte <= c.dte(as_of) <= sel.max_dte}
    if not exps:
        return None
    mid = (sel.min_dte + sel.max_dte) / 2
    return min(exps, key=lambda e: abs((e - as_of).days - mid))


def _liquid_by_delta(
    chain: OptionChain,
    otype: OptionType,
    exp: date,
    target_delta: float,
    liq: OptionLiquidityConfig,
) -> OptionContract | None:
    cands = [
        c
        for c in chain.contracts
        if c.option_type == otype
        and c.expiration == exp
        and c.greeks.delta is not None
        and c.mid
        and not gate_option(c, liq)
    ]
    if not cands:
        return None
    return min(cands, key=lambda c: abs(abs(c.greeks.delta) - target_delta))


def select_credit_vertical(
    chain: OptionChain,
    direction: Direction,
    as_of: date,
    *,
    max_risk_usd: float,
    short_delta: float = 0.30,
    long_delta: float = 0.15,
    sel: SelectionConfig | None = None,
    liq: OptionLiquidityConfig | None = None,
) -> StructureChoice | None:
    """Sell a ~short_delta option, buy a further-OTM wing for defined risk.

    Bullish -> bull PUT spread (sell higher put, buy lower put).
    Bearish -> bear CALL spread (sell lower call, buy higher call).
    Credit is collected up front; max loss = (width - credit).
    """
    sel = sel or SelectionConfig()
    liq = liq or OptionLiquidityConfig()
    exp = _choose_expiration(chain, as_of, sel)
    if exp is None:
        return None

    if direction == Direction.BULLISH:
        otype, strategy = OptionType.PUT, StrategyType.BULL_PUT_SPREAD
    elif direction == Direction.BEARISH:
        otype, strategy = OptionType.CALL, StrategyType.BEAR_CALL_SPREAD
    else:
        return None

    short = _liquid_by_delta(chain, otype, exp, short_delta, liq)
    long = _liquid_by_delta(chain, otype, exp, long_delta, liq)
    if not short or not long or short.mid is None or long.mid is None:
        return None
    # The long (protective) wing must be further OTM than the short.
    if otype == OptionType.PUT and long.strike >= short.strike:
        return None
    if otype == OptionType.CALL and long.strike <= short.strike:
        return None

    width = abs(short.strike - long.strike)
    credit = short.mid - long.mid
    if credit <= 0 or width <= 0:
        return None
    max_loss = round((width - credit) * 100, 2)
    if max_loss <= 0 or max_loss > max_risk_usd:
        return None

    rr = (credit * 100) / max_loss if max_loss > 0 else 0.0
    return StructureChoice(
        strategy=strategy,
        stance=direction,
        legs=[
            StructureLeg(short, OptionAction.SELL_TO_OPEN),
            StructureLeg(long, OptionAction.BUY_TO_OPEN),
        ],
        net_debit_per_share=round(-credit, 4),
        max_loss_per_contract=max_loss,
        max_profit_per_contract=round(credit * 100, 2),
        fit_score=round(min(1.0, rr), 4),
    )


def select_straddle(
    chain: OptionChain,
    as_of: date,
    *,
    max_debit_usd: float,
    sel: SelectionConfig | None = None,
    liq: OptionLiquidityConfig | None = None,
) -> StructureChoice | None:
    """Long ATM call + ATM put (same strike) — a long-volatility structure."""
    sel = sel or SelectionConfig()
    liq = liq or OptionLiquidityConfig()
    exp = _choose_expiration(chain, as_of, sel)
    if exp is None or not chain.underlying_price:
        return None
    spot = chain.underlying_price

    calls = [
        c
        for c in chain.contracts
        if c.option_type == OptionType.CALL
        and c.expiration == exp
        and c.mid
        and not gate_option(c, liq)
    ]
    if not calls:
        return None
    atm = min(calls, key=lambda c: abs(c.strike - spot)).strike
    call = next((c for c in calls if c.strike == atm), None)
    put = next(
        (
            c
            for c in chain.contracts
            if c.option_type == OptionType.PUT
            and c.expiration == exp
            and c.strike == atm
            and c.mid
            and not gate_option(c, liq)
        ),
        None,
    )
    if not call or not put or call.mid is None or put.mid is None:
        return None

    debit = call.mid + put.mid
    max_loss = round(debit * 100, 2)
    if max_loss <= 0 or max_loss > max_debit_usd:
        return None
    return StructureChoice(
        strategy=StrategyType.LONG_STRADDLE,
        stance=Direction.VOL_LONG,
        legs=[
            StructureLeg(call, OptionAction.BUY_TO_OPEN),
            StructureLeg(put, OptionAction.BUY_TO_OPEN),
        ],
        net_debit_per_share=round(debit, 4),
        max_loss_per_contract=max_loss,
        max_profit_per_contract=None,  # uncapped
        fit_score=0.5,
    )


def select_strangle(
    chain: OptionChain,
    as_of: date,
    *,
    max_debit_usd: float,
    wing_delta: float = 0.30,
    sel: SelectionConfig | None = None,
    liq: OptionLiquidityConfig | None = None,
) -> StructureChoice | None:
    """Long ~wing_delta OTM call + OTM put — cheaper long-vol than a straddle."""
    sel = sel or SelectionConfig()
    liq = liq or OptionLiquidityConfig()
    exp = _choose_expiration(chain, as_of, sel)
    if exp is None:
        return None
    call = _liquid_by_delta(chain, OptionType.CALL, exp, wing_delta, liq)
    put = _liquid_by_delta(chain, OptionType.PUT, exp, wing_delta, liq)
    if not call or not put or call.mid is None or put.mid is None:
        return None
    debit = call.mid + put.mid
    max_loss = round(debit * 100, 2)
    if max_loss <= 0 or max_loss > max_debit_usd:
        return None
    return StructureChoice(
        strategy=StrategyType.LONG_STRANGLE,
        stance=Direction.VOL_LONG,
        legs=[
            StructureLeg(call, OptionAction.BUY_TO_OPEN),
            StructureLeg(put, OptionAction.BUY_TO_OPEN),
        ],
        net_debit_per_share=round(debit, 4),
        max_loss_per_contract=max_loss,
        max_profit_per_contract=None,
        fit_score=0.5,
    )


def select_iron_condor(
    chain: OptionChain,
    as_of: date,
    *,
    max_risk_usd: float,
    short_delta: float = 0.20,
    long_delta: float = 0.10,
    sel: SelectionConfig | None = None,
    liq: OptionLiquidityConfig | None = None,
) -> StructureChoice | None:
    """Sell an OTM put spread + an OTM call spread — a defined-risk short-vol,
    range-bound structure. Max loss = wider wing width - net credit."""
    sel = sel or SelectionConfig()
    liq = liq or OptionLiquidityConfig()
    exp = _choose_expiration(chain, as_of, sel)
    if exp is None:
        return None

    sp = _liquid_by_delta(chain, OptionType.PUT, exp, short_delta, liq)
    lp = _liquid_by_delta(chain, OptionType.PUT, exp, long_delta, liq)
    sc = _liquid_by_delta(chain, OptionType.CALL, exp, short_delta, liq)
    lc = _liquid_by_delta(chain, OptionType.CALL, exp, long_delta, liq)
    if not (sp and lp and sc and lc):
        return None
    if not (lp.strike < sp.strike < sc.strike < lc.strike):
        return None
    if sp.mid is None or lp.mid is None or sc.mid is None or lc.mid is None:
        return None

    credit = (sp.mid + sc.mid) - (lp.mid + lc.mid)
    put_width = sp.strike - lp.strike
    call_width = lc.strike - sc.strike
    width = max(put_width, call_width)
    if credit <= 0:
        return None
    max_loss = round((width - credit) * 100, 2)
    if max_loss <= 0 or max_loss > max_risk_usd:
        return None
    rr = (credit * 100) / max_loss if max_loss > 0 else 0.0
    return StructureChoice(
        strategy=StrategyType.IRON_CONDOR,
        stance=Direction.VOL_SHORT,
        legs=[
            StructureLeg(sp, OptionAction.SELL_TO_OPEN),
            StructureLeg(lp, OptionAction.BUY_TO_OPEN),
            StructureLeg(sc, OptionAction.SELL_TO_OPEN),
            StructureLeg(lc, OptionAction.BUY_TO_OPEN),
        ],
        net_debit_per_share=round(-credit, 4),
        max_loss_per_contract=max_loss,
        max_profit_per_contract=round(credit * 100, 2),
        fit_score=round(min(1.0, rr), 4),
    )
