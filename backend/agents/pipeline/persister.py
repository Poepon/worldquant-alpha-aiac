"""Pipeline persister stage — the single DB writer.

run_pipeline_session funnels every SimResult through ONE persister coroutine,
which owns its own session, so no two coroutines ever share an asyncpg
connection. This mirrors node_save_results' call into _incremental_save_alphas
(which writes PASS / PASS_PROVISIONAL Alpha rows, stamps the bandit-recommended
arm, links the hypothesis, and commits internally).

Writes (mirrors node_save_results + run_with_persistence): PASS/PROVISIONAL
Alpha rows (_incremental_save_alphas), non-PASS alpha_failures rows
(_incremental_save_failures, Sub-phase 1), and the buffered trace_steps
(_flush_trace, Sub-phase 1). F3 iteration grouping: ONE iteration per candidate
— each candidate's row group is its full trajectory (the batch's generation
trace carried on candidate.trace_records + the consumer's SIMULATE/EVALUATE on
SimResult.trace_records), so step_order never collides across candidates.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, List, Optional

from backend.agents.pipeline.types import SimResult

logger = logging.getLogger(__name__)


def _attr(obj: Any, name: str, default: Any) -> Any:
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def _resolve_hypothesis_id(state: Any) -> Optional[int]:
    # Mirror node_save_results: scalar current_hypothesis_id, else first of the
    # per-round list.
    hid = _attr(state, "current_hypothesis_id", None)
    if hid is None:
        hids = _attr(state, "current_hypothesis_ids", None) or []
        if hids:
            hid = hids[0]
    return hid


async def _flush_trace(session, task_id, run_id, iteration, steps) -> int:
    """Write one candidate's buffered trace steps (gen + sim/eval) to trace_steps
    under a single iteration. Reuses TraceService (auto step_order + per-row
    commit). Returns the number of steps written. Soft-fail per step."""
    from backend.agents.services.trace_service import TraceService

    ts = TraceService(session, task_id=task_id, run_id=run_id, iteration=iteration)
    written = 0
    for step in steps:
        rec = ts.create_record(
            step_type=_attr(step, "step_type", "STEP"),
            input_data=_attr(step, "input_data", {}) or {},
            output_data=_attr(step, "output_data", {}) or {},
            duration_ms=int(_attr(step, "duration_ms", 0) or 0),
            status=_attr(step, "status", "SUCCESS") or "SUCCESS",
            error_message=_attr(step, "error_message", None),
        )
        if await ts.persist_record(rec) is not None:
            written += 1
    return written


def build_persister(
    *,
    run_id: Optional[int],
    save_fn: Optional[Callable[..., Awaitable[List]]] = None,
    save_failures_fn: Optional[Callable[..., Awaitable[int]]] = None,
    flush_trace_fn: Optional[Callable[..., Awaitable[int]]] = None,
) -> Callable[[Any, List[SimResult]], Awaitable[int]]:
    """Build the ``persist(session, results) -> persisted_count`` callable for
    run_pipeline_session.

    Args:
        run_id: the FLAT session's experiment_runs.id, stamped on every alpha.
        save_fn: injection seam for tests; defaults to node persistence
            ``_incremental_save_alphas`` (PASS/PASS_PROVISIONAL → alphas table).
        save_failures_fn: injection seam for tests; defaults to
            ``_incremental_save_failures`` (non-PASS → alpha_failures log, the
            failure-attribution path node_save_results writes on the legacy
            path). Returns the persisted PASS count (failures are a side write).
    """
    if save_fn is None:
        from backend.agents.graph.nodes.persistence import _incremental_save_alphas
        save_fn = _incremental_save_alphas
    if save_failures_fn is None:
        from backend.agents.graph.nodes.persistence import _incremental_save_failures
        save_failures_fn = _incremental_save_failures
    if flush_trace_fn is None:
        flush_trace_fn = _flush_trace

    # Monotonic per-candidate iteration (F3). Single persister coroutine → no
    # concurrency on this counter.
    iter_state = {"n": 0}

    async def persist(session: Any, results: List[SimResult]) -> int:
        persisted = 0
        for r in results:
            cand = getattr(r, "candidate", None)
            # Trace flush (one iteration per candidate) — independent of the
            # alpha persist below; runs even for slot-timeout/empty results so a
            # generated-but-not-simulated candidate still shows its partial
            # trajectory. Never blocks alpha capture.
            iter_state["n"] += 1
            trace_steps = (
                list(getattr(cand, "trace_records", None) or [])
                + list(getattr(r, "trace_records", None) or [])
            )
            if trace_steps:
                _trace_tid = _attr(
                    getattr(r, "state", None) or getattr(cand, "payload", None),
                    "task_id", None,
                )
                if _trace_tid is not None:
                    try:
                        await flush_trace_fn(session, _trace_tid, run_id, iter_state["n"], trace_steps)
                    except Exception:  # noqa: BLE001 — trace is observability, never fatal
                        logger.exception("[pipeline] trace flush failed (skipped)")

            st = getattr(r, "state", None)
            if st is None:
                continue  # slot-timeout / pre-sim failure → nothing more to persist
            pending = _attr(st, "pending_alphas", []) or []
            if not pending:
                continue
            hypothesis_id = _resolve_hypothesis_id(st)
            try:
                # _incremental_save_alphas filters to PASS/PASS_PROVISIONAL,
                # stamps the bandit arm, links the hypothesis, and commits.
                saved = await save_fn(
                    session,
                    task_id=_attr(st, "task_id", None),
                    run_id=run_id,
                    region=_attr(st, "region", None),
                    universe=_attr(st, "universe", None),
                    dataset_id=_attr(st, "dataset_id", None),
                    pending_alphas=pending,
                    hypothesis_id=hypothesis_id,
                )
                persisted += len(saved or [])
            except Exception:  # noqa: BLE001 — one bad result must not drop the batch
                logger.exception("[pipeline] persist of one result failed (skipped)")
            # Failure log (non-PASS) — separate write, never blocks the PASS path.
            try:
                await save_failures_fn(
                    session,
                    task_id=_attr(st, "task_id", None),
                    run_id=run_id,
                    pending_alphas=pending,
                    hypothesis_id=hypothesis_id,
                    rag_ab_arm=_attr(st, "rag_ab_arm", None),
                )
            except Exception:  # noqa: BLE001
                logger.exception("[pipeline] failure-log write failed (skipped)")
        return persisted

    return persist
