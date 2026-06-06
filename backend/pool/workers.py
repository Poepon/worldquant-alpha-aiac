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
from backend.agents.services.llm_service import (
    set_task_function_overrides,
    clear_task_function_overrides,
)
from backend.models import CandidateQueue, HypothesisIntent
from backend.pool import stages as st
from backend.pool.budget import sims_budget_exceeded, tokens_budget_exceeded, incr_sims
from backend.pool.drain import is_draining
from backend.pool.hydrate import hydrate_candidate_state, hydrate_hg_state, hg_run_config
from backend.pool.queue import claim_one, complete, fail_or_retry, renew_lease


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


async def _renew_loop(model: Any, row_id: int, lease_sec: int, worker_id: str,
                      stop: "asyncio.Event", interval_sec: Optional[float] = None) -> None:
    """Heartbeat coroutine: re-stamp the claimed row's lease every ~lease_sec/4
    so a legitimately-long op (a BRAIN sim holds 30-90 min) is NOT lease-recycled
    out from under a still-alive worker. lease/4 (not /3) gives 4 ticks per lease →
    survives up to THREE consecutive missed renewals (a transient renew exception
    just `continue`s to the next tick) before the lease can lapse. Stops when
    ``stop`` is set (op finished) or ``renew_lease`` reports the row is gone /
    terminal / owned by someone else (already recycled away — nothing left to
    renew). ``interval_sec`` is injectable for tests; production → max(30, lease/4)."""
    interval = interval_sec if interval_sec is not None else max(30.0, lease_sec / 4.0)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return  # stop signalled → op done
        except asyncio.TimeoutError:
            pass
        try:
            ok = await renew_lease(model, row_id, lease_sec, worker_id=worker_id)
        except Exception as ex:  # noqa: BLE001 — a transient renew failure must not
            # abandon a live op; the lease margin covers a missed beat, retry next tick.
            logger.debug(f"[pool.heartbeat] renew {model.__tablename__}:{row_id} failed (retry): {ex}")
            continue
        if not ok:
            return  # row recycled/terminal/reclaimed — stop renewing


async def _run_with_lease_heartbeat(model: Any, row_id: int, lease_sec: int,
                                    worker_id: str, coro: Any, *,
                                    interval_sec: Optional[float] = None) -> Any:
    """Await ``coro`` while a background heartbeat renews its lease. Cancels the
    heartbeat in finally so it never outlives the op."""
    stop = asyncio.Event()
    hb = asyncio.create_task(_renew_loop(model, row_id, lease_sec, worker_id, stop, interval_sec))
    try:
        return await coro
    finally:
        stop.set()
        hb.cancel()
        try:
            await hb
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


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

async def s_loop(*, worker_id: str, poll_sec: float = 2.0, lease_sec: int = 3600,
                 max_attempts: int = 3, should_stop: Any = None) -> None:
    # lease_sec 3600 (1h) = the real worst-case BRAIN hold, which is one of two
    # MUTUALLY-EXCLUSIVE paths (not their sum): single-sim = slot-acquire ≤1800 +
    # poll ≤1800 (BRAIN_SIM_MAX_WAIT_SEC); multisim = poll ≤3600
    # (BRAIN_MULTISIM_MAX_WAIT_SEC, no slot-acquire). Both ≈3600, so the lease is a
    # belt. The heartbeat (_run_with_lease_heartbeat) is the PRIMARY guard — it
    # re-stamps the lease every lease_sec/4 so a live long sim is never lease-
    # recycled + double-run (gotcha G2).
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
                out = await _run_with_lease_heartbeat(
                    CandidateQueue, row.id, lease_sec, worker_id,
                    s_process_one(workflow, row, snap, config),
                )
                # worker_id-guarded: a NO-OP (returns False) if our lease lapsed
                # and the row was recycled + reclaimed by another S worker — we
                # must not clobber its stage/sim_result (lost-update).
                advanced = await complete(
                    CandidateQueue, row.id, st.EVAL_PENDING,
                    updates={
                        "sim_result": out["sim_result"],
                        "trace_records": list(row.trace_records or []) + out["trace"],
                    },
                    worker_id=worker_id,
                )
                # Count the BRAIN sim ONLY after a confirmed successful POST
                # (终审 #6: is_simulated != BRAIN-truth — never count pre-POST /
                # slot-timeout / 429), and only if WE actually advanced the row
                # (a stale/recycled claim's duplicate sim must not double-charge
                # the daily budget). Pool-sim count; the brain_adapter global hook
                # (incl opt/auto-submit) is a later refinement.
                if advanced and out["sim_result"].get("simulation_success"):
                    incr_sims(1)
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[pool.s] candidate {row.id} sim failed: {ex}")
                await fail_or_retry(CandidateQueue, row.id, st.SIM_PENDING, max_attempts,
                                    error=str(ex), worker_id=worker_id)


