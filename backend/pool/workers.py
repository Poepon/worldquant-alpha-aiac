"""S (simulate) + E (evaluate) pool worker loops (Phase 1b B3).

Each is a resident loop: check drain/budget → claim a candidate_queue row →
hydrate a single-candidate MiningState → run the verbatim node (run_simulate /
run_evaluate) → write the result + advance the stage. The nodes open their OWN
ephemeral sessions internally (F1 contract), so the worker holds no shared
session across the (long) BRAIN sim.

S→E wire format: S persists the structured sim outcome into
candidate_queue.sim_result = {metrics, simulation_success, simulation_error,
alpha_id}; E re-hydrates the post-sim candidate from it (see hydrate.py).

INERT until ``ENABLE_POOL_PIPELINE`` is flipped on (the supervisor starts these
loops only then — B6). budget:sims INCR is deferred to the brain_adapter
success-branch hook (B5); this loop only CHECKS the budget pre-claim.
"""
import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from backend.database import AsyncSessionLocal
from backend.agents.graph.workflow import MiningWorkflow
from backend.agents.pipeline.types import Candidate, SimResult
from backend.agents.pipeline.persister import build_persister
from backend.models import CandidateQueue, HypothesisIntent
from backend.pool import stages as st
from backend.pool.budget import sims_budget_exceeded
from backend.pool.drain import is_draining
from backend.pool.hydrate import hydrate_candidate_state, hg_run_config
from backend.pool.queue import claim_one, complete, fail_or_retry


