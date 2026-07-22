# Real-Mark Corpus — 2021→2026, split-free wide universe

Result of the Conviction-Scanner spec §7 corpus run: the binding-constraint
dataset for validated conviction. Every option leg is repriced from **recorded UW
NBBO** (not model marks), net of commission, at two fill assumptions:

- **k = 0.5** — pay the mid (optimistic).
- **k = 1.0** — pay the full bid/ask spread (honest / conservative). A strategy
  positive only at k = 0.5 is *slippage-fragile*; grouped stats are reported at
  k = 1.0.

**Universe (split-free, so OCC strike ladders stay continuous):** SPY, QQQ, IWM,
AAPL, MSFT. **Eras:** 2021-22 (rate-shock selloff), 2023-24 (recovery), 2025-26
(current). **Modes:** `fixed` = systematic call-debit / put-credit / put-debit
verticals; `engine` = the scanner's own selection rule chooses the trade.

Run: `UW_HISTORIC_ENABLED=true UNUSUAL_WHALES_API_KEY=… python -m scripts.real_mark_backtest --mode both --preset all`

## Headline

The scanner's own picks (`engine`) have **negative net-of-cost expectancy across a
full market cycle** — and are negative even at the mid, so this is not merely a
spread-tax problem, the selection itself does not clear costs pooled.

| Slice | n (k=1.0) | exp @0.5 | exp @1.0 | win @1.0 | PF @1.0 |
|---|---:|---:|---:|---:|---:|
| **Engine — pooled** | 218 | **−$30.70** | **−$49.50** | 0.468 | 0.726 |
| Engine — 2021-22 (selloff) | 80 | −$111.03 | −$131.56 | 0.350 | 0.422 |
| Engine — 2023-24 (recovery) | 81 | +$4.37 | −$9.11 | 0.518 | 0.941 |
| Engine — 2025-26 (up-tape) | 57 | +$32.19 | **+$8.28** | 0.561 | 1.054 |
| Fixed — pooled | 669 | −$15.50 | −$34.59 | 0.504 | 0.791 |

## The signal is regime-conditional directional beta, not alpha

The engine only makes money when the tape goes up:

- **Selloff (2021-22):** catastrophic — −$131.56/trade, 35% win, PF 0.42.
- **Recovery (2023-24):** ~break-even — +$4.37 at mid, −$9.11 at full spread.
- **Current up-tape (2025-26):** positive — +$8.28 even at full spread, PF 1.05.

Pooled is negative because the selloff era dominates. This is the **same finding**
as the pre-registered flow experiment: the "edge" is a long/up-tape phenomenon.
By structure (engine, k=1.0) the only positive bucket is `call_debit_spread`
(+$3,778, 61.8% win) — bullish, long-premium — while both put spreads and call
credits lose. By vol regime it loses most in **high vol** (−$9,265, 41% win).

## What this means for conviction

1. **Layer-1's honest degrade was correct.** The picks do not beat the spread over
   a full cycle, so the hand-weighted number must keep displaying **UNCALIBRATED
   tradability**, never asserted conviction.
2. **Calibration must be per-regime** (spec §6, `CALIBRATION_PER_REGIME=true`). A
   single pooled number hides that expectancy flips sign with the tape. Any
   Layer-2 gate has to condition on regime or it will "green" in a bull sample and
   blow up in the next selloff.
3. **This is the raw material for the feature-validation harness (#76).** The
   corpus is now the out-of-sample set against which each candidate feature (delta,
   DTE, structure, vol regime, spread-tightness, flow-confirm) is tested for
   net-of-cost predictive power — with walk-forward purge + embargo and bootstrap
   CIs. Per-era engine n is small (57–81), so CIs will be wide; treat single-regime
   positives as unproven.

## Caveats

- Per-era `engine` samples are small (n = 57–81); pooled engine n = 218.
- Pre-2023 flow-side historic fields are unpopulated in the UW tier, so 2021-22 is
  NBBO/IV-only (no historic flow-alerts) — flow features can only be validated on
  2023+ data.
- Universe is deliberately split-free (5 clean names). Widening to split names
  (TSLA/NVDA/AMZN/GOOGL) needs split-adjusted OCC handling before it can be added.