async def e_loop(*, worker_id: str, poll_sec: float = 2.0, lease_sec: int = 1800,
                 max_attempts: int = 3, should_stop: Any = None) -> None:
    # lease_sec 1800 (30m): node_evaluate can issue fresh sign-flip-retry sims, so
    # E is not always sub-minute. Heartbeat-renewed like S (G2).
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
                result = await _run_with_lease_heartbeat(
                    CandidateQueue, row.id, lease_sec, worker_id,
                    e_process_one(workflow, row, snap, config),
                )
                # Persist FIRST, then advance the stage guarded by worker_id. This
                # ordering is deliberate: a crash BETWEEN the two leaves the row
                # EVAL_INFLIGHT → lease-recycle re-evaluates it → the candidate
                # outcome is never LOST (vs complete-first, which would mark a row
                # DONE then lose the persist on a crash). The stage is never
                # clobbered (complete()'s worker_id guard NO-OPs a stale advance).
                # Residual (rare, heartbeat-gated): on a recycle race a duplicate
                # persist can occur — the alphas table is idempotent (ON CONFLICT
                # DO NOTHING on alpha_id, persistence.py), but alpha_failures /
                # trace_steps have no per-candidate idempotency, so a duplicate FAIL
                # row + trace can land. Full failure-write idempotency is a P1
                # follow-up; accepted here because losing a PASS is worse than a
                # duplicate FAIL and the heartbeat makes the recycle race rare.
                await persist_eval(result)
                await complete(
                    CandidateQueue, row.id, st.CAND_DONE,
                    updates={"verdict": result.verdict, "error": result.error},
                    worker_id=worker_id,
                )
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[pool.e] candidate {row.id} eval failed: {ex}")
                await fail_or_retry(CandidateQueue, row.id, st.EVAL_PENDING, max_attempts,
                                    error=str(ex), worker_id=worker_id)


# =============================================================================
# HG (hypothesis + generation, fused) pool worker
# =============================================================================

def _resolve_hyp_id(state: Any) -> Optional[int]:
    """Scalar current_hypothesis_id, else first of the per-round list (mirrors
    persister._resolve_hypothesis_id — LangGraph scalar-drop resilience)."""
    hid = _attr(state, "current_hypothesis_id", None)
    if hid is None:
        hids = _attr(state, "current_hypothesis_ids", None) or []
        hid = hids[0] if hids else None
    return hid


