# flow_sweep Power Pass — all five encodings REJECTED (the thread is dead at EOD)

Run exactly as pre-registered (`flow_sweep_preregistration.md`, committed first).
Densified entries on the same cached corpus: **n = 1,257 vectors, 1,006 OOS pairs**
— 4× the original power. Cluster-bootstrap CIs over (symbol, entry-month) at
Bonferroni α = 0.01. Seed 12345. Machine-readable: `flow_sweep_pass_report.json`.

## The table

| encoding | n_oos | fwd ρ | 99% cluster CI | rev ρ | regime flip | verdict |
|---|---:|---:|---|---:|:--:|---|
| **flow_sweep** (replication) | 1006 | **−0.009** | [−0.110, +0.086] | +0.097 | yes | **REJECTED** |
| flow_sweep_x_at_ask | 1006 | −0.012 | [−0.112, +0.084] | +0.095 | yes | REJECTED |
| flow_sweep_oi | 1006 | +0.050 | [−0.050, +0.149] | +0.066 | yes | REJECTED |
| flow_sweep_burst | 968 | −0.080 | [−0.166, +0.014] | −0.075 | yes | REJECTED |
| flow_sweep_persist | 1006 | +0.055 | [−0.040, +0.144] | +0.079 | no | REJECTED |

## The headline: the original signal did not replicate

At n=236 OOS, `flow_sweep` read **+0.136** forward — the one sign-consistent,
no-flip thread in the whole flow study. At **4× the power, it collapsed to
−0.009**, and its per-regime signs now flip. That is the textbook signature of a
noise artifact meeting a bigger sample — regression to nothing. The mechanism
story (urgency vs chasing) was plausible; the data says no, at least at EOD.

None of the four alternative encodings rescued it. The closest
(`flow_sweep_persist`: +0.055/+0.079, no flip) still has a CI spanning zero at
the corrected level — and per the pre-registered rule, "closest" is not a pass.

## Consequences (all pre-registered, now binding)

1. **All five encodings are REJECTED in the registry, permanently.** The hard
   stopping rule applies: no re-runs with fresh thresholds, no new EOD encodings.
2. **The intraday-data spend is not justified.** The free higher-power EOD pass
   was the gate for that decision; it came back null. Recorded honestly as "not
   worth paying for yet" — an EOD aggregate is a degraded proxy for an intraday
   event, so this does not *disprove* an intraday sweep signal; it establishes
   that nothing EOD-visible carries information and nothing here funds the bet
   that intraday would.
3. **The flow premise is now fully falsified at the available granularity.**
   Flow-as-confirmer (experiment): null. Flow features (n=305): null, with
   at-ask negative. The last thread (`flow_sweep`), at 4× power: gone. The
   registry retains every one of these verdicts.

## Where this leaves the engagement's evidence ledger

Every hypothesis is now resolved on real marks, pre-registered or OOS:

| hypothesis | verdict |
|---|---|
| Directional selection (engine picks) | −$49.50/trade at k=1.0; regime-conditional beta |
| Flow as confirmer | fail to reject H0 |
| Pricing features (7) | none validated |
| Flow features (12) | none validated; `flow_at_ask` negative (chasing) |
| **flow_sweep, 5 encodings at 4× power** | **all rejected; original did not replicate** |
| VRP existence | real: +2.3 pts at 45d, REVERSES in bear (−2.28) |
| VRP harvest | premium ≈ execution cost; bear uncompensated |

The truth-teller stance is no longer a posture — it is the unanimous verdict of
seven pre-registered or out-of-sample tests. The remaining build item is the
permanently-red Layer-2 gate, which encodes that verdict structurally.

*Research and decision-support only — not investment advice.*
