# V-26.11 — RAG / KB writes share caller's `db` (partial fix in place)

**File**: `backend/agents/services/rag_service.py`
**Severity**: 🟡 medium
**Status**: partial — `_track_retrieval_hit` isolated; `record_*` still shares

## Problem

`RAGService` is constructed with the caller's `AsyncSession`. The three
write paths (`_track_retrieval_hit`, `record_success_pattern`,
`record_failure_pattern`, `update_brain_status`) call `await self.db.commit()`
inside the method, which commits **everything the caller has staged so
far** — not just the KB row.

Concrete scenarios where this hurts:

1. `persistence.py:_incremental_save_alphas` adds Alpha rows via savepoint,
   then calls `rag.record_success_pattern(...)`. If a downstream step
   (e.g. `fields_used` UPDATE, last_alpha_persisted_at write) raises and
   triggers rollback, the alpha rows roll back but the KB write is already
   committed. KB now references an alpha_id that doesn't exist.

2. `evaluation.py:1409` calls `record_failure_pattern` from inside the
   evaluate node. Same shape — KB write commits whatever was staged
   above it.

3. `_track_retrieval_hit` runs as part of `query()`. The caller didn't
   know retrieve would commit. Any in-flight transaction the caller had
   open gets flushed without warning.

## Mitigation in place (2026-05-13)

- `_track_retrieval_hit` now opens an isolated `AsyncSessionLocal()`
  and commits there. The caller's transaction is no longer affected by
  retrieve-time bookkeeping.

## What's still pending

`record_success_pattern`, `record_failure_pattern`, and
`update_brain_status` still call `self.db.commit()` mid-method. The
cleanest fix is to mirror the `_track_retrieval_hit` approach: each
method opens its own `AsyncSessionLocal` for the write.

That migration is non-trivial because the helpers used inside
(`_find_similar_success`, `_find_similar_pitfall`) reference `self.db`
directly. Options:

- **Option A**: refactor `_find_similar_*` to accept a `db` parameter,
  then have `record_*` pass the isolated session through.
- **Option B**: have `record_*` construct a temporary `RAGService(kb_db)`
  instance and delegate to its `_find_similar_*` — simpler but creates a
  short-lived service object per write.

Either is straightforward. Estimated effort 1-2h. The current state is
safer than pre-fix because the most frequent commit point (`_track_retrieval_hit`
fires on every retrieve) is now isolated, but the `record_*` paths still
have the original failure mode.

## Tracking

- Pre-fix commits: V-26.4/5/7/27 + V-26.13/26 + V-26.8 / V-26.11 partial.
- This file lives until `record_success_pattern` / `record_failure_pattern`
  / `update_brain_status` all open isolated sessions.
