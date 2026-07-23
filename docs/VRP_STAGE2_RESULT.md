# VRP Stage 2 — fail to reject H0: the premium exists, and execution cost consumes it

Run exactly as pre-registered (`vrp_stage2_preregistration.md`, committed first;
machine-readable in `vrp_stage2_report.json`; seed 12345). Frozen 6-variant grid,
h=45, real NBBO at k=1.0 both legs + commissions, fixed exits, liquidity guard,
cluster bootstrap with Bonferroni α=0.05/6.

## The grid (expectancy per trade, net of spread + commissions)

| variant | n | win% | exp @k=1.0 | exp @k=0.5 | PF | 2022 bear exp | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| **uncond : put_credit** | 129 | 73% | **−$2.63** | **+$10.99** | 0.96 | **−$171.89** (n=14) | **slippage-fragile** |
| uncond : call_credit | 114 | 47% | −$130.58 | −$116.61 | 0.22 | −$46.27 | CI < 0 (harmful) |
| uncond : iron_condor | 59 | 29% | −$163.69 | −$143.45 | 0.19 | −$190.60 | CI < 0 (harmful) |
| iv_rich : put_credit | 39 | 64% | −$40.22 | −$22.16 | 0.56 | −$341.60 | fail |
| iv_rich : call_credit | 28 | 39% | −$162.46 | −$148.44 | 0.19 | −$18.85 | CI < 0 |
| iv_rich : iron_condor | 13 | 23% | −$207.35 | −$180.33 | 0.10 | −$209.20 | degenerate + fail |

**No variant passes any of the five pre-registered criteria. Fail to reject H0 —
as pre-registered, and as expected.**

## The three findings that matter

1. **The §7 prior replicated at 13× the sample.** The canonical VRP trade
   (unconditional put credit spread, n=129) is **positive at mid (+$10.99) and
   negative paying the spread (−$2.63)** — the definition of slippage-fragile. The
   implied round-trip execution cost is ≈ **$13.6/trade**, larger than the entire
   harvestable premium on these structures. Stage 1 said the premium is ~2.3 vol
   pts; Stage 2 says the toll booth charges more than that. *The premium exists;
   retail multi-leg execution consumes it* — precisely the §7-predicted outcome,
   now at n=129 instead of n≈10.
2. **The bear cell did exactly what Stage 1 said it would.** PCS in 2022:
   **−$171.89/trade, net −$2,406 over 14 entries** (worst single trade −$1,348).
   The −2.28-pt premium reversal translated directly into dollar losses. The 73%
   pooled win rate is the insurance body; 2022 is the claims year.
3. **Losses cluster — the account-ending pattern.** The worst loss-months are
   shared across variants (2022-04, 2024-10, 2025-05, 2025-10): losses arrive
   together across names, not independently. Per-trade averages hide this; the
   tail block does not. Sizing implication for a $2k book: with observed single
   trades of −$500 to −$1,350 per 1-lot on these underlyings, **the observed worst
   cluster is not survivable at any meaningful allocation** — which moots the
   sizing question.

Also noted: both call-side variants are **significantly harmful** (Bonferroni CI
entirely below zero) — shorting call wings against 2023-26's up-tape was beta
against the book, consistent with every directional finding this engagement has
produced. Conditioning on trailing IV richness did not rescue anything.

## Verdict against the five §9/§4 criteria

1. Pooled net expectancy > $5 with corrected CI excluding zero — **no variant.**
2. Tail paid with pooled expectancy still positive — **no** (bear losses real and
   uncompensated).
3. Not slippage-fragile — **the best variant is exactly slippage-fragile.**
4. Degeneracy guard — one variant degenerate; moot.
5. Registry entry — written (`vrp_registry_entry.json`), null recorded.

## What this means (write-up per the pre-registered framing)

- **Correct conclusion is NOT "no edge exists."** Stage 1 stands: a real, small,
  regime-reversing premium exists at 45d. Stage 2's null means **our execution
  layer cannot keep it**: ~$13.6/trade of spread+commission against ~$11/trade of
  mid-priced edge. The result points at wider structures, fewer legs, more liquid
  underlyings (index/SPX-class), or materially better fills — not at the
  hypothesis being wrong.
- **Nothing here validates any product path.** Not the 0DTE/1-5DTE scanner (§8 —
  wrong horizon entirely) and not a swing credit book as retail-executed here.
  The truth-teller stance is unchanged and re-confirmed by a fourth independent
  line of evidence.
- **The binding constraint was bear survival and cost, not signal quality** — as
  the addendum predicted. Any future revisit must attack cost (structure/venue)
  first; no amount of entry-signal cleverness closes a gap the toll booth owns.

*Research and decision-support only — not investment advice.*