def _candidate_row_kwargs(final: Any, ac: Any, intent: Any,
                          gen_trace: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Map one post-codegen is_valid AlphaCandidate → candidate_queue INSERT kwargs.

    CRITICAL (B4 gotcha #1): the full RAG/distill/hypothesis context lives on the
    post-codegen state, NOT on Candidate.context. The pool persists it into
    candidate_queue.context so S/E (separate processes) can re-hydrate it.
    """
    ctx = {
        "hypothesis": getattr(ac, "hypothesis", None),
        "patterns": _attr(final, "patterns", []) or [],
        "pitfalls": _attr(final, "pitfalls", []) or [],
        "focused_fields": _attr(final, "focused_fields", []) or [],
        "distilled_concepts": _attr(final, "distilled_concepts", []) or [],
        "hypotheses": _attr(final, "hypotheses", []) or [],
        "cognitive_layer_id_used": _attr(final, "cognitive_layer_id_used", "") or "",
        "g8_forest_referenced_ids": _attr(final, "g8_forest_referenced_ids", []) or [],
    }
    _delay = _attr(final, "delay", None)
    return dict(
        hyp_intent_id=intent.id,
        task_id=_attr(final, "task_id", intent.task_id),
        current_hypothesis_id=_resolve_hyp_id(final),
        stage=st.SIM_PENDING,
        expression=getattr(ac, "expression", "") or "",
        region=_attr(final, "region", intent.region),
        universe=_attr(final, "universe", intent.universe),
        delay=_delay if _delay is not None else (intent.delay if intent.delay is not None else 1),
        dataset_id=_attr(final, "dataset_id", intent.dataset_id),
        dataset_category=_attr(final, "dataset_category", "") or "",
        effective_default_test_period=_attr(final, "effective_default_test_period", None),
        effective_sharpe_submit_min=_attr(final, "effective_sharpe_submit_min", None),
        rag_ab_arm=_attr(final, "rag_ab_arm", "") or "",
        context=ctx,
        trace_records=gen_trace,
        attempts=0,
    )


async def hg_process_one(workflow: Any, intent: Any, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run rag→distill→hypothesis→codegen→validate→[self_correct] for one intent;
    return candidate_queue INSERT kwargs for each is_valid candidate."""
    state = await hydrate_hg_state(intent)
    hyp_state = await workflow.run_hypothesis(state, config)
    final = await workflow.run_codegen(hyp_state, config)
    pending = _attr(final, "pending_alphas", []) or []
    gen_trace = _serialize_trace(_attr(final, "trace_steps", []))
    return [
        _candidate_row_kwargs(final, ac, intent, gen_trace)
        for ac in pending
        if getattr(ac, "is_valid", None)
    ]


async def emit_candidates(rows_kwargs: List[Dict[str, Any]], *, session_factory: Any = None) -> int:
    """INSERT the candidate_queue rows (PENDING_SIM) in one transaction."""
    if not rows_kwargs:
        return 0
    factory = session_factory or AsyncSessionLocal
    async with factory() as s:
        async with s.begin():
            s.add_all([CandidateQueue(**kw) for kw in rows_kwargs])
    return len(rows_kwargs)


async def emit_candidates_and_complete(
    intent_id: int, rows_kwargs: List[Dict[str, Any]], *,
    worker_id: Optional[str] = None, session_factory: Any = None,
) -> int:
    """ATOMIC emit + intent-DONE: INSERT the candidate rows AND flip the parent
    hyp_intent → DONE in ONE transaction.

    Why atomic (review finding): candidate_queue has no idempotency key. If emit
    and the intent→DONE flip are two separate commits and the worker dies (or its
    lease is recycled) in between, the candidates are committed but the intent
    stays CLAIMED → lease-recycle re-PENDINGs it → the HG pool re-runs and emits a
    DUPLICATE candidate set (up to max_attempts×), every duplicate then BRAIN-
    simulated. One transaction closes that window. Owner-guarded: if the intent
    was recycled + reclaimed by another HG worker, emit NOTHING (return -1).
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as s:
        async with s.begin():
            # FOR UPDATE so the owner check + emit + intent→DONE serialize against a
            # concurrent recycle_expired (which also locks) — closes the narrow
            # READ-COMMITTED window where a just-re-PENDING'd intent gets flipped to
            # DONE. SQLite (tests) ignores FOR UPDATE — harmless no-op.
            intent = await s.get(HypothesisIntent, intent_id, with_for_update=True)
            if intent is None:
                return 0  # intent vanished (hard-deleted) — nothing to do
            if worker_id is not None and intent.claimed_by != worker_id:
                return -1  # stale claim — another worker owns this intent now
            if rows_kwargs:
                s.add_all([CandidateQueue(**kw) for kw in rows_kwargs])
            intent.stage = st.INTENT_DONE
            intent.claimed_by = None
            intent.lease_expires_at = None
    return len(rows_kwargs)


async def hg_loop(*, worker_id: str, poll_sec: float = 3.0, lease_sec: int = 1800,
                  max_attempts: int = 3, should_stop: Any = None) -> None:
    # lease_sec 1800 (30m): the LLM-bound rag→distill→hypothesis→codegen→validate→
    # [self_correct] chain runs minutes, not the 30-90m a BRAIN sim can; the
    # heartbeat re-stamps a live chain every lease/4 so even a rare long one is not
    # recycled mid-generation (re-run would duplicate candidates, G2). Kept at 1800
    # (not 3600) because with POOL_N_HG=1 a *dead* HG worker strands generation for
    # one full lease before recycle — a smaller lease halves that MTTR, and the
    # heartbeat (not the lease size) is what protects a live worker.
    config = hg_run_config()
    while not (should_stop and should_stop()):
        if is_draining("hg") or tokens_budget_exceeded():
            await asyncio.sleep(poll_sec)
            continue
        intent = await claim_one(HypothesisIntent, st.INTENT_PENDING, worker_id, lease_sec)
        if intent is None:
            await asyncio.sleep(poll_sec)
            continue
        # Per-intent LLM routing: bind the frozen llm_overrides on the contextvar
        # around the whole generation, reset in finally (no cross-intent leak).
        token = set_task_function_overrides((intent.config_snapshot or {}).get("llm_overrides"))
        try:
            async with AsyncSessionLocal() as wdb:
                workflow = build_workflow(wdb)
                rows = await _run_with_lease_heartbeat(
                    HypothesisIntent, intent.id, lease_sec, worker_id,
                    hg_process_one(workflow, intent, config),
                )
            # Atomic emit + intent→DONE (one txn) so a crash/recycle between the
            # two never duplicates the candidate set. worker_id-guarded.
            n = await emit_candidates_and_complete(intent.id, rows, worker_id=worker_id)
            if n < 0:
                logger.warning(f"[pool.hg] intent {intent.id} claim went stale "
                               f"(lease-recycled) — candidates NOT emitted")
            else:
                logger.info(f"[pool.hg] intent {intent.id} → {n} candidates")
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"[pool.hg] intent {intent.id} failed: {ex}")
            await fail_or_retry(HypothesisIntent, intent.id, st.INTENT_PENDING, max_attempts,
                                error=str(ex), worker_id=worker_id)
        finally:
            clear_task_function_overrides(token)
