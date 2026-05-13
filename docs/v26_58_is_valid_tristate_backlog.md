# V-26.58 — `AlphaResult.is_valid` tri-state semantics

**Severity**: 🟡 medium (review found ambiguity; no concrete bug observed today)
**Status**: backlog — semantic audit deferred
**Owner**: TBD

## What this is

`AlphaResult.is_valid` (and the parallel field on the LangGraph state) is
typed `Optional[bool]` with three intended values:

| value | meaning |
|---|---|
| `True`  | passed VALIDATE node successfully |
| `False` | VALIDATE marked it invalid |
| `None`  | not yet validated (e.g. SELF_CORRECT just reset it for re-validation) |

The review (`docs/quality_review_mining_task_2026-05-13.md:V-26.58`)
flagged that downstream callers use `if alpha.is_valid:` truthiness,
which collapses `None` and `False` into the same path. Today this is
**correct behaviour** — both "didn't pass" and "not yet checked" should
gate the same way against simulate / persist. But it's fragile: any
future caller that legitimately needs to distinguish "needs re-VALIDATE"
from "rejected by VALIDATE" will silently get the wrong answer.

## Why this commit doesn't fix it

The mitigation costs more than the current risk:

- All callers (`node_simulate`, `node_evaluate`, `node_save_results`,
  persistence paths) read `is_valid` via plain truthiness. Switching to
  explicit `is True` / `is False` / `is None` is mechanically safe but
  touches ~20 call sites, several of which are inside conditional
  chains where the change is non-trivial to verify.
- No observed bug today. The tri-state collapses safely because
  `None → re-validate → True/False` happens within a single graph
  invocation; nothing currently asks the question between SELF_CORRECT
  and the next VALIDATE.

## What to do when this becomes urgent

Trigger: a new node or workflow step that legitimately needs to consume
`is_valid` between SELF_CORRECT and the next VALIDATE.

Then:

1. Convert each `if alpha.is_valid:` to `if alpha.is_valid is True:`
   (or the explicit form that matches the intent).
2. Add a state invariant test: at no point should an alpha enter
   simulate / persist with `is_valid is None`.
3. Consider replacing the tri-state with a separate enum
   (`UNCHECKED | VALID | INVALID`) — cleaner API but a larger migration.

## Cross-references

- `backend/agents/graph/state.py` — `AlphaResult` definition.
- `backend/agents/graph/nodes/validation.py:386` — original RESET site
  (SELF_CORRECT clears `is_valid` and `validation_error` so the next
  VALIDATE can re-stamp them).
- Quality review V-26.58.