def build_workflow(db: Any) -> MiningWorkflow:
    """One MiningWorkflow per worker. db is only used to build RAGService (the
    sim/eval sub-graphs never touch it — their nodes self-open sessions), so a
    long-lived unused session is fine (NullPool acquires no connection until a
    query runs)."""
    return MiningWorkflow(db)


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Read off a Pydantic state OR a dict (LangGraph ainvoke returns either)."""
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def _serialize_trace(steps: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for s in (steps or []):
        if isinstance(s, dict):
            out.append(s)
        elif hasattr(s, "model_dump"):
            out.append(s.model_dump())
        elif hasattr(s, "dict"):
            out.append(s.dict())
    return out


async def _fetch_intent_snapshot(hyp_intent_id: Optional[int]) -> Dict[str, Any]:
    """Parent hyp_intent.config_snapshot (carries brain_role_snapshot). {} if no
    parent / not found. Role-snapshot thresholds S/E actually read are already
    first-class candidate columns; this is for the consultant-mode audit flag."""
    if hyp_intent_id is None:
        return {}
    async with AsyncSessionLocal() as s:
        row = await s.get(HypothesisIntent, hyp_intent_id)
        return dict(row.config_snapshot or {}) if row is not None else {}


# =============================================================================
# Per-candidate processors (the testable core; mock the workflow in tests)
# =============================================================================

async def s_process_one(
    workflow: Any, row: Any, intent_snapshot: Dict[str, Any], config: Dict[str, Any],
) -> Dict[str, Any]:
    """Run BRAIN simulate on one claimed candidate. Returns the structured
    sim_result + serialized SIMULATE trace for candidate_queue."""
    state = hydrate_candidate_state(row, intent_snapshot)
    sim_state = await workflow.run_simulate(state, config)
    pending = _attr(sim_state, "pending_alphas", []) or []
    first = pending[0] if pending else None
    metrics = (getattr(first, "metrics", {}) if first is not None else {}) or {}
    sim_ok = bool(getattr(first, "simulation_success", False)) if first is not None else False
    sim_err = getattr(first, "simulation_error", None) if first is not None else None
    alpha_id = getattr(first, "alpha_id", None) if first is not None else None
    trace = _serialize_trace(_attr(sim_state, "trace_steps", []))
    return {
        "sim_result": {
            "metrics": metrics if isinstance(metrics, dict) else {},
            "simulation_success": sim_ok,
            "simulation_error": sim_err,
            "alpha_id": alpha_id,
        },
        "trace": trace,
    }


async def e_process_one(
    workflow: Any, row: Any, intent_snapshot: Dict[str, Any], config: Dict[str, Any],
) -> SimResult:
    """Run evaluate on one post-sim candidate; return a SimResult ready for the
    shared persister (PASS/PROV→alphas, non-PASS→alpha_failures, trace flush)."""
    state = hydrate_candidate_state(row, intent_snapshot)
    eval_state = await workflow.run_evaluate(state, config)
    pending = _attr(eval_state, "pending_alphas", []) or []
    first = pending[0] if pending else None
    ok = bool(getattr(first, "simulation_success", False)) if first is not None else False
    verdict = getattr(first, "quality_status", None) if first is not None else None
    metrics = (getattr(first, "metrics", {}) if first is not None else {}) or {}
    error = getattr(first, "simulation_error", None) if (first is not None and not ok) else None
    e_trace = _serialize_trace(_attr(eval_state, "trace_steps", []))
    candidate = Candidate(
        expression=row.expression,
        context={"dataset_id": row.dataset_id},
        # HG + S trace already buffered on the row; E trace is on SimResult.
        trace_records=list(row.trace_records or []),
        payload=eval_state,
    )
    return SimResult(
        candidate=candidate,
        ok=ok,
        metrics=metrics if isinstance(metrics, dict) else {},
        verdict=verdict,
        trace_records=e_trace,
        error=error,
        state=eval_state,
    )


async def persist_eval(result: SimResult, *, persister: Any = None,
                       session_factory: Any = None) -> int:
    """Persist one E result via the shared FLAT persister (run_id=None — Phase 1d
    drops run_id). Returns the persisted PASS count."""
    factory = session_factory or AsyncSessionLocal
    p = persister or build_persister(run_id=None)
    async with factory() as s:
        return await p(s, [result])


# =============================================================================
# Resident loops (thin; started by the supervisor in B6, INERT until then)
# =============================================================================

async def s_loop(*, worker_id: str, poll_sec: float = 2.0, lease_sec: int = 1800,
                 max_attempts: int = 3, should_stop: Any = None) -> None:
    async with AsyncSessionLocal() as wdb:
        workflow = build_workflow(wdb)
        config = hg_run_config()
        while not (should_stop and should_stop()):
            if is_draining("s") or sims_budget_exceeded():
                await asyncio.sleep(poll_sec)
                continue
            row = await claim_one(CandidateQueue, st.SIM_PENDING, worker_id, lease_sec)
            if row is None:
                await asyncio.sleep(poll_sec)
                continue
            try:
                snap = await _fetch_intent_snapshot(row.hyp_intent_id)
                out = await s_process_one(workflow, row, snap, config)
                await complete(
                    CandidateQueue, row.id, st.EVAL_PENDING,
                    updates={
                        "sim_result": out["sim_result"],
                        "trace_records": list(row.trace_records or []) + out["trace"],
                    },
                )
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[pool.s] candidate {row.id} sim failed: {ex}")
                await fail_or_retry(CandidateQueue, row.id, st.SIM_PENDING, max_attempts, error=str(ex))


async def e_loop(*, worker_id: str, poll_sec: float = 2.0, lease_sec: int = 600,
                 max_attempts: int = 3, should_stop: Any = None) -> None:
    async with AsyncSessionLocal() as wdb:
        workflow = build_workflow(wdb)
        config = hg_run_config()
        while not (should_stop and should_stop()):
            if is_draining("e"):
                await asyncio.sleep(poll_sec)
                continue
            row = await claim_one(CandidateQueue, st.EVAL_PENDING, worker_id, lease_sec)
            if row is None:
                await asyncio.sleep(poll_sec)
                continue
            try:
                snap = await _fetch_intent_snapshot(row.hyp_intent_id)
                result = await e_process_one(workflow, row, snap, config)
                await persist_eval(result)
                await complete(
                    CandidateQueue, row.id, st.CAND_DONE,
                    updates={"verdict": result.verdict, "error": result.error},
                )
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[pool.e] candidate {row.id} eval failed: {ex}")
                await fail_or_retry(CandidateQueue, row.id, st.EVAL_PENDING, max_attempts, error=str(ex))
