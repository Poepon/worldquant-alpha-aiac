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
