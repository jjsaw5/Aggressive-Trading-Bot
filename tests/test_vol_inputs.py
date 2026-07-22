"""Phase 3.1: volatility scoring consumes >=2 independent features + skew, and
labels/down-weights an HV-proxy rank."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.domain.enums import OptionType
from app.domain.options import Greeks, IVContext, OptionChain, OptionContract
from app.quant.iv import put_call_iv_skew
from app.shortduration.scoring.components import volatility_suitability

_NOW = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
_EXP = date(2026, 8, 21)


def _iv(**kw) -> IVContext:
    base = {"symbol": "AAA", "iv_rank": 0.35, "iv_percentile": 0.35,
            "iv_rank_source": "iv_history", "as_of": _NOW}
    base.update(kw)
    return IVContext(**base)


def test_uses_rank_and_percentile_together() -> None:
    c = volatility_suitability(_iv(iv_rank=0.35, iv_percentile=0.35))
    assert c.value == 1.0 and "2-feature" in c.explanation
    # percentile alone still scores (rank missing) -> 1-feature
    c1 = volatility_suitability(_iv(iv_rank=None, iv_percentile=0.35))
    assert c1.value is not None and "1-feature" in c1.explanation
    # neither present -> abstain
    assert volatility_suitability(_iv(iv_rank=None, iv_percentile=None)).value is None


def test_hv_proxy_is_labeled_and_downweighted() -> None:
    true_rank = volatility_suitability(_iv(iv_rank_source="iv_history"))
    proxy = volatility_suitability(_iv(iv_rank_source="hv_proxy"))
    assert proxy.value < true_rank.value  # discounted
    assert "HV-proxy" in proxy.explanation


def test_backwardation_downweights_debit_suitability() -> None:
    flat = volatility_suitability(_iv(term_structure_slope=0.0))
    backward = volatility_suitability(_iv(term_structure_slope=-0.05))
    assert backward.value < flat.value
    assert "backwardated" in backward.explanation


def test_put_call_skew_reads_downside_fear() -> None:
    def c(strike, otype, iv):
        return OptionContract(symbol="AAA", expiration=_EXP, strike=strike, option_type=otype,
                              implied_volatility=iv, greeks=Greeks(), as_of=_NOW, source="test")
    # spot 100: OTM puts (95) richer than OTM calls (105) -> positive skew.
    chain = OptionChain(symbol="AAA", underlying_price=100.0, as_of=_NOW, source="test", contracts=[
        c(95.0, OptionType.PUT, 0.45), c(105.0, OptionType.CALL, 0.30),
    ])
    skew = put_call_iv_skew(chain, 100.0)
    assert skew is not None and skew > 0
    assert round(skew, 2) == 0.15


def test_skew_none_when_a_wing_is_missing() -> None:
    def c(strike, otype, iv):
        return OptionContract(symbol="AAA", expiration=_EXP, strike=strike, option_type=otype,
                              implied_volatility=iv, greeks=Greeks(), as_of=_NOW, source="test")
    chain = OptionChain(symbol="AAA", underlying_price=100.0, as_of=_NOW, source="test",
                        contracts=[c(95.0, OptionType.PUT, 0.45)])  # no OTM call
    assert put_call_iv_skew(chain, 100.0) is None
