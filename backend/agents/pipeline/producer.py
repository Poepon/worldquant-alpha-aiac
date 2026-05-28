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

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from backend.agents.pipeline.consumer import build_consumer_stages
from backend.agents.pipeline.persister import build_persister
from backend.agents.pipeline.runner import _with_timeout, run_pipeline_session
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


# Sentinel ending the internal hyp_q in the split (Sub-phase 3) producer.
_HYP_SENTINEL = object()


def _pattr(obj: Any, name: str, default: Any) -> Any:
    """Read a field off a Pydantic state OR a dict (ainvoke may return either)."""
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


async def _drain_feedback(feedback_ctx, handle_feedback, push, db, wf, should_stop,
                          op_timeout=None) -> int:
    """Run the F2 feedback drain phase — shared by the single-stage and the
    split-stage producers. No-op (returns 0) when the loop isn't wired. Each
    handler call is bounded by op_timeout (its rewrite/mutate/crossover does LLM
    + BRAIN I/O that must not hang the drain)."""
    if feedback_ctx is None or handle_feedback is None:
        return 0
    handled = 0
    feedback_ctx.mark_primary_done()
    while not should_stop():
        event = await feedback_ctx.next_event()
        if event is None:
            break  # quiescence sentinel (or stop)
        try:
            await _with_timeout(handle_feedback(event, push, db, wf), op_timeout)
            handled += 1
        except Exception:  # noqa: BLE001
            # END the drain (DO NOT `continue` — the previous "log + skip"
            # caused a permanent freeze on task 3738, 2026-05-28): a timed-
            # out/failed handler may have wait_for-cancelled mid asyncpg
            # query and poisoned the producer's SHARED `db` session
            # (dc7c8e5 class). The next iteration's handler/next_event would
            # then hang on that same session with no timer → loop parks in
            # select forever (same root as the next_round_inputs gap
            # `be1d287` closed). Mirrors the gen-op break-on-timeout at L166.
            # event_done() in finally → outstanding decrements → quiescence
            # for any remaining events when produce() returns cleanly.
            logger.exception(
                "[pipeline] feedback handler failed/timed out; ending drain "
                "(shared session integrity uncertain)")
            break
        finally:
            feedback_ctx.event_done()
    return handled


