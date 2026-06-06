"""DB row -> MiningState hydration for the pool workers (Phase 1b B2).

A claimed candidate_queue row carries everything the S (simulate) and E
(evaluate) pools need to reconstruct a SINGLE-candidate MiningState — the same
shape the FLAT pipeline's ``producer._sim_ready_payload`` emits. The S/E nodes
(``run_simulate`` / ``run_evaluate``) then run verbatim over it.

Threshold flow (matches FLAT today): the eval-band thresholds (EVAL_SHARPE_MIN,
…) are read LIVE from settings via ``_eval_thresholds()`` inside node_evaluate —
NOT frozen per candidate (settings-sweep is Phase 2). Only the per-task
ROLE-snapshot overrides (effective_default_test_period / effective_sharpe_submit_
min — 终审 #7 first-class columns) are frozen at HG emit time and fed onto the
MiningState so a Consultant-era candidate keeps its testPeriod / sharpe gate even
when evaluated by a User-role E worker.
"""
from typing import Any, Dict, Optional

from backend.database import AsyncSessionLocal
from backend.agents.graph.state import MiningState, AlphaCandidate


def hydrate_candidate_state(
    row: Any,
    intent_config_snapshot: Optional[Dict[str, Any]] = None,
) -> MiningState:
    """Build a single-candidate MiningState from a claimed candidate_queue row.

    Used by BOTH S (row fresh from HG, sim_result empty) and E (row carries S's
    sim_result, which becomes the candidate's metrics for verdict routing).
    ``intent_config_snapshot`` is the parent hyp_intent.config_snapshot (carries
    brain_role_snapshot for the consultant-mode flag); pass None on the S path
    if not needed.
    """
    ctx: Dict[str, Any] = dict(row.context or {})
    snap: Dict[str, Any] = dict(intent_config_snapshot or {})
    role_snap: Dict[str, Any] = dict(snap.get("brain_role_snapshot", {}) or {})

    # candidate_queue.sim_result wire format (S writes, E reads): the structured
    # post-sim outcome {metrics, simulation_success, simulation_error, alpha_id}.
    # None/{} = S has not simulated yet (S path).
    sr: Dict[str, Any] = row.sim_result if isinstance(row.sim_result, dict) else {}
    metrics = sr.get("metrics", {}) or {}
    candidate = AlphaCandidate(
        expression=row.expression,
        is_valid=True,  # HG already validated before emitting the row
        hypothesis=ctx.get("hypothesis"),
        explanation=ctx.get("explanation"),
        metrics=dict(metrics),  # {} on the S path; BRAIN metrics on the E path
        is_simulated=bool(sr),
        simulation_success=sr.get("simulation_success"),
        simulation_error=sr.get("simulation_error"),
        alpha_id=sr.get("alpha_id"),
        quality_status=(row.verdict or "PENDING"),
    )

    hyp_id = row.current_hypothesis_id
    state = MiningState(
        # --- task scope ---
        task_id=int(row.task_id) if row.task_id is not None else 0,
        region=row.region,
        universe=row.universe or "TOP3000",
        delay=row.delay if row.delay is not None else 1,
        dataset_id=row.dataset_id or "",
        dataset_category=row.dataset_category or "",
        # --- the one candidate S/E processes ---
        pending_alphas=[candidate],
        # --- lineage (hypotheses.id is the anchor; scalar + list for LangGraph
        #     scalar-drop resilience, gotcha #6) ---
        current_hypothesis_id=hyp_id,
        current_hypothesis_ids=([hyp_id] if hyp_id is not None else []),
        rag_ab_arm=row.rag_ab_arm or "",
        # --- role-snapshot first-class cols (终审 #7) — only what S/E read.
        #     (effective_region_universes is an HG/scheduling concern, a
        #     Dict[str,list] of a different shape than MiningState's
        #     effective_region_universes_at_start Dict[str,str]; S/E never read
        #     it, so it is intentionally NOT hydrated here.) ---
        effective_default_test_period=row.effective_default_test_period,
        effective_sharpe_submit_min=row.effective_sharpe_submit_min,
        brain_consultant_mode_at_start=role_snap.get("brain_consultant_mode_at_start"),
        # --- buffered HG context (patterns/pitfalls/focused_fields/... — present
        #     for completeness; S/E don't re-derive them, but keep them so trace
        #     + any default-OFF screen reads what HG saw) ---
        patterns=ctx.get("patterns", []) or [],
        pitfalls=ctx.get("pitfalls", []) or [],
        focused_fields=ctx.get("focused_fields", []) or [],
        distilled_concepts=ctx.get("distilled_concepts", []) or [],
        hypotheses=ctx.get("hypotheses", []) or [],
        cognitive_layer_id_used=ctx.get("cognitive_layer_id_used", "") or "",
        g8_forest_referenced_ids=ctx.get("g8_forest_referenced_ids", []) or [],
        # fresh trace for this candidate's S+E steps (HG trace already in
        # row.trace_records; persister concatenates).
        trace_steps=[],
    )
    return state


