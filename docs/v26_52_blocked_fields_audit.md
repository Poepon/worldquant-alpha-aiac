# V-26.52 — REGION_BLOCKED_FIELDS audit backlog (CHN / EUR / ASI / GLB)

**File**: `backend/agents/seed_pool/composite_fields.py:59-67`
**Severity**: 🟡 medium
**Owner**: unassigned
**Status**: backlog

## Problem

`REGION_BLOCKED_FIELDS` filters composite candidates whose required fields
are known absent from the given BRAIN region. Today only `USA` has been
probed (2026-05-12); the other four regions sit at `set()`, meaning
**no blocked fields**. Any composite referencing a fundamental / sentiment /
analyst field that doesn't actually exist in (say) CHN TOP3000 will silently
flow through to `_dedup_and_validate` → BRAIN simulate → guaranteed FAIL,
burning quota.

V-26.52 was confirmed real by the audit; the impact is unobserved only
because mining traffic today is overwhelmingly USA.

## Mitigation in place

Commit `2026-05-13` adds a one-shot logger.warning per process when
`build_candidates_from_composite_fields` is called with `region != "USA"`
and the region has no entries in `REGION_BLOCKED_FIELDS`. This makes the
gap observable without changing behavior.

## What the audit needs to produce

For each of CHN / EUR / ASI / GLB:

1. Pull the union of `required_fields` from
   `backend/agents/seed_pool/composite_fields.yaml` (currently 25 distinct
   ingredients across all composites).
2. For each region, query `datafields` for `region={X}, universe={primary}`
   (and any secondary universes the mining mode uses).
3. Diff: which ingredients are **absent** in that region's datafields?
4. Populate `REGION_BLOCKED_FIELDS[region] = {missing fields}`.

Sketch query:

```sql
SELECT DISTINCT field_id
FROM datafields f
JOIN datasets d ON f.dataset_id = d.id
WHERE d.region = 'CHN'
  AND d.universe IN ('TOP3000', 'TOP1200');
```

Cross-reference against composite ingredients:

```python
from backend.agents.seed_pool.composite_fields import list_composites
ingredients = set()
for c in list_composites():
    ingredients.update(c.get("required_fields") or [])
# diff vs DB result
```

## When to run

- Before any non-USA mining session (`mining_session.start` with
  `region != "USA"`).
- After any addition to `composite_fields.yaml` that introduces new
  ingredient fields.
- After any BRAIN-side datafields sync that changes coverage.

## Out of scope here

- LLM `promising_fields` selection (orthogonal — handled by
  `_composite_is_eligible` via `available_set`).
- UNIVERSAL_PV_FIELDS (already region-agnostic by design — `close`,
  `volume`, etc. are guaranteed in every BRAIN region).

## Tracking

When the audit lands:
- Update this file with the populated `REGION_BLOCKED_FIELDS` dict.
- Remove the one-shot warning at `composite_fields.py:build_candidates_from_composite_fields`.
- Update the comment block above `REGION_BLOCKED_FIELDS` to record the
  audit date per region.
