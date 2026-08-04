# Freeze point — signal-only capture window

**Model:** `sd-scoring-2026.08-v3.1`
**Tag:** `freeze/sd-scoring-2026.08-v3.1`
**Commit:** `80eb42c36541a72a7eb30b9699639b5f10ef5414`
**Established:** 2026-08-01, merge commit of PR #49 (reviewer Ruling 1 /
FINDING_01 closure)

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
git diff 80eb42c36541a72a7eb30b9699639b5f10ef5414 -- \
    app/shortduration/scoring/ app/shortduration/strategies/
```

Expected **empty** for the duration of the capture window. A non-empty diff means
the frozen model changed and requires, in the same commit: a
`scoring_model_version` bump, a dated amendment under
`docs/CAPTURE_WINDOW_PREREGISTRATION.md` §8, and a regenerated golden file.

## Scope — what the freeze actually covers

The path diff above is necessary and **not sufficient**. FINDING_01 changed the
shipped model's behaviour without touching a single file under those two paths,
by populating a provider field the scorer had always read and nobody had ever
supplied.

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
judgement. The guard diffs the PR against the base for edits to guarded paths —
including contract selection, added 2026-08-03 after the same gap was found a
second time: `scoring/components.py:181` reads `reward_to_risk` off the SELECTED
plan, so changing which structure the selector returns changes a scored component
and therefore the shipped model, with no diff under `scoring/` at all —
including the provider files, because FINDING_01 proved a provider edit is a
model change — and fails unless the same PR carries a `scoring_model_version`
bump and a dated §8 amendment. It gates on PATH and deliberately does not try to
judge semantics: it failed a comment-only edit in the PR #52 demonstration,
which is correct behaviour, not a false positive.

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
git tag -a freeze/sd-scoring-2026.08-v3.1 80eb42c36541a72a7eb30b9699639b5f10ef5414 \
  -m "Freeze point for the signal-only capture window."
git push origin refs/tags/freeze/sd-scoring-2026.08-v3.1
```