def build_producer(
    *,
    session_factory: Callable[[], Any],
    workflow_factory: Callable[[Any], Any],
    next_round_inputs: Callable[[Any], Awaitable[Optional[Dict[str, Any]]]],
    num_alphas: int,
    code_producer_count: int = 1,
    target_candidates: Optional[int] = None,
    handle_feedback: Optional[Callable[..., Awaitable[None]]] = None,
    queue_maxsize: int = 0,
    op_timeout: Optional[float] = None,
) -> Callable[..., Awaitable[None]]:
    """The pipeline's generation producer — split at HYPOTHESIS into two internal
    stages joined by ``hyp_q``. Returns the ``produce(push, should_stop[,
    feedback_ctx])`` callable the runner drives. (Sub-phase 3; the prior
    single-stage producer was removed 2026-05-28 — this is the only path.)

    Stage 1 (one hyp-producer, owns the DB session): next_round_inputs →
    wf.run(stop_after_hypothesis=True) → push the post-hypothesis state onto an
    internal hyp_q. This is the pluggable hypothesis SOURCE seam — a paper-derived
    generator would push equivalent states onto the same queue.

    Stage 2 (``code_producer_count`` code-producers, DB-free): drain hyp_q →
    wf.run_codegen → push validated candidates onto the runner's work queue.

    All coroutines share ONE workflow: only the single hyp-producer touches the
    DB (run_hypothesis' RAG, sequentially); run_codegen is DB-free, so concurrent
    code-producers never share a session (F1). The sub-graphs are pre-built to
    avoid a concurrent lazy-build race.
    """

    async def produce(push, should_stop, feedback_ctx=None) -> None:
        cpc = max(1, int(code_producer_count))
        hq_max = queue_maxsize if queue_maxsize and queue_maxsize > 0 else max(2, 2 * cpc)
        hyp_q: asyncio.Queue = asyncio.Queue(maxsize=hq_max)
        st = {"produced": 0, "rounds": 0}

        async with session_factory() as db:
            wf = workflow_factory(db)
            # Pre-build both sub-graphs so concurrent code-producers don't race
            # the lazy build inside run_codegen.
            if getattr(wf, "_hyp_graph", None) is None:
                wf._hyp_graph = wf._build_hyp_graph()
            if getattr(wf, "_codegen_graph", None) is None:
                wf._codegen_graph = wf._build_codegen_graph()

            def _at_target() -> bool:
                return target_candidates is not None and st["produced"] >= target_candidates

            async def _hyp_producer() -> None:
                try:
                    while not should_stop() and not _at_target():
                        # next_round_inputs runs DB-heavy cursor/bandit/ownership
                        # reads + cursor writes on the producer's SHARED asyncpg
                        # session. It is the one producer-loop await that must
                        # also be bounded: if a prior wait_for-cancel poisoned the
                        # session (dc7c8e5 class) or a lock stalls, an UNWRAPPED
                        # hang here parks the loop with no timer (select forever),
                        # the hyp-producer never reaches its finally → sentinels
                        # never sent → code-producers block on an empty hyp_q →
                        # TOTAL permanent freeze (observed task 3737, 2026-05-28).
                        # On timeout the outer `except` below drains stage 2
                        # cleanly (cursor already persisted → re-dispatch resumes).
                        inputs = await _with_timeout(next_round_inputs(db), op_timeout)
                        if not inputs:
                            break
                        st["rounds"] += 1
                        try:
                            result = await _with_timeout(wf.run(
                                task=inputs["task"],
                                dataset_id=inputs["dataset_id"],
                                fields=inputs.get("fields") or [],
                                operators=inputs.get("operators") or [],
                                num_alphas=num_alphas,
                                config=inputs.get("config"),
                                generate_only=True,
                                stop_after_hypothesis=True,
                            ), op_timeout)
                        except Exception:  # noqa: BLE001
                            # END stage 1 (don't `continue`): a timed-out/failed
                            # round may have poisoned the producer's SHARED
                            # asyncpg session (wait_for cancel mid RAG query —
                            # dc7c8e5 precedent). Break so the finally sends the
                            # sentinels and the code-producers drain cleanly.
                            logger.exception(
                                "[pipeline] hyp round failed/timed out; ending "
                                "stage 1 (shared session integrity uncertain)")
                            break
                        hyp_state = result.get("state") if isinstance(result, dict) else None
                        if hyp_state is not None:
                            await hyp_q.put((hyp_state, inputs["dataset_id"]))
                except Exception:  # noqa: BLE001 — a stage-1 crash must still drain stage 2
                    logger.exception("[pipeline] hyp-producer crashed; draining code-producers")
                finally:
                    for _ in range(cpc):
                        await hyp_q.put(_HYP_SENTINEL)

            async def _code_producer() -> None:
                while True:
                    item = await hyp_q.get()
                    if item is _HYP_SENTINEL:
                        return
                    if should_stop() or _at_target():
                        continue  # drain remaining sentinels; produce nothing more
                    hyp_state, dataset_id = item
                    try:
                        final = await _with_timeout(wf.run_codegen(hyp_state), op_timeout)
                    except Exception:  # noqa: BLE001 — one bad/slow hypothesis ≠ dead producer
                        logger.exception("[pipeline] code-producer failed/timed out a hypothesis (skipped)")
                        continue
                    pending = _pattr(final, "pending_alphas", []) or []
                    gen_trace = _pattr(final, "trace_steps", []) or []
                    for ac in pending:
                        if not getattr(ac, "is_valid", None):
                            continue
                        cand = Candidate(
                            expression=getattr(ac, "expression", "") or "",
                            context={"dataset_id": dataset_id},
                            trace_records=list(gen_trace),
                            payload=_sim_ready_payload(final, ac),
                        )
                        await push(cand)
                        st["produced"] += 1

            hyp_task = asyncio.create_task(_hyp_producer(), name="hyp-producer")
            code_tasks = [
                asyncio.create_task(_code_producer(), name=f"code-producer-{i}")
                for i in range(cpc)
            ]
            try:
                await hyp_task
                await asyncio.gather(*code_tasks)
            finally:
                for t in (hyp_task, *code_tasks):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(hyp_task, *code_tasks, return_exceptions=True)

            handled = await _drain_feedback(
                feedback_ctx, handle_feedback, push, db, wf, should_stop, op_timeout)

        logger.info(
            "[pipeline] split-producer finished after %d round(s), %d candidate(s), %d feedback handled",
            st["rounds"], st["produced"], handled,
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
    code_producer_count: int = 1,
    op_timeout: Optional[float] = None,
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
    # The producer is split at HYPOTHESIS (hyp-producer → hyp_q → code-producers,
    # Sub-phase 3) — now the only generation path (single-stage was removed
    # 2026-05-28). code_producer_count=1 keeps the seam with no extra concurrency.
    produce = build_producer(
        session_factory=session_factory,
        workflow_factory=producer_workflow_factory,
        next_round_inputs=next_round_inputs,
        num_alphas=num_alphas,
        code_producer_count=code_producer_count,
        target_candidates=daily_goal,
        handle_feedback=handle_feedback,
        op_timeout=op_timeout,
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
        op_timeout=op_timeout,
    )