def hg_run_config(trace_service: Any = None) -> Dict[str, Any]:
    """The RunnableConfig the pool passes to run_simulate / run_evaluate.

    trace_service=None → DB-free per-candidate tracing (the pool persists trace
    rows itself via the persister, not through a live TraceService).
    """
    return {"configurable": {"trace_service": trace_service}}


# The FK anchor + attribution carrier for scheduler-inserted (scope-less)
# intents. candidate_queue.task_id / alphas.task_id are FKs to mining_tasks; a
# coerced task_id=0 violates the FK (no task 0) and a NULL drops the row from the
# bandit / daily-quota (which filter task_id IS NOT NULL). One resident pool task
# gives the whole chain a valid, attributable task_id. Cached per worker process.
_RESIDENT_TASK_NAME = "__pool_resident__"
_RESIDENT_TASK_ID: Optional[int] = None


async def _resident_pool_task_id(s: Any) -> int:
    """Find-or-create the resident pool MiningTask (the FK anchor); cache its id."""
    global _RESIDENT_TASK_ID
    if _RESIDENT_TASK_ID is not None:
        return _RESIDENT_TASK_ID
    from sqlalchemy import select
    from backend.models import MiningTask
    from backend.config import settings as _s
    row = (await s.execute(
        select(MiningTask).where(MiningTask.task_name == _RESIDENT_TASK_NAME).limit(1)
    )).scalar_one_or_none()
    if row is None:
        row = MiningTask(
            task_name=_RESIDENT_TASK_NAME,
            region=getattr(_s, "DEFAULT_REGION", "USA"),
            universe=getattr(_s, "DEFAULT_UNIVERSE", "TOP3000"),
            status="ACTIVE",          # resident scope (not RUNNING → no watchdog revive)
            schedule="POOL",          # not FLAT/ONESHOT → run_mining_task never dispatches it
            daily_goal=0,
        )
        s.add(row)
        await s.flush()
        await s.commit()
    _RESIDENT_TASK_ID = int(row.id)
    return _RESIDENT_TASK_ID


async def hydrate_hg_state(intent: Any, *, session_factory: Any = None) -> MiningState:
    """Build the initial round MiningState for the HG pool from a hyp_intent row.

    Mirrors the FLAT round build (mining_tasks.py:576-595): scope from the intent
    + role-snapshot from config_snapshot["brain_role_snapshot"] + a fresh
    fields/operators fetch (reusing _get_dataset_fields / _get_operators). RAG /
    distill / hypothesis context is produced by run_hypothesis itself, not
    hydrated here.

    available_dataset_pool defaults to [] (legacy single-anchor) — the Phase-1
    cross-dataset complementary pool (_build_dataset_pool) is a follow-up; empty
    = the byte-for-byte legacy code-gen path.
    """
    # Phase 1c-delete: mining_tasks.py was deleted; these two fetch helpers were
    # extracted verbatim to backend/tasks/fetch_helpers.py (the pool's only dep
    # on the old FLAT task module).
    from backend.tasks.fetch_helpers import _get_dataset_fields, _get_operators

    factory = session_factory or AsyncSessionLocal
    snap: Dict[str, Any] = dict(intent.config_snapshot or {})
    role: Dict[str, Any] = dict(snap.get("brain_role_snapshot", {}) or {})
    region = intent.region
    universe = intent.universe or "TOP3000"
    delay = intent.delay if intent.delay is not None else 1
    dataset_id = intent.dataset_id or ""

    async with factory() as s:
        # scope-less scheduler intents (task_id NULL) → anchor to the resident pool
        # task so candidate_queue/alphas FKs resolve + the row is bandit-attributable.
        resident_tid = await _resident_pool_task_id(s)
        fields = await _get_dataset_fields(s, dataset_id, region, universe, delay) if dataset_id else []
        operators = await _get_operators(s)

    return MiningState(
        task_id=int(intent.task_id) if intent.task_id is not None else resident_tid,
        region=region,
        universe=universe,
        delay=delay,
        dataset_id=dataset_id,
        fields=fields or [],
        operators=operators or [],
        num_alphas_target=int(intent.fanout) if intent.fanout else 3,
        # Pool 1a: carry the parent intent id so node_hypothesis can dedup typed-
        # Hypothesis creation on lease-recycle (a re-claimed intent re-runs HG).
        hyp_intent_id=int(intent.id) if intent.id is not None else None,
        available_dataset_pool=[],  # legacy single-anchor (Phase-1 pool = follow-up)
        brain_consultant_mode_at_start=role.get("brain_consultant_mode_at_start"),
        effective_default_test_period=role.get("effective_default_test_period"),
        effective_sharpe_submit_min=role.get("effective_sharpe_submit_min"),
    )
