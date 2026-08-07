"""Contract selection for the four allowed strategies.

Selection runs **after** the underlying thesis has been validated, against a
freshly-retrieved chain, and never before the options market is open. See
`app.multiagent.stages` for why that ordering is enforced rather than merely
recommended.

Two rules govern the choice.

**Do not select a contract merely because it is cheap.** The selector ranks by a
composite of delta fit, liquidity and cost drag; premium enters only through the
risk budget, as a constraint. A $12 contract with a 40% spread and 3 open
interest loses to a $180 contract that can actually be exited.

**Greeks are labeled.** Not every provider supplies them. Where the chain gives
Greeks they are used as PROVIDER; where it does not they are computed from
Black-Scholes and carried as MODELED, all the way to the report
(CLAUDE.md §4, "Modeled is labeled").
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from app.domain.enums import OptionAction, OptionType, StrategyType
from app.domain.options import Greeks, OptionChain, OptionContract
from app.multiagent.config import ContractConfig
from app.multiagent.models.contracts import ProposedLeg, ProposedStructure
from app.multiagent.models.enums import Direction
from app.multiagent.models.measurements import Provenance
from app.quant.pricing import black_scholes_greeks
from app.quant.probability import probability_of_profit

# Risk-free rate for modeled Greeks. A single documented constant beats the same
# number appearing in four call sites.
_RATE = 0.04


class NoContractError(RuntimeError):
    """Raised when no contract in the chain satisfies the configured bands."""


def _mid(c: OptionContract) -> float | None:
    if c.bid is not None and c.ask is not None:
        return (c.bid + c.ask) / 2.0
    return c.mark if c.mark is not None else c.last


def _spread_pct(c: OptionContract) -> float | None:
    if c.bid is None or c.ask is None:
        return None
    mid = (c.bid + c.ask) / 2.0
    return None if mid <= 0 else (c.ask - c.bid) / mid


def strike_invariant_greeks(chain: OptionChain) -> set[str]:
    """Names of second-order Greeks the provider reports identically at every strike.

    Gamma, theta and vega all vary strongly with moneyness. A chain that reports
    one constant for every strike and expiry is not measuring them — it is
    filling the field. Taking such a value at face value produces confidently
    wrong arithmetic: a vertical's net theta comes out as *exactly* zero, the
    theta-burden rule then passes for free, and the excessive-theta hard rule can
    never fire.

    So they are detected and recomputed from Black-Scholes, labeled MODELED.
    Delta is excluded from this check because a genuinely flat delta is possible
    across a narrow sample; the second-order Greeks are not.
    """
    flat: set[str] = set()
    for name in ("gamma", "theta", "vega"):
        values = {
            getattr(c.greeks, name)
            for c in chain.contracts
            if getattr(c.greeks, name, None) is not None
        }
        # More than a handful of contracts sharing exactly one value is the tell.
        if len(values) == 1 and len(chain.contracts) > 5:
            flat.add(name)
    return flat


def _resolve_greeks(
    c: OptionContract,
    spot: float | None,
    now: datetime,
    distrusted: frozenset[str] = frozenset(),
) -> tuple[Greeks, Provenance]:
    """Provider Greeks where trustworthy, Black-Scholes where not — always labeled."""
    g = c.greeks
    needed = {
        name
        for name in ("delta", "gamma", "theta", "vega")
        if getattr(g, name, None) is None or name in distrusted
    }
    if not needed:
        return g, Provenance.PROVIDER
    if spot is None or c.implied_volatility is None:
        # No basis to model from. Absent stays absent — a missing Greek is
        # reported as missing rather than filled with a plausible number.
        return g, Provenance.PROVIDER if g.delta is not None else Provenance.MODELED

    t_years = max((c.expiration - now.date()).days, 0) / 365.0
    modeled = black_scholes_greeks(
        spot, c.strike, t_years, c.implied_volatility, c.option_type, _RATE
    )

    def _pick(name: str, places: int) -> float | None:
        if name in needed:
            return round(modeled[name], places)
        return getattr(g, name)

    return (
        Greeks(
            delta=_pick("delta", 4),
            gamma=_pick("gamma", 6),
            theta=_pick("theta", 4),
            vega=_pick("vega", 4),
            rho=g.rho,
        ),
        Provenance.MODELED,
    )


def _to_leg(
    c: OptionContract,
    action: OptionAction,
    *,
    spot: float | None,
    now: datetime,
    distrusted: frozenset[str] = frozenset(),
) -> ProposedLeg:
    greeks, source = _resolve_greeks(c, spot, now, distrusted)
    return ProposedLeg(
        option_symbol=c.option_symbol,
        underlying=c.symbol,
        expiration=c.expiration,
        strike=c.strike,
        option_type=c.option_type,
        action=action,
        bid=c.bid,
        ask=c.ask,
        mark=c.mark if c.mark is not None else _mid(c),
        last=c.last,
        volume=c.volume,
        open_interest=c.open_interest,
        implied_volatility=c.implied_volatility,
        greeks=greeks,
        greeks_source=source,
        as_of=c.as_of,
        delayed_minutes=c.delayed_minutes,
        source=c.source,
    )


def _candidate_expirations(chain: OptionChain, cfg: ContractConfig, today: date) -> list[date]:
    """Expirations inside the configured DTE band, nearest the midpoint first."""
    target = (cfg.preferred_dte_min + cfg.preferred_dte_max) / 2.0
    dates = sorted({c.expiration for c in chain.contracts})
    in_band = [d for d in dates if cfg.preferred_dte_min <= (d - today).days <= cfg.preferred_dte_max]
    in_band.sort(key=lambda d: abs((d - today).days - target))
    return in_band[: cfg.max_expirations_considered]


def _tradable(c: OptionContract) -> bool:
    """A contract with no two-sided market cannot be priced, so it cannot be chosen."""
    return c.bid is not None and c.ask is not None and c.ask > 0 and _mid(c) not in (None, 0)


def strike_increment(strikes: list[float]) -> float | None:
    """The modal gap between adjacent strikes, or None if undeterminable."""
    ordered = sorted(set(strikes))
    if len(ordered) < 2:
        return None
    gaps: dict[float, int] = {}
    for a, b in zip(ordered, ordered[1:], strict=False):
        gap = round(b - a, 4)
        if gap > 0:
            gaps[gap] = gaps.get(gap, 0) + 1
    if not gaps:
        return None
    return max(gaps.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def candidate_widths(cfg: ContractConfig, strikes: list[float]) -> list[float]:
    """Spread widths to try, in dollars.

    The configured widths are **absolute dollars**, which quietly stops working
    at both ends of the price range: on a $20 underlying a $2.50 spread puts the
    short leg 12.5% out of the money at ~0.14 delta — below the configured short
    band, so no spread is ever built — while on a $900 underlying it is barely
    one strike wide. Either way the selector returns "no debit vertical could be
    built" for a chain that plainly supports one.

    So the configured widths are supplemented with multiples of the chain's own
    strike increment. That adapts to whatever grid the underlying actually
    trades on without giving up the explicit configured widths.
    """
    widths = list(cfg.spread_widths)
    increment = strike_increment(strikes)
    if increment:
        widths.extend(round(increment * n, 4) for n in cfg.spread_width_strike_steps)
    # Narrowest first: a tighter spread costs less and is more likely to size
    # inside the risk budget, and the ranking still decides on merit.
    seen: dict[float, None] = {}
    for w in sorted(widths):
        if w > 0:
            seen.setdefault(w, None)
    return list(seen)


def _delta_of(
    c: OptionContract,
    spot: float | None,
    now: datetime,
    distrusted: frozenset[str] = frozenset(),
) -> float | None:
    greeks, _ = _resolve_greeks(c, spot, now, distrusted)
    return None if greeks.delta is None else abs(greeks.delta)


def _fits_budget(max_loss_per_contract: float | None, max_risk_usd: float) -> bool:
    """Whether at least one contract can be bought inside the risk budget.

    A structure that cannot be sized is not a cheaper trade, it is not a trade.
    Ranking it above a sizeable alternative would hand every candidate to the
    rules engine to reject, which is a broken selector wearing a correct
    rejection.
    """
    return max_loss_per_contract is not None and 0 < max_loss_per_contract <= max_risk_usd


def _score_long(
    c: OptionContract,
    *,
    spot: float | None,
    now: datetime,
    cfg: ContractConfig,
    max_risk_usd: float,
    distrusted: frozenset[str] = frozenset(),
) -> float | None:
    """Rank a long candidate. Higher is better. None disqualifies.

    Blends four terms so no single one dominates. Note what is *not* a term:
    premium. Cheapness enters only as the sizeability constraint below, never as
    a reward — "do not select a contract merely because it is cheap".
    """
    delta = _delta_of(c, spot, now, distrusted)
    if delta is None or not (cfg.long_delta_min <= delta <= cfg.long_delta_max):
        return None
    spread = _spread_pct(c)
    if spread is None:
        return None
    mid = _mid(c)
    max_loss = None if mid is None else mid * 100.0

    mid_delta = (cfg.long_delta_min + cfg.long_delta_max) / 2.0
    delta_fit = 1.0 - abs(delta - mid_delta) / max(mid_delta, 1e-9)
    tightness = max(0.0, 1.0 - spread / 0.25)
    oi = c.open_interest or 0
    vol = c.volume or 0
    liquidity = min(1.0, oi / 1000.0) * 0.6 + min(1.0, vol / 500.0) * 0.4
    base = 0.4 * delta_fit + 0.35 * tightness + 0.25 * liquidity
    # Sizeability dominates by a full point so any sizeable structure outranks
    # every unsizeable one, while still ordering sensibly inside each group.
    return round(base + (1.0 if _fits_budget(max_loss, max_risk_usd) else 0.0), 5)


def select_long_option(
    chain: OptionChain,
    direction: Direction,
    cfg: ContractConfig,
    *,
    candidate_id: str,
    run_id: str,
    now: datetime,
    max_risk_usd: float,
    expected_move_pct: float | None = None,
) -> ProposedStructure:
    """Best long call (bullish) or long put (bearish) within the bands."""
    today = now.date()
    spot = chain.underlying_price
    distrusted = frozenset(strike_invariant_greeks(chain))
    otype = OptionType.CALL if direction == Direction.BULLISH else OptionType.PUT

    best: tuple[float, OptionContract] | None = None
    for expiry in _candidate_expirations(chain, cfg, today):
        for c in chain.contracts:
            if c.expiration != expiry or c.option_type != otype or not _tradable(c):
                continue
            s = _score_long(
                c, spot=spot, now=now, cfg=cfg, max_risk_usd=max_risk_usd, distrusted=distrusted
            )
            if s is None:
                continue
            if best is None or s > best[0]:
                best = (s, c)

    if best is None:
        raise NoContractError(
            f"no {otype.value} in the {cfg.preferred_dte_min}-{cfg.preferred_dte_max} DTE band "
            f"had a two-sided market and a delta in [{cfg.long_delta_min}, {cfg.long_delta_max}]"
        )

    _, contract = best
    leg = _to_leg(contract, OptionAction.BUY_TO_OPEN, spot=spot, now=now, distrusted=distrusted)
    debit = _mid(contract)
    ask_debit = contract.ask

    structure = ProposedStructure(
        structure_id=str(uuid.uuid4()),
        candidate_id=candidate_id,
        run_id=run_id,
        ticker=chain.symbol,
        strategy_type=StrategyType.LONG_CALL if otype is OptionType.CALL else StrategyType.LONG_PUT,
        legs=[leg],
        underlying_price=spot,
        underlying_as_of=chain.as_of,
        net_debit_per_share=round(debit, 4) if debit is not None else None,
        net_debit_at_ask_per_share=round(ask_debit, 4) if ask_debit is not None else None,
        selected_at=now,
    )
    _finalise(structure, cfg, now=now, max_risk_usd=max_risk_usd, expected_move_pct=expected_move_pct)
    return structure


def select_vertical_spread(
    chain: OptionChain,
    direction: Direction,
    cfg: ContractConfig,
    *,
    candidate_id: str,
    run_id: str,
    now: datetime,
    max_risk_usd: float,
    expected_move_pct: float | None = None,
) -> ProposedStructure:
    """Best debit vertical: bull call spread (bullish) or bear put spread (bearish)."""
    today = now.date()
    spot = chain.underlying_price
    distrusted = frozenset(strike_invariant_greeks(chain))
    bullish = direction == Direction.BULLISH
    otype = OptionType.CALL if bullish else OptionType.PUT
    strategy = StrategyType.BULL_CALL_SPREAD if bullish else StrategyType.BEAR_PUT_SPREAD

    best: tuple[float, OptionContract, OptionContract] | None = None

    for expiry in _candidate_expirations(chain, cfg, today):
        legs = [
            c
            for c in chain.contracts
            if c.expiration == expiry and c.option_type == otype and _tradable(c)
        ]
        if len(legs) < 2:
            continue
        by_strike = {c.strike: c for c in legs}
        widths = candidate_widths(cfg, list(by_strike))

        for long_c in legs:
            ld = _delta_of(long_c, spot, now, distrusted)
            if ld is None or not (cfg.spread_long_delta_min <= ld <= cfg.spread_long_delta_max):
                continue
            for width in widths:
                # A bull call spread sells a HIGHER strike; a bear put spread
                # sells a LOWER one. Both are debits, both are defined risk.
                short_strike = long_c.strike + width if bullish else long_c.strike - width
                short_c = by_strike.get(short_strike)
                if short_c is None:
                    continue
                sd = _delta_of(short_c, spot, now, distrusted)
                if sd is None or not (
                    cfg.spread_short_delta_min <= sd <= cfg.spread_short_delta_max
                ):
                    continue
                long_mid, short_mid = _mid(long_c), _mid(short_c)
                if long_mid is None or short_mid is None:
                    continue
                debit = long_mid - short_mid
                if debit <= 0 or debit >= width:
                    # A debit at or above the width cannot profit; a non-positive
                    # one means the quotes are inconsistent, not that the trade
                    # is free.
                    continue
                s = _score_spread(
                    long_c,
                    short_c,
                    debit=debit,
                    width=width,
                    cfg=cfg,
                    spot=spot,
                    now=now,
                    max_risk_usd=max_risk_usd,
                    distrusted=distrusted,
                )
                if s is None:
                    continue
                if best is None or s > best[0]:
                    best = (s, long_c, short_c)

    if best is None:
        raise NoContractError(
            f"no debit vertical could be built: needed two tradable {otype.value} legs at one of "
            f"widths {candidate_widths(cfg, [c.strike for c in chain.contracts])} with long delta in "
            f"[{cfg.spread_long_delta_min}, {cfg.spread_long_delta_max}] and short delta in "
            f"[{cfg.spread_short_delta_min}, {cfg.spread_short_delta_max}]"
        )

    _, long_c, short_c = best
    legs = [
        _to_leg(long_c, OptionAction.BUY_TO_OPEN, spot=spot, now=now, distrusted=distrusted),
        _to_leg(short_c, OptionAction.SELL_TO_OPEN, spot=spot, now=now, distrusted=distrusted),
    ]
    long_mid, short_mid = _mid(long_c), _mid(short_c)
    debit = (long_mid - short_mid) if (long_mid is not None and short_mid is not None) else None
    # Crossing the spread on both legs: pay the ask, receive the bid.
    ask_debit = (
        (long_c.ask - short_c.bid)
        if (long_c.ask is not None and short_c.bid is not None)
        else None
    )

    structure = ProposedStructure(
        structure_id=str(uuid.uuid4()),
        candidate_id=candidate_id,
        run_id=run_id,
        ticker=chain.symbol,
        strategy_type=strategy,
        legs=legs,
        underlying_price=spot,
        underlying_as_of=chain.as_of,
        net_debit_per_share=round(debit, 4) if debit is not None else None,
        net_debit_at_ask_per_share=round(ask_debit, 4) if ask_debit is not None else None,
        width=abs(long_c.strike - short_c.strike),
        selected_at=now,
    )
    _finalise(structure, cfg, now=now, max_risk_usd=max_risk_usd, expected_move_pct=expected_move_pct)
    return structure


def _score_spread(
    long_c: OptionContract,
    short_c: OptionContract,
    *,
    debit: float,
    width: float,
    cfg: ContractConfig,
    spot: float | None,
    now: datetime,
    max_risk_usd: float,
    distrusted: frozenset[str] = frozenset(),
) -> float | None:
    """Rank a vertical. Payoff ratio matters, but so do exitability and fit."""
    ls, ss = _spread_pct(long_c), _spread_pct(short_c)
    if ls is None or ss is None:
        return None
    payoff = (width - debit) / debit  # reward-to-risk at expiry
    payoff_term = min(1.0, payoff / 3.0)
    tightness = max(0.0, 1.0 - max(ls, ss) / 0.25)
    oi = min(long_c.open_interest or 0, short_c.open_interest or 0)
    vol = min(long_c.volume or 0, short_c.volume or 0)
    liquidity = min(1.0, oi / 1000.0) * 0.6 + min(1.0, vol / 500.0) * 0.4
    delta = _delta_of(long_c, spot, now, distrusted)
    mid_delta = (cfg.spread_long_delta_min + cfg.spread_long_delta_max) / 2.0
    delta_fit = 1.0 - abs((delta or mid_delta) - mid_delta) / max(mid_delta, 1e-9)
    base = 0.35 * payoff_term + 0.3 * tightness + 0.2 * liquidity + 0.15 * delta_fit
    # As for long options: any sizeable structure outranks every unsizeable one.
    # Without this the selector reliably picks the widest, highest-payoff spread
    # and then reports it as unsizeable.
    return round(base + (1.0 if _fits_budget(debit * 100.0, max_risk_usd) else 0.0), 5)


def _finalise(
    s: ProposedStructure,
    cfg: ContractConfig,
    *,
    now: datetime,
    max_risk_usd: float,
    expected_move_pct: float | None,
) -> None:
    """Fill payoff, sizing, net Greeks, cost drag and POP. Never guesses."""
    debit = s.net_debit_per_share
    bullish = s.strategy_type in (StrategyType.LONG_CALL, StrategyType.BULL_CALL_SPREAD)
    long_leg = next((leg for leg in s.legs if leg.action == OptionAction.BUY_TO_OPEN), None)

    if debit is not None:
        s.max_loss_per_contract = round(debit * 100.0, 2)
        if s.width is not None:
            s.max_profit_per_contract = round((s.width - debit) * 100.0, 2)
        # A long single option has unbounded (call) or strike-bounded (put)
        # upside. Leaving max_profit None is the honest statement; the risk/reward
        # rule uses a target-based figure instead.

    if long_leg is not None and debit is not None:
        s.breakeven = round(
            long_leg.strike + debit if bullish else long_leg.strike - debit, 4
        )

    # Position size: the largest contract count whose defined risk fits the
    # budget, capped by the concentration limit. Floor, never round up.
    if s.max_loss_per_contract and s.max_loss_per_contract > 0:
        n = int(max_risk_usd // s.max_loss_per_contract)
        s.contracts = max(0, min(n, cfg_max_contracts()))
        if s.contracts == 0:
            s.selection_notes.append(
                f"one contract risks ${s.max_loss_per_contract:,.2f}, above the "
                f"${max_risk_usd:,.2f} per-trade budget — unsizeable"
            )

    # Net Greeks, signed by action.
    def _net(attr: str) -> float | None:
        vals = []
        for leg in s.legs:
            v = getattr(leg.greeks, attr, None)
            if v is None:
                return None  # a partial sum would be a fabricated total
            vals.append(v if leg.action == OptionAction.BUY_TO_OPEN else -v)
        return round(sum(vals), 6) if vals else None

    s.net_delta = _net("delta")
    s.net_gamma = _net("gamma")
    s.net_theta = _net("theta")
    s.net_vega = _net("vega")
    s.greeks_source = (
        Provenance.MODELED
        if any(leg.greeks_source is Provenance.MODELED for leg in s.legs)
        else Provenance.PROVIDER
    )

    # Round-trip spread tax as a share of defined max loss.
    if (
        s.net_debit_at_ask_per_share is not None
        and debit is not None
        and s.max_loss_per_contract
    ):
        cross = (s.net_debit_at_ask_per_share - debit) * 2.0 * 100.0
        s.cost_drag_pct = round(cross / s.max_loss_per_contract, 4)

    # Probability of profit from the market-implied distribution.
    iv = long_leg.implied_volatility if long_leg else None
    dte = s.dte(now.date())
    if s.breakeven is not None and s.underlying_price and iv and dte is not None and dte > 0:
        s.probability_of_profit = probability_of_profit(
            spot=s.underlying_price, breakeven=s.breakeven, iv=iv, days=float(dte), bullish=bullish
        )
        s.pop_source = Provenance.MODELED
    else:
        s.selection_notes.append(
            "probability of profit uncomputable (needs spot, IV, breakeven and positive DTE)"
        )

    if expected_move_pct is not None and s.breakeven and s.underlying_price:
        needed = abs(s.breakeven - s.underlying_price) / s.underlying_price * 100.0
        s.selection_notes.append(
            f"needs a {needed:.2f}% move to break even against a claimed {expected_move_pct:.2f}% expected move"
        )


def cfg_max_contracts() -> int:
    """Concentration cap from the risk policy.

    Read from `app.config.settings` rather than the methodology file: it is a
    hard risk limit (`docs/RISK_POLICY.md`), not a tunable methodology knob, and
    it must be the same number the rest of the platform enforces.
    """
    from app.config import settings

    return int(settings.max_contracts_per_trade)


def select_structure(
    chain: OptionChain,
    strategy: StrategyType,
    direction: Direction,
    cfg: ContractConfig,
    *,
    candidate_id: str,
    run_id: str,
    now: datetime | None = None,
    max_risk_usd: float,
    expected_move_pct: float | None = None,
) -> ProposedStructure:
    """Dispatch to the selector for an allowed strategy."""
    when = now or datetime.now(UTC)
    kwargs = {
        "candidate_id": candidate_id,
        "run_id": run_id,
        "now": when,
        "max_risk_usd": max_risk_usd,
        "expected_move_pct": expected_move_pct,
    }
    if strategy in (StrategyType.LONG_CALL, StrategyType.LONG_PUT):
        return select_long_option(chain, direction, cfg, **kwargs)
    if strategy in (StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD):
        return select_vertical_spread(chain, direction, cfg, **kwargs)
    raise NoContractError(
        f"strategy {strategy.value} is not in the allowed set for this milestone "
        "(long_call, long_put, bull_call_spread, bear_put_spread)"
    )


def _spread_equivalent(strategy: StrategyType) -> StrategyType | None:
    return {
        StrategyType.LONG_CALL: StrategyType.BULL_CALL_SPREAD,
        StrategyType.LONG_PUT: StrategyType.BEAR_PUT_SPREAD,
    }.get(strategy)


def propose_structures(
    chain: OptionChain,
    strategy: StrategyType,
    direction: Direction,
    cfg: ContractConfig,
    *,
    candidate_id: str,
    run_id: str,
    now: datetime | None = None,
    max_risk_usd: float,
    expected_move_pct: float | None = None,
    allowed_strategies: set[str] | None = None,
) -> tuple[list[ProposedStructure], list[str]]:
    """The agent's strategy, plus a spread fallback when it cannot be sized.

    A single long option on a $130 underlying routinely costs more than the
    $100 per-trade risk cap in `docs/RISK_POLICY.md`. Rejecting the whole
    candidate for that would discard a validated thesis over an expression
    choice the agent was explicitly told not to make. So: if the requested
    structure is unsizeable, the defined-risk vertical is offered alongside it
    and the reason is recorded.

    This never *loosens* the risk cap — the fallback is a cheaper structure, not
    a bigger budget — and it never introduces a strategy outside the allow-list.

    Returns (structures, notes). Best-first; empty when nothing could be built.
    """
    when = now or datetime.now(UTC)
    allowed = allowed_strategies if allowed_strategies is not None else {s.value for s in _ALLOWED}
    notes: list[str] = []
    out: list[ProposedStructure] = []

    def _try(strat: StrategyType) -> ProposedStructure | None:
        if strat.value not in allowed:
            notes.append(f"{strat.value} is not in the configured allow-list — not attempted")
            return None
        try:
            return select_structure(
                chain,
                strat,
                direction,
                cfg,
                candidate_id=candidate_id,
                run_id=run_id,
                now=when,
                max_risk_usd=max_risk_usd,
                expected_move_pct=expected_move_pct,
            )
        except NoContractError as exc:
            notes.append(f"{strat.value}: {exc}")
            return None

    primary = _try(strategy)
    if primary is not None:
        out.append(primary)

    needs_fallback = primary is None or primary.contracts == 0
    fallback_strategy = _spread_equivalent(strategy)
    if needs_fallback and fallback_strategy is not None:
        if primary is not None:
            notes.append(
                f"{strategy.value} could not be sized within ${max_risk_usd:,.0f} of defined risk "
                f"(one contract risks ${primary.max_loss_per_contract or 0:,.2f}); "
                f"offering {fallback_strategy.value} as a defined-risk alternative"
            )
        alt = _try(fallback_strategy)
        if alt is not None:
            alt.selection_notes.append(
                f"selected as a sizeable alternative to {strategy.value}, which exceeded the "
                f"${max_risk_usd:,.0f} per-trade risk cap"
            )
            out.append(alt)

    # Sizeable structures first, ordered by cost drag (cheapest round trip).
    # Among UNSIZEABLE ones, order by max loss ascending — the closest to fitting
    # is the most useful thing to report, since "this needs $328 against a $100
    # cap" tells the reader how far off they are, while the same rejection
    # quoting an $1,800 structure does not.
    out.sort(
        key=lambda s: (
            s.contracts == 0,
            (s.max_loss_per_contract or 1e9)
            if s.contracts == 0
            else (s.cost_drag_pct if s.cost_drag_pct is not None else 9.9),
        )
    )
    return out[: cfg.max_proposals_per_candidate], notes


_ALLOWED = (
    StrategyType.LONG_CALL,
    StrategyType.LONG_PUT,
    StrategyType.BULL_CALL_SPREAD,
    StrategyType.BEAR_PUT_SPREAD,
)
