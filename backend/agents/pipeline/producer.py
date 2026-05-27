"""Pipeline producer + FLAT-session assembly.

The producer drives generation: it loops over dataset rounds, runs the
MiningWorkflow generation-only graph, and pushes each validated candidate
(as a sim-ready Candidate) onto the work queue. It owns its OWN DB session
(used only for the injected ``next_round_inputs`` dataset/cursor logic and the
generation nodes' own reads) — never shared with the consumers or persister.

``run_flat_pipeline_session`` wires producer + consumer + persister into
``run_pipeline_session``. The FLAT-specific dataset cursor / bandit / stop
logic is INJECTED via ``next_round_inputs`` and the ``daily_goal`` gate, so the
assembly stays unit-testable and the live wiring lands in the
_run_flat_iteration integration step.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from backend.agents.pipeline.consumer import build_consumer_stages
from backend.agents.pipeline.persister import build_persister
from backend.agents.pipeline.runner import run_pipeline_session
from backend.agents.pipeline.types import Candidate

logger = logging.getLogger(__name__)


def _sim_ready_payload(gen_state: Any, alpha_candidate: Any) -> Any:
    """Slice the generation state down to ONE candidate for the consumer.

    Copies the full generation context (region/universe/dataset_id/delay/
    thresholds/role snapshot/task_id/hypothesis ids) but replaces pending_alphas
    with just this candidate and clears trace/generated so the consumer's
    sim/eval trace is per-candidate.
    """
    update = {"pending_alphas": [alpha_candidate], "trace_steps": [], "generated_alphas": []}
    if hasattr(gen_state, "model_copy"):
        return gen_state.model_copy(update=update)
    if hasattr(gen_state, "copy"):
        try:
            return gen_state.copy(update=update)  # pydantic v1 fallback
        except TypeError:
            pass
    if isinstance(gen_state, dict):
        merged = dict(gen_state)
        merged.update(update)
        return merged
    return gen_state


def build_producer(
    *,
    session_factory: Callable[[], Any],
    workflow_factory: Callable[[Any], Any],
    next_round_inputs: Callable[[Any], Awaitable[Optional[Dict[str, Any]]]],
    num_alphas: int,
    should_continue: Optional[Callable[[], bool]] = None,
    target_candidates: Optional[int] = None,
    handle_feedback: Optional[Callable[..., Awaitable[None]]] = None,
) -> Callable[..., Awaitable[None]]:
    """Build the ``produce(push, should_stop[, feedback_ctx])`` callable.

    Args:
        session_factory: () -> async context manager → the producer's own session.
        workflow_factory: (db) -> MiningWorkflow for generation.
        next_round_inputs: async (db) -> dict | None. Returns the next round's
            {task, dataset_id, fields, operators, config} (the FLAT cursor /
            bandit pick), or None to stop (cursor exhausted / ownership lost /
            task paused). Owns all FLAT-specific round-selection logic.
        num_alphas: candidates to request per generation round.
        should_continue: optional () -> bool gate checked before each round.
        target_candidates: optional cap on TOTAL candidates produced this
            session (mirrors the legacy loop's daily_goal, which counts alphas
            ATTEMPTED per round — not persisted-PASS, which at a ~0 PASS rate
            would never terminate). Stop once produced >= target_candidates.
        handle_feedback: optional (F2) ``async (event, push, db, wf) -> None``.
            When set AND the runner passes a ``feedback_ctx`` (3rd produce arg),
            the producer runs a DRAIN phase after primary generation: it pulls
            feedback events and lets the handler ``push`` derived candidates
            (R1b retry/mutate, G5 crossover) using the producer's own ``db`` +
            ``wf`` (F1: only the producer + persister touch the DB). The runner
            owns termination (the handler just regenerates).
    """

    async def produce(push, should_stop, feedback_ctx=None) -> None:
        rounds = 0
        produced = 0
        handled = 0
        async with session_factory() as db:
            wf = workflow_factory(db)
            # --- primary generation phase -------------------------------------
            while not should_stop():
                if target_candidates is not None and produced >= target_candidates:
                    break
                if should_continue is not None and not should_continue():
                    break
                inputs = await next_round_inputs(db)
                if not inputs:
                    break
                rounds += 1
                result = await wf.run(
                    task=inputs["task"],
                    dataset_id=inputs["dataset_id"],
                    fields=inputs.get("fields") or [],
                    operators=inputs.get("operators") or [],
                    num_alphas=num_alphas,
                    config=inputs.get("config"),
                    generate_only=True,
                )
                gen_state = result.get("state") if isinstance(result, dict) else None
                pending = (result.get("pending_alphas") if isinstance(result, dict) else None) or []
                # The batch's shared generation trace (RAG/DISTILL/HYPOTHESIS/
                # CODE_GEN/VALIDATE) — carried on each candidate so the persister
                # can flush a complete per-candidate trajectory (the consumer
                # appends SIMULATE/EVALUATE). _sim_ready_payload cleared the
                # state's trace_steps, so this is the only carrier.
                gen_trace = (result.get("trace_steps") if isinstance(result, dict) else None) or []
                for ac in pending:
                    cand = Candidate(
                        expression=getattr(ac, "expression", "") or "",
                        context={"dataset_id": inputs["dataset_id"]},
                        trace_records=list(gen_trace),
                        payload=_sim_ready_payload(gen_state, ac),
                    )
                    await push(cand)
                    produced += 1

            # --- feedback drain phase (F2; only when wired active) ------------
            # Runs while consumers/persister are still processing in-flight work,
            # so a FAIL→retry / FAIL→mutate / PASS→crossover can regenerate. The
            # runner ends it with a None (quiescence) or on stop.
            if feedback_ctx is not None and handle_feedback is not None:
                feedback_ctx.mark_primary_done()
                while not should_stop():
                    event = await feedback_ctx.next_event()
                    if event is None:
                        break  # quiescence sentinel (or stop)
                    try:
                        await handle_feedback(event, push, db, wf)
                        handled += 1
                    except Exception:  # noqa: BLE001 — one bad event ≠ dead drain
                        logger.exception("[pipeline] feedback handler failed (skipped)")
                    finally:
                        feedback_ctx.event_done()
        logger.info(
            "[pipeline] producer finished after %d round(s), %d candidate(s), %d feedback handled",
            rounds, produced, handled,
        )

    return produce


async def run_flat_pipeline_session(
    *,
    session_factory: Callable[[], Any],
    producer_workflow_factory: Callable[[Any], Any],
    consumer_workflow: Any,
    next_round_inputs: Callable[[Any], Awaitable[Optional[Dict[str, Any]]]],
    run_id: Optional[int],
    num_alphas: int,
    num_consumers: int,
    daily_goal: Optional[int] = None,
    queue_maxsize: int = 0,
    persist_every: int = 1,
    stop_event: Optional[Any] = None,
    persist_fn: Optional[Callable[[Any, Any], Awaitable[int]]] = None,
    acquire_slot: Optional[Callable[[], Awaitable[bool]]] = None,
    release_slot: Optional[Callable[[], Awaitable[None]]] = None,
    refresher: Any = None,
    reward_hook: Optional[Callable[[Any, float], None]] = None,
    classify_feedback: Optional[Callable[[Any], Any]] = None,
    handle_feedback: Optional[Callable[..., Awaitable[None]]] = None,
) -> dict:
    """Assemble producer + consumer + persister and run one pipeline session.

    Returns the run_pipeline_session stats dict (produced / simulated /
    persisted / errors / slot_timeouts / dropped_on_stop / persist_failures,
    and — when feedback is active — feedback_events / feedback_handled).

    ``persist_fn`` is an injection seam for tests; defaults to the real
    build_persister(run_id).

    F2: ``classify_feedback`` (persister-side, DB-free) and ``handle_feedback``
    (producer-side, owns db+wf) MUST be passed together or not at all — the
    persister classifying events the producer never drains would hang on
    quiescence. When both None the path is the pre-F2 byte-identical pipeline.
    """
    if (classify_feedback is None) != (handle_feedback is None):
        raise ValueError(
            "classify_feedback and handle_feedback must be provided together "
            "(persister-classify + producer-handle are two ends of one loop)"
        )
    persist = persist_fn or build_persister(run_id=run_id, reward_hook=reward_hook)

    # daily_goal is a PRODUCED-candidate cap (≈ legacy's alphas-attempted-per-
    # round counting), NOT a persisted-PASS gate — gating on persisted-PASS at a
    # ~0 PASS rate would never terminate and burn the full max_iters every
    # session. Termination otherwise comes from next_round_inputs returning None
    # (cursor exhausted / ownership lost / paused / max_iters).
    produce = build_producer(
        session_factory=session_factory,
        workflow_factory=producer_workflow_factory,
        next_round_inputs=next_round_inputs,
        num_alphas=num_alphas,
        target_candidates=daily_goal,
        handle_feedback=handle_feedback,
    )
    simulate, evaluate = build_consumer_stages(
        consumer_workflow,
        config={"configurable": {"trace_service": None, "run_id": run_id}},
        refresher=refresher,
    )

    return await run_pipeline_session(
        produce=produce,
        simulate=simulate,
        evaluate=evaluate,
        persist=persist,
        session_factory=session_factory,
        num_consumers=num_consumers,
        queue_maxsize=queue_maxsize,
        persist_every=persist_every,
        stop_event=stop_event,
        acquire_slot=acquire_slot,
        release_slot=release_slot,
        classify_feedback=classify_feedback,
    )
