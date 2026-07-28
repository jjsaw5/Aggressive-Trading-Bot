# Pre-registration: Earnings Option Overpricing (Earnings-VRP)

**Status:** REGISTERED — committed before any measurement is run.
**Registered:** 2026-07-28
**Runs:** exactly one (see §6 stopping rules).

## §0 Question

Is the option market's implied earnings move systematically larger than the
realized move in our liquid universe — and if so, does the gap survive
defined-risk expression and full spread-crossing (k=1.0) at 1-lot retail size?

This is a NARROWER effect than the tenor-matched VRP already tested (see
docs/vrp_registry_entry.json: premium real, bear-reversing, execution cost
consumed the edge). The earnings variant concentrates the premium into a
single overnight catalyst, which changes both the numerator (richness) and
the denominator (only two spread crossings per trade).

## §1 Coverage precondition (BLOCKING)

Before any outcome metric is computed, measure data coverage:

- An earnings event is USABLE only if (a) the earnings date is confirmed
  (epsActual present in FMP `/stable/earnings`), (b) ATM straddle marks exist
  for BOTH the last close before the print and the first close after, from UW
  per-contract history, and (c) the underlying has FMP daily closes on both
  sides.
- If fewer than **60%** of candidate events are usable, or fewer than **150**
  usable events remain, the experiment reports BLOCKED and stops. No metric
  may be computed from a sample that failed the precondition.

## §2 Universe & sample

- Symbols: the 1-5DTE scan universe (app/engine/universe.py
  SHORT_DURATION_UNIVERSE) as of this commit — broadly liquid, actively
  optioned names. No additions or removals after registration.
- Events: all confirmed quarterly earnings 2023-01-01 through 2026-06-30.
- No hindsight filters of any kind: no dropping events by regime, by realized
  outcome, by "unusual" IV, or by anything not knowable strictly before the
  print. Liquidity gates use pre-print data only.

## §3 Measurement definitions (frozen)

- **Anchor expiry:** nearest listed expiry on or after the first trading
  session following the print.
- **Implied move:** (ATM straddle mid at last pre-print close) / (underlying
  close), ATM = strike nearest the pre-print close.
- **Realized move:** |first post-print close − last pre-print close| /
  (pre-print close). Close-to-close, not intraday extremes.
- **Trade convention (variants):** enter at last pre-print close, exit at
  first post-print close. Hold one overnight only.

### Variants (all four registered up front; Bonferroni m=4)

| id | description | purpose |
|----|-------------|---------|
| V1 | implied vs realized move gap (vol pts) | existence, informational |
| V2 | short ATM straddle at MID both ways, $/trade at 1-lot | gross premium capture |
| V3 | V2 with k=1.0 spread crossing both ways | net of friction |
| V4 | defined-risk iron fly (wings at strike nearest ±1.5× implied move) at k=1.0, $/trade at 1-lot | the only version this account could ever trade |

k=1.0 means the full quoted bid/ask spread is paid on entry AND exit, per leg
— identical to the VRP Stage-2 convention.

## §4 Statistics (frozen)

- Cluster bootstrap (10,000 resamples) clustered by **symbol × quarter**;
  95% CIs.
- Bonferroni correction across the 4 variants (α = 0.0125 per variant).
- Regime split: sample halves by calendar midpoint, reported separately.
  A result whose sign flips between halves is FRAGILE regardless of the
  pooled number — same treatment that caught the VRP bear reversal.

## §5 Success criteria (frozen)

The effect is **HARVESTABLE** only if ALL of:

1. V4 mean net P&L > **+$5.00 per trade** at 1-lot (material margin, not
   merely > $0), AND
2. the Bonferroni-adjusted 95% CI for V4 excludes zero, AND
3. the V4 sign is consistent across both sample halves, AND
4. the §1 coverage precondition passed.

Anything less is a NULL for harvestability, whatever V1–V3 show. A real but
unharvestable premium (V1/V2 positive, V4 failing) is recorded exactly as
such — like VRP.

## §6 Stopping rules (hard)

- The measurement runs ONCE against this registration. No re-runs with
  adjusted strikes, wings, tenors, holds, or universes.
- A failed result is registered as a negative in the feature registry and
  closes this avenue. Re-opening requires a NEW pre-registration that attacks
  a different lever (e.g. execution cost), not a re-roll of this one.
- If the coverage precondition blocks, the experiment may be re-attempted
  ONLY after the data gap itself is fixed, under an amended registration
  noting what changed.

## §7 Prior evidence & expectation

Registered prior: the premium likely exists gross (V1/V2 positive — this is
among the most-documented option effects), and friction likely consumes most
of it at 1-lot retail size (V4 near or below zero), consistent with the
tenor-matched VRP result (+$10.99 at mid → −$2.63 at k=1.0). This experiment
is run to MEASURE, not to confirm; the criteria above were set before any
data was pulled.

## §8 Product note (registered before results)

- HARVESTABLE → a new **Earnings** strategy section is warranted (event
  calendar of confirmed upcoming prints on the universe, per-name richness,
  sized defined-risk short-premium structure). It does NOT belong on the
  0DTE or 1-5DTE boards (directional, long-premium character) nor in Core
  (directional swing research).
- NULL → registry entry + truth-banner update. The existing defensive
  earnings features (earnings-before-expiry and IV-crush warnings) remain
  the app's only earnings surface. No UI is built on a null.
