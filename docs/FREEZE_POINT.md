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

## Restoring the tag

If the tag is absent from a clone or a remote:

```sh
git tag -a freeze/sd-scoring-2026.08-v3.1 80eb42c36541a72a7eb30b9699639b5f10ef5414 \
  -m "Freeze point for the signal-only capture window."
git push origin refs/tags/freeze/sd-scoring-2026.08-v3.1
```
