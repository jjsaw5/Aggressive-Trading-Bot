"""Phase 1.2: paper-engine fills cross the real bid/ask spread, not just a floor."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.enums import Direction, OptionAction, OptionType, StrategyType
from app.domain.options import Greeks, OptionChain, OptionContract
from app.domain.trades import ContractLeg, RiskPlan, TradePlan
from app.services.paper_engine import open_paper_trade
from app.tiers.tier4_positions import structure_spread

_EXP = date(2026, 7, 17)
_NOW = datetime(2026, 6, 20, 15, 0, tzinfo=UTC)


def _plan() -> TradePlan:
    legs = [
        ContractLeg(symbol="AAA", action=OptionAction.BUY_TO_OPEN, option_type=OptionType.CALL,
                    strike=100.0, expiration=_EXP, quantity=1, entry_price=3.0),
        ContractLeg(symbol="AAA", action=OptionAction.SELL_TO_OPEN, option_type=OptionType.CALL,
                    strike=105.0, expiration=_EXP, quantity=1, entry_price=1.0),
    ]
    return TradePlan(symbol="AAA", direction=Direction.BULLISH, strategy=StrategyType.BULL_CALL_SPREAD,
                     legs=legs, net_debit=200.0, contracts=1,
                     risk=RiskPlan(max_loss_usd=200.0, max_profit_usd=300.0, account_risk_pct=0.05,
                                   profit_target_pct=0.5, stop_loss_pct=0.5))


def _chain() -> OptionChain:
    def c(strike, bid, ask):
        return OptionContract(symbol="AAA", expiration=_EXP, strike=strike, option_type=OptionType.CALL,
                              bid=bid, ask=ask, mark=(bid + ask) / 2, greeks=Greeks(), as_of=_NOW, source="test")
    return OptionChain(symbol="AAA", underlying_price=103.0,
                       contracts=[c(100.0, 3.9, 4.1), c(105.0, 1.45, 1.55)], as_of=_NOW, source="test")


def test_structure_spread_sums_leg_spreads() -> None:
    # 100C spread 0.20 + 105C spread 0.10 = 0.30.
    assert structure_spread(_plan(), _chain()) == 0.30


def test_missing_legs_contribute_zero_spread() -> None:
    empty = OptionChain(symbol="AAA", underlying_price=103.0, contracts=[], as_of=_NOW, source="test")
    assert structure_spread(_plan(), empty) == 0.0


def test_entry_fill_worsens_with_real_spread() -> None:
    plan = _plan()
    spread = structure_spread(plan, _chain())  # 0.30
    mid = 2.0
    floor_only = open_paper_trade(plan, "s", entry_mid=mid, entry_spread=0.0)
    with_spread = open_paper_trade(plan, "s", entry_mid=mid, entry_spread=spread)
    # A debit fill is worse (higher) when the real spread is crossed vs. the floor.
    assert floor_only.entry_fill == 2.01  # mid + $0.01 floor
    # SlippageModel crosses spread*0.5/2 = 0.30*0.25 = 0.075, well above the floor.
    assert with_spread.entry_fill == 2.075
    assert with_spread.entry_fill > floor_only.entry_fill
