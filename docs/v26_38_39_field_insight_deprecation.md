# V-26.38 / V-26.39 — FIELD_INSIGHT / HYPOTHESIS_INSIGHT deprecation

**Severity**: 🟡 medium
**Status**: write-gated 2026-05-13 (V-24.E); retrieve path never existed
**Decision deadline**: 2026-Q3 (re-evaluate at Plan v5+ Phase 3 review)

## What this is

`KnowledgeType.FIELD_INSIGHT` and `KnowledgeType.HYPOTHESIS_INSIGHT`
were introduced as KB row categories in
`backend/agents/core/knowledge.py:23`. The write side
(`feedback_agent.py:1039-1111`) populated them with field- and
hypothesis-level summary text — e.g. "field X correlates with PASS
rate", "hypothesis Y showed 3 PROV alphas in this round".

The retrieve side never landed. `rag_service.query()` only fetches
`SUCCESS_PATTERN` and `FAILURE_PITFALL` entry_types. The two _INSIGHT
categories accumulated 4170 rows over 6 months with zero retrieve
traffic (verified by `scripts/kb_hit_audit.py` 2026-05-13).

V-24.E response (2026-05-13) flipped
`settings.WRITE_FIELD_HYPOTHESIS_INSIGHTS=False`, stopping the write
flood. Old rows stay in DB (soft-deleted via `is_active=False`).

V-26 review surfaced two open questions about this state:

- **V-26.38** — the enum lives in `core/knowledge.py` and looks like an
  active category. Reader of the code can't tell from the enum alone
  that nothing reads it.
- **V-26.39** — V-24.E backlog entry "enable retrieve path" has no
  owner / no decision deadline.

## Decision options

Three paths, none committed yet:

### A. Retire entirely (recommended absent a use case)

- Drop the enum members (`FIELD_INSIGHT`, `HYPOTHESIS_INSIGHT`)
- Drop the write block in `feedback_agent.py:1039-1111`
- Drop the metrics_tracker counter at `metrics_tracker.py:396`
- Hard-delete or archive the 4170 old rows
- Estimated ~2h

### B. Build the retrieve path (only if a concrete consumer is identified)

- Add `_get_insights_enhanced(...)` to `rag_service.py`
- Surface insight strings to the LLM via `query()` return shape
- Decide whether insights affect scoring or just appear as context
- Estimated ~1 day plus prompt-engineering iteration

### C. Punt to Q3

- Keep enum + write-gate, no retrieve path
- Re-evaluate when Phase 3 main-loop flip is on the table
- Risk: code stays subtly half-finished; new contributors will keep
  asking what these types are for

## Where this is recorded today

- `backend/config.py:275-280` — `WRITE_FIELD_HYPOTHESIS_INSIGHTS=False`
  comment notes "no _get_*_insights path". Updated by V-24.E.
- `backend/agents/feedback_agent.py:1039-1111` — write block guarded
  by the settings flag.
- `backend/agents/core/knowledge.py:23` — enum members carry no
  deprecation marker yet.

## Action this PR takes

Adds this backlog doc with explicit ownership ("TBD by Q3 review") and
the three options. **No code changes** — the V-24.E gate is sufficient
mitigation for the runtime problem; the underlying decision (A vs B vs
C) needs a product call, not code.

When a decision is made:

- **A**: delete this file along with the code removal.
- **B**: convert to a tracking ticket pointing at the consumer use case.
- **C**: extend the deadline header and re-review at the next phase.

## Cross-references

- V-24.E commit (2026-05-13) — write-gating + 4170 deactivation.
- V-26 quality review — items V-26.38 + V-26.39.
- `scripts/kb_hit_audit.py` — confirms zero retrieve traffic.
