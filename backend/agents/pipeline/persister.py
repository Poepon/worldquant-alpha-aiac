"""Pipeline persister stage — the single DB writer.

run_pipeline_session funnels every SimResult through ONE persister coroutine,
which owns its own session, so no two coroutines ever share an asyncpg
connection. This mirrors node_save_results' call into _incremental_save_alphas
(which writes PASS / PASS_PROVISIONAL Alpha rows, stamps the bandit-recommended
arm, links the hypothesis, and commits internally).

Scope (Sub-phase 0 / Unit 2b): PASS/PROVISIONAL alpha persistence — the core
capture path. Two node_save_results responsibilities are deferred to Sub-phase 1
(observability/attribution, not alpha capture): FAIL → alpha_failures rows, and
flushing the in-memory trace_records (which also need the iteration-grouping
redefinition the pipeline introduces, design-doc F3).
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


def build_persister(
    *,
    run_id: Optional[int],
    save_fn: Optional[Callable[..., Awaitable[List]]] = None,
) -> Callable[[Any, List[SimResult]], Awaitable[int]]:
    """Build the ``persist(session, results) -> persisted_count`` callable for
    run_pipeline_session.

    Args:
        run_id: the FLAT session's experiment_runs.id, stamped on every alpha.
        save_fn: injection seam for tests; defaults to
            node persistence ``_incremental_save_alphas``.
    """
    if save_fn is None:
        from backend.agents.graph.nodes.persistence import _incremental_save_alphas
        save_fn = _incremental_save_alphas

    async def persist(session: Any, results: List[SimResult]) -> int:
        persisted = 0
        for r in results:
            st = getattr(r, "state", None)
            if st is None:
                continue  # slot-timeout / pre-sim failure → nothing to persist
            pending = _attr(st, "pending_alphas", []) or []
            if not pending:
                continue
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
                    hypothesis_id=_resolve_hypothesis_id(st),
                )
                persisted += len(saved or [])
            except Exception:  # noqa: BLE001 — one bad result must not drop the batch
                logger.exception("[pipeline] persist of one result failed (skipped)")
        return persisted

    return persist
