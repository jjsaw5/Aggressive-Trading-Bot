# Freeze point — signal-only capture window

**Model:** `sd-scoring-2026.08-v4.0`
**Tag:** `freeze/sd-scoring-2026.08-v4.0`
**Commit:** `935160d4735716150414d0b1c610f5cbfc5530cc`
**Established:** 2026-08-04, merge commit of PR #55 (Amendment 2 — contract
selection prices probability, not only payoff)

> **The first line of this file is machine-read.** CI parses the first
> `freeze/...` string and the first 40-hex SHA out of this document. Both were
> previously hardcoded in `.github/workflows/ci.yml`, and the tag name went stale
> the moment the model moved — so the informational diff compared against a
> superseded baseline while printing reassurance. Keep the header format.

## Superseded freeze points

Kept so a row stamped with an older `scoring_model_version` can still be traced
to the code that produced it. **They are history, not baselines** — never diff
current work against them.

| Model | Tag | Commit | Established | Superseded by |
|---|---|---|---|---|
| `sd-scoring-2026.08-v3.1` | `freeze/sd-scoring-2026.08-v3.1` | `80eb42c36541a72a7eb30b9699639b5f10ef5414` | 2026-08-01, PR #49 (Ruling 1 / FINDING_01) | Amendment 2 |

## Why the SHA is recorded here and not only in the tag

The tag is the human-facing name; **the SHA is the authority.** A tag can be
missing from a clone (`--no-tags`, a shallow fetch, a mirror that filters refs),
moved, or — as happened when this freeze point was established — fail to push
because the pushing credential is scoped to `refs/heads/*` only. A governance
check that silently passes because the tag it compares against does not exist is
worse than no check.

Every automated freeze check therefore resolves the tag **and falls back to this
SHA**, and fails loudly if neither is reachable.

## The check

```sh
git diff 935160d4735716150414d0b1c610f5cbfc5530cc -- \
    app/shortduration/scoring/ \
    app/shortduration/strategies/ \
    app/shortduration/contracts.py \
    app/providers/unusual_whales/client.py \
    app/providers/mock/provider.py \
    app/engine/iv_context.py \
    app/engine/contract_selection.py
```

Expected **empty** for the duration of the capture window. A non-empty diff means
the frozen model changed and requires, in the same commit: a
`scoring_model_version` bump, a dated amendment under
`docs/CAPTURE_WINDOW_PREREGISTRATION.md` §8, and a regenerated golden file.

**That path list is the guarded set, and it lives in exactly one place**:
`GUARDED_RE` / `GUARDED_PATHS` at the top of the `freeze-guard` job in
`.github/workflows/ci.yml`. `tests/test_freeze_guard_config.py` asserts the two
forms describe the same set and that this document's copy matches. They drifted
once — the guard was widened to cover contract selection while the informational
diff still looked at two directories, so CI reported "unchanged since the freeze"
about paths it was not looking at.

## Scope — what the freeze actually covers

The path diff above is necessary and **not sufficient**. FINDING_01 changed the
shipped model's behaviour without touching a single file under the two paths the
list originally held, by populating a provider field the scorer had always read
and nobody had ever supplied. Amendment 2 then did the same thing again through
contract selection. Both files are now in the list; the lesson is that the list
is a lagging indicator of a truth the behavioural controls catch first.

Full coverage is the path diff **plus** the three behavioural controls:

| Control | Catches |
|---|---|
| this path diff | edits to scoring/strategy source |
| `tests/test_scoring_golden.py` | changes to what the scorer computes for fixed inputs |
| `tests/test_provider_scoring_contract.py` | changes to which provider fields reach the scorer |
| `tests/test_scoring_freeze.py` | capture-only imports; a silent version bump |

## The trio, and which class of change each one catches

Reviewer Rulings #2, R1. Ruling 1 originally predicted the golden file would
break on the FINDING_01 fix. It did not and could not — and understanding *why*
is what makes the set of controls legible, so it is written down here rather
than left as a footnote in a session log.

A scoring change can arrive from exactly two directions: the arithmetic can
change, or the inputs to the arithmetic can change. One test guards each
direction, and CI forces the declaration when either fires.

**1. `tests/test_scoring_golden.py` — pins the scorer's MATH on fixed inputs.**
It constructs `IVContext` fixtures by hand and asserts the composite and every
component against `tests/golden/scoring_v3.json`. It is blind to providers by
construction: hand-built inputs cannot move when a provider changes. That
blindness is the design, not a defect — it is what makes the file a clean
measurement of the arithmetic alone. If a weight, threshold or component
formula moves, this fails.

**2. `tests/test_provider_scoring_contract.py` — pins WHAT THE SCORER RECEIVES.**
It fixes `SCORED_IV_FIELDS` and greps `components.py` so the declared set cannot
drift out of sync with what the scorer actually reads, then exercises the live
provider's derivation of each field. This is the control that covers FINDING_01's
class of defect, and it is the one that demonstrated efficacy across the fix:
**8 of 9 failing before, 9 of 9 passing after.** A field the scorer reads and no
provider populates fails here.

**3. `.github/workflows/ci.yml` job `freeze-guard` — forces the DECLARATION.**
Neither test above can tell whether a change is legitimate; that is a human
judgement. The guard diffs the PR against its base for edits to any guarded path
and fails unless the same PR carries a `scoring_model_version` bump and a dated
§8 amendment.

The guarded set has grown twice, each time after a model change slipped past it:

- **the provider files**, after FINDING_01 changed the shipped model from
  `unusual_whales/client.py` with no diff under `scoring/`;
- **contract selection**, after Amendment 2 — `scoring/components.py:181` reads
  `reward_to_risk` off the SELECTED plan, so changing which structure the
  selector returns changes a scored component, again with no diff under
  `scoring/`.

It gates on PATH and deliberately does not judge semantics: it failed a
comment-only edit in the PR #52 demonstration, which is correct behaviour rather
than a false positive. It passed Amendment 2 (PR #55) only because the version
bump and the §8 amendment were both present — verbatim from that run:

```
Guarded paths touched:
  app/engine/contract_selection.py
  app/shortduration/contracts.py
Declared: scoring_model_version bumped AND pre-registration amended.
```

Between them: (1) catches the math moving, (2) catches the inputs moving, (3)
makes either one impossible to ship silently. A change that evades all three
would have to alter neither the arithmetic, nor the field set, nor any guarded
file — in which case it is not a scoring change.

**Ruling 1 step 2 is amended to match this.** The prediction that the golden file
would break was mistaken about which control covers a provider change; the
substitute proof (control 2's 8→9, plus control 3 catching the version bump
independently) is the stronger evidence and stands in its place.

## Restoring the tag

If the tag is absent from a clone or a remote:

```sh
git tag -a freeze/sd-scoring-2026.08-v4.0 935160d4735716150414d0b1c610f5cbfc5530cc \
  -m "Freeze point for the signal-only capture window (Amendment 2)."
git push origin refs/tags/freeze/sd-scoring-2026.08-v4.0
```
