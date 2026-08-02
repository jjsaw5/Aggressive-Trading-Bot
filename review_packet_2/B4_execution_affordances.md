# B4 — Execution affordances

**They exist.** The UI contains buttons whose label is "Execute", wired to
`POST .../execute`. This is the finding; everything below is the detail of what
they actually do.

## The affordances, verbatim

| Line | Element |
|---|---|
| 450 | `<button data-p="${p.id}" data-act="execute">Execute (guarded)</button>` |
| 1002 | `<button data-act="sd-execute" data-pid="${prop.id}">Execute (guarded)</button>` |
| 958 | `<button class="ghost" data-act="sd-arm" data-id="${c.id}">Arm</button>` |
| ~1000 | `<button class="ghost" data-act="sd-propose">Propose (live · gated)</button>` |
| 291 | `automation: ARMED` / `automation: off` badge |

Handlers:

```js
1837:  const d = await api("POST", `/proposals/${actBtn.dataset.p}/execute`);
1955:  await api("POST", `/short-duration/candidates/${actBtn.dataset.id}/arm`);
1986:  const dsn = await api("POST", `/short-duration/proposals/${actBtn.dataset.pid}/execute`);
```

## What the endpoints do

`app/api/routes/proposals.py:94`

```python
@router.post("/{proposal_id}/execute")
async def execute(proposal_id: str) -> dict:
    """Attempt execution — passes through the guard. With automation disabled by
    default this ALWAYS returns authorized=false; the endpoint makes the safety
    gate observable and testable. No broker order is placed."""
    ...
    decision = ExecutionGuard().authorize(p)
    return {..., "authorized": decision.authorized, "reason": decision.reason,
            "note": "No broker order is placed by this platform build."}
```

`app/api/routes/short_duration.py:414`

```python
"""Route an APPROVED proposal through the ExecutionGuard. Denied by default
(research mode + automation off); never places an order here."""
return {"authorized": decision.authorized, "reason": decision.reason}
```

**Neither endpoint has a broker call in any branch.** There is no code path from
these handlers to an order. The button's entire effect is to obtain a denial and
display its reason.

## The prose that frames it (line 436–438)

> Candidate → Propose → Approve → **Execute**. **Execute is double-gated** — it
> stays denied by default (research mode) and only ever routes an order when live
> trading is explicitly armed, which it is not.

Plus, at 434: *"held here for **your** review. Nothing is sent to a broker
automatically."*

## Assessment

**The gate is real, is tested, and is not bypassable from this UI.** That is the
substantive point and it holds.

But two things are worth stating plainly rather than filing as satisfied:

1. **"only ever routes an order when live trading is explicitly armed" is a
   promise about a build that does not exist.** There is no arming path in this
   codebase — no broker order call to reach. The sentence describes a hypothetical
   future capability in the present tense, which reads as "a switch exists and is
   off" rather than "the wire was never run." `ExecutionGuard` and
   `automation_armed` are genuinely load-bearing today (they gate the response),
   so the sentence is not false; it is stronger than the code.

2. **A button labelled "Execute" trains a habit.** The label is the verb for
   placing an order. `(guarded)` qualifies it, and the double-gate holds today.
   The risk is not that this build trades — it demonstrably cannot — but that
   the muscle memory is being built now, on a surface that will one day have a
   broker behind it, next to `PICK #1` badges.

Neither of these is a defect in the gate. Both are display-honesty
observations, which is what this review is for.

## Read-only guarantees elsewhere

`GET /short-duration/configuration` returns
`"note": "Research/paper only. Live trading is disabled for this module."`
(captured live in `B5_sample_configuration.json`).

Robinhood access via MCP is read-only by standing instruction and is not
reachable from the app at all.
