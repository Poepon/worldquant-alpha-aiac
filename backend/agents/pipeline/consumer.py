"""Pipeline consumer stages — per-candidate simulate + evaluate (DB-free).

The runner calls ``simulate(candidate)`` while holding a BRAIN slot, then
``evaluate(candidate, sim_outcome)`` after releasing it. Both delegate to the
MiningWorkflow sim/eval sub-graphs (reusing node_simulate / node_evaluate
unchanged), so per-candidate behaviour matches the in-round batch path.

DB safety: node_simulate (dedup) and node_evaluate (Q10/R1a/PR06 logging) each
open their OWN ephemeral AsyncSessionLocal internally — never a shared session —
so N consumers run concurrently without sharing an asyncpg connection. We pass
``trace_service=None`` so trace steps accumulate in-memory on the state; the
persister flushes them through its own session.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from backend.agents.pipeline.types import Candidate, SimResult

logger = logging.getLogger(__name__)


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Read ``name`` off a Pydantic state OR a plain dict (LangGraph ainvoke may
    return either)."""
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def build_consumer_stages(
    workflow: Any,
    *,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[
    Callable[[Candidate], Awaitable[Any]],
    Callable[[Candidate, Any], Awaitable[SimResult]],
]:
    """Build the ``(simulate, evaluate)`` callables for run_pipeline_session.

    ``candidate.payload`` MUST be a sim-ready MiningState (pending_alphas == the
    single validated candidate, empty trace_steps, full generation context),
    as emitted by the producer.

    ``config`` is the RunnableConfig passed to the sub-graphs. It MUST NOT carry
    a shared ``trace_service`` (keep it None) so trace stays in-memory for the
    persister; run_id / other configurable keys are fine.
    """

    async def simulate(candidate: Candidate) -> Any:
        # Holds a BRAIN slot (the runner acquired it). Returns the post-sim
        # state, which evaluate() consumes after the slot is released.
        return await workflow.run_simulate(candidate.payload, config=config)

    async def evaluate(candidate: Candidate, sim_state: Any) -> SimResult:
        eval_state = await workflow.run_evaluate(sim_state, config=config)
        pending = _attr(eval_state, "pending_alphas", []) or []
        first = pending[0] if pending else None

        ok = bool(getattr(first, "simulation_success", False)) if first is not None else False
        verdict = getattr(first, "quality_status", None) if first is not None else None
        metrics = getattr(first, "metrics", {}) if first is not None else {}
        error = getattr(first, "simulation_error", None) if (first is not None and not ok) else None
        trace = _attr(eval_state, "trace_steps", []) or []

        return SimResult(
            candidate=candidate,
            ok=ok,
            metrics=metrics if isinstance(metrics, dict) else {},
            verdict=verdict,
            trace_records=list(trace),
            error=error,
            state=eval_state,
        )

    return simulate, evaluate
