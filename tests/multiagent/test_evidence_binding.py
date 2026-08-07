"""The anti-hallucination control, tested with an agent that deliberately lies.

This is the most important file in the suite. The whole architecture rests on
one claim — *an agent can select evidence but cannot introduce it* — and a claim
about an LLM is only worth what its enforcement is worth. So the tests here run
a runner engineered to fabricate: invented evidence ids, invented headlines,
tickers nothing was retrieved for, strategies outside the allow-list.

Every one of those must be dropped and recorded. If a test here fails, the
product's central honesty guarantee is broken, whatever else passes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.multiagent.agents.binding import (
    BindingResult,
    bind_claims,
    restrict_to_known_symbols,
)
from app.multiagent.agents.market_intelligence import run_market_intelligence
from app.multiagent.agents.opportunity_generator import run_opportunity_generator
from app.multiagent.evidence.collector import MarketEvidence
from app.multiagent.llm.definitions import load_agent
from app.multiagent.llm.runner import AgentResult, AgentRunner
from app.multiagent.models.enums import DataQualityFlag, PipelineStage
from tests.multiagent.conftest import make_quote


class LyingRunner(AgentRunner):
    """Returns whatever payload it is constructed with, regardless of input."""

    runner_id = "test-liar"

    def __init__(self, payload):
        self.payload = payload

    async def run(self, invocation):
        return AgentResult(data=self.payload, runner_id=self.runner_id, raw_text="fabricated")


def test_a_claim_citing_an_unknown_evidence_id_is_dropped(ledger, now):
    result = BindingResult()
    kept = bind_claims(
        [
            {"headline": "real", "evidence_refs": list(ledger.items)[:1]},
            {"headline": "fabricated", "evidence_refs": ["news-deadbeef00"]},
        ],
        ledger,
        label="catalyst",
        now=now,
        result=result,
    )
    assert [c["headline"] for c in kept] == ["real"]
    assert len(result.dropped) == 1
    assert "fabricated" in result.dropped[0]
    assert any(q.flag is DataQualityFlag.UNREFERENCED_AGENT_CLAIM for q in result.quality)


def test_a_stray_ref_is_stripped_but_a_partly_valid_claim_survives(ledger, now):
    real = list(ledger.items)[0]
    result = BindingResult()
    kept = bind_claims(
        [{"headline": "half real", "evidence_refs": [real, "news-notathing"]}],
        ledger,
        label="catalyst",
        now=now,
        result=result,
    )
    assert len(kept) == 1
    # The unknown id is gone from the surviving claim, not merely flagged.
    assert kept[0]["evidence_refs"] == [real]
    assert "news-notathing" in result.stripped_refs
    assert not result.dropped


def test_a_claim_with_no_refs_is_dropped_when_refs_are_required(ledger, now):
    result = BindingResult()
    kept = bind_claims(
        [{"headline": "unsourced assertion"}], ledger, label="catalyst", now=now, result=result
    )
    assert kept == []
    assert len(result.dropped) == 1


def test_a_claim_with_no_refs_survives_when_refs_are_optional(ledger, now):
    result = BindingResult()
    kept = bind_claims(
        [{"headline": "interpretive note"}],
        ledger,
        label="note",
        now=now,
        require_refs=False,
        result=result,
    )
    assert len(kept) == 1
    assert not result.dropped


def test_a_ticker_with_no_retrieved_data_is_dropped(now):
    result = BindingResult()
    kept = restrict_to_known_symbols(
        [{"ticker": "NVDA"}, {"ticker": "ZZZZ"}],
        {"NVDA"},
        label="candidate",
        now=now,
        result=result,
    )
    assert [c["ticker"] for c in kept] == ["NVDA"]
    assert any(q.flag is DataQualityFlag.OUT_OF_UNIVERSE_TICKER for q in result.quality)


@pytest.mark.asyncio
async def test_agent1_cannot_smuggle_a_fabricated_catalyst_into_the_brief(ledger, now, methodology):
    real_ref = list(ledger.items)[0]
    runner = LyingRunner(
        {
            "market_regime": "trending_up",
            "company_catalysts": [
                {
                    "ticker": "NVDA",
                    "catalyst_type": "earnings",
                    "headline": "REAL — cites a retrieved item",
                    "source": "test-wire",
                    "evidence_refs": [real_ref],
                },
                {
                    "ticker": "NVDA",
                    "catalyst_type": "merger_acquisition",
                    "headline": "FABRICATED — NVDA to acquire a competitor for $90bn",
                    "source": "invented-wire",
                    "source_url": "https://not-a-real-source.test/scoop",
                    "evidence_refs": ["news-0000000000"],
                },
            ],
            "news_items": [],
            "macro_events": [],
            "risk_events": [],
            "summary": "test",
        }
    )
    evidence = MarketEvidence(ledger=ledger)

    brief, record, binding = await run_market_intelligence(
        load_agent("market-intelligence", methodology.definitions_path()),
        runner,
        evidence,
        methodology,
        run_id="test-run",
        stage=PipelineStage.PREMARKET,
        now=now,
    )

    headlines = [c.headline for c in brief.company_catalysts]
    assert headlines == ["REAL — cites a retrieved item"]
    assert not any("FABRICATED" in h for h in headlines)
    # The drop is recorded, not silent — that is what makes it auditable.
    assert len(binding.dropped) == 1
    assert record.dropped_claims == binding.dropped
    assert brief.dropped_claims == binding.dropped


@pytest.mark.asyncio
async def test_agent1_cannot_overwrite_a_measured_index_price(ledger, now, methodology):
    """Measured fields are written by code after the agent returns."""
    from app.multiagent.evidence.collector import build_index_context

    evidence = MarketEvidence(ledger=ledger)
    evidence.indices["SPY"] = build_index_context(
        "SPY",
        make_quote("SPY", 500.0, as_of=now),
        None,
        lookback_days=20,
        flat_threshold_pct=1.0,
    )

    runner = LyingRunner(
        {
            "market_regime": "trending_up",
            "company_catalysts": [],
            "news_items": [],
            # The agent asserts a different SPY price. It must not survive.
            "spy": {"symbol": "SPY", "price": 1.0, "bias": "bearish"},
            "spy_bias": "bearish",
            "summary": "test",
        }
    )
    brief, _record, _binding = await run_market_intelligence(
        load_agent("market-intelligence", methodology.definitions_path()),
        runner,
        evidence,
        methodology,
        run_id="test-run",
        stage=PipelineStage.PREMARKET,
        now=now,
    )
    assert brief.spy is not None
    assert brief.spy.price == 500.0
    assert brief.spy_bias is brief.spy.bias


@pytest.mark.asyncio
async def test_agent2_drops_unsourced_candidates_out_of_universe_and_bad_strategies(
    ledger, now, methodology
):
    from app.multiagent.models.brief import MarketBrief

    real_ref = list(ledger.items)[0]
    brief = MarketBrief(run_id="test-run", generated_at=now)

    runner = LyingRunner(
        {
            "candidates": [
                {  # good
                    "ticker": "NVDA",
                    "direction": "bullish",
                    "strategy_type": "bull_call_spread",
                    "thesis": "t",
                    "primary_catalyst": "c",
                    "primary_catalyst_refs": [real_ref],
                    "invalidation_thesis": "i",
                },
                {  # fabricated evidence
                    "ticker": "NVDA",
                    "direction": "bearish",
                    "strategy_type": "long_put",
                    "thesis": "t",
                    "primary_catalyst": "invented",
                    "primary_catalyst_refs": ["news-ffffffffff"],
                    "invalidation_thesis": "i",
                },
                {  # ticker nothing was retrieved for
                    "ticker": "ZZZZ",
                    "direction": "bullish",
                    "strategy_type": "long_call",
                    "thesis": "t",
                    "primary_catalyst": "c",
                    "primary_catalyst_refs": [real_ref],
                    "invalidation_thesis": "i",
                },
                {  # forbidden strategy
                    "ticker": "NVDA",
                    "direction": "bullish",
                    "strategy_type": "iron_condor",
                    "thesis": "t",
                    "primary_catalyst": "c",
                    "primary_catalyst_refs": [real_ref],
                    "invalidation_thesis": "i",
                },
            ]
        }
    )

    candidates, record, binding = await run_opportunity_generator(
        load_agent("opportunity-generator", methodology.definitions_path()),
        runner,
        brief,
        ledger,
        methodology,
        run_id="test-run",
        stage=PipelineStage.MARKET_OPEN,
        trends={},
        quotes={"NVDA": make_quote("NVDA", 100.0, as_of=now)},
        now=now,
    )

    assert [(c.ticker, c.strategy_type.value) for c in candidates] == [
        ("NVDA", "bull_call_spread")
    ]
    assert len(binding.dropped) == 3
    assert record.dropped_claims == binding.dropped


@pytest.mark.asyncio
async def test_agent2_reference_price_comes_from_the_quote_not_the_agent(
    ledger, now, methodology
):
    from app.multiagent.models.brief import MarketBrief

    real_ref = list(ledger.items)[0]
    runner = LyingRunner(
        {
            "candidates": [
                {
                    "ticker": "NVDA",
                    "direction": "bullish",
                    "strategy_type": "long_call",
                    "thesis": "t",
                    "primary_catalyst": "c",
                    "primary_catalyst_refs": [real_ref],
                    "invalidation_thesis": "i",
                    # The agent asserts a price. It is overwritten.
                    "underlying_reference_price": 12345.0,
                }
            ]
        }
    )
    candidates, _record, _binding = await run_opportunity_generator(
        load_agent("opportunity-generator", methodology.definitions_path()),
        runner,
        MarketBrief(run_id="test-run", generated_at=now),
        ledger,
        methodology,
        run_id="test-run",
        stage=PipelineStage.MARKET_OPEN,
        trends={},
        quotes={"NVDA": make_quote("NVDA", 137.5, as_of=now)},
        now=now,
    )
    assert candidates[0].underlying_reference_price == 137.5


@pytest.mark.asyncio
async def test_agent2_respects_the_candidate_cap(ledger, now, methodology):
    from app.multiagent.models.brief import MarketBrief

    real_ref = list(ledger.items)[0]
    over_cap = methodology.run.max_candidates + 5
    runner = LyingRunner(
        {
            "candidates": [
                {
                    "ticker": f"SYM{i}",
                    "direction": "bullish",
                    "strategy_type": "long_call",
                    "thesis": "t",
                    "primary_catalyst": "c",
                    "primary_catalyst_refs": [real_ref],
                    "invalidation_thesis": "i",
                }
                for i in range(over_cap)
            ]
        }
    )
    quotes = {f"SYM{i}": make_quote(f"SYM{i}", 100.0, as_of=now) for i in range(over_cap)}
    candidates, _record, binding = await run_opportunity_generator(
        load_agent("opportunity-generator", methodology.definitions_path()),
        runner,
        MarketBrief(run_id="test-run", generated_at=now),
        ledger,
        methodology,
        run_id="test-run",
        stage=PipelineStage.MARKET_OPEN,
        trends={},
        quotes=quotes,
        now=now,
    )
    assert len(candidates) == methodology.run.max_candidates
    assert any("beyond the" in d for d in binding.dropped)


@pytest.mark.asyncio
async def test_a_malformed_agent_response_degrades_to_a_measured_brief(ledger, now, methodology):
    """A broken agent must not take the run with it."""
    evidence = MarketEvidence(ledger=ledger)
    runner = LyingRunner({"market_regime": "not_a_real_regime"})

    brief, record, _binding = await run_market_intelligence(
        load_agent("market-intelligence", methodology.definitions_path()),
        runner,
        evidence,
        methodology,
        run_id="test-run",
        stage=PipelineStage.PREMARKET,
        now=now,
    )
    assert brief.company_catalysts == []
    assert any("schema" in g.lower() for g in brief.data_gaps)
    assert record.status.value == "failed"


@pytest.mark.asyncio
async def test_an_agent_returning_nothing_yields_a_brief_that_says_so(ledger, now, methodology):
    evidence = MarketEvidence(ledger=ledger)
    brief, record, _ = await run_market_intelligence(
        load_agent("market-intelligence", methodology.definitions_path()),
        LyingRunner(None),
        evidence,
        methodology,
        run_id="test-run",
        stage=PipelineStage.PREMARKET,
        now=now,
    )
    assert "no output" in " ".join(brief.data_gaps)
    assert record.status.value == "failed"


def test_secrets_never_reach_a_stored_agent_record():
    """Prompt and response excerpts are redacted before storage."""
    from app.multiagent.models.enums import AgentName
    from app.multiagent.models.enums import PipelineStage as PS
    from app.multiagent.models.runs import AgentRunRecord

    record = AgentRunRecord(
        agent=AgentName.MARKET_INTELLIGENCE,
        run_id="r",
        stage=PS.FULL,
        started_at=datetime.now(UTC),
    )
    record.record_prompt("call it with api_key=sk-abcdef0123456789abcdef and go")
    record.record_response("token: sk-9876543210fedcba9876 returned")

    assert "sk-abcdef0123456789abcdef" not in record.prompt_excerpt
    assert "sk-9876543210fedcba9876" not in record.raw_response_excerpt
    assert "[REDACTED]" in record.prompt_excerpt
    assert "[REDACTED]" in record.raw_response_excerpt
