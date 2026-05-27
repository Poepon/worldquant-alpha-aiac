"""F2-2/F2-3: R1b retry + hypothesis-mutate through the pipeline feedback channel.

One UNIFIED classifier (persister-side, sync, DB-free) turns a FAIL SimResult
into a RETRY or MUTATE FeedbackEvent based on its R1a attribution; one
dispatching handler (producer-side, owns db+wf) regenerates accordingly. Wired
(by _run_flat_iteration_pipeline) only for whichever of ENABLE_R1B_RETRY_LOOP /
ENABLE_R1B_HYPOTHESIS_MUTATE is on; otherwise the loop stays inactive.

- **RETRY** (FAIL + attribution implementation): rewrite the expression
  (wf.run_retry → node_code_gen_retry) + re-validate, then re-push the valid
  rewrite. Depth caps at R1B_MAX_RETRIES_PER_ALPHA (carried on the candidate's
  r1b_retries_attempted_this_alpha).
- **MUTATE** (FAIL + attribution hypothesis): propose a revised hypothesis
  (wf.run_mutate → node_hypothesis_mutate, which INSERTs the new Hypothesis row),
  then drive a fresh generation round with it injected (wf.run(generate_only) via
  the legacy consumed-hypothesis slot) and push the new candidates. The chain is
  bounded by the DB mutation-depth cap (R1B_MAX_MUTATION_DEPTH), chained through
  the new hypothesis's id on current_hypothesis_id.

Conflict rule (mirrors legacy [V1.0-A2-3]): **mutate dominates retry on "both"**
attribution. To stop a single failed hypothesis spawning N mutations (legacy
mutates once per round per unique hypothesis; the pipeline classifies each FAIL
candidate individually), the classifier DEDUPES MUTATE by hypothesis statement
within the session.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from backend.agents.pipeline.producer import _sim_ready_payload
from backend.agents.pipeline.types import (
    FEEDBACK_MUTATE,
    FEEDBACK_RETRY,
    Candidate,
    FeedbackEvent,
    SimResult,
)

logger = logging.getLogger(__name__)


def _sattr(state: Any, name: str, default: Any) -> Any:
    """Read a top-level state field off a Pydantic MiningState OR a dict
    (LangGraph ainvoke may return either)."""
    if state is None:
        return default
    if isinstance(state, dict):
        return state.get(name, default)
    return getattr(state, name, default)


def _first_pending(state: Any):
    pending = _sattr(state, "pending_alphas", None) or []
    return pending[0] if pending else None


# --------------------------------------------------------------------------- #
# Classifier (persister-side, sync, DB-free)                                   #
# --------------------------------------------------------------------------- #
def build_feedback_classifier(
    *, retry_on: bool, mutate_on: bool, max_retries: int
) -> Callable[[SimResult], Optional[FeedbackEvent]]:
    """Build the unified persister-side classifier.

    Emits MUTATE for hypothesis-side failures (deduped per hypothesis statement)
    and RETRY for implementation-side failures with budget left. ``both`` goes to
    MUTATE when mutate_on (mutate dominates), else to RETRY. Returns None for
    PASS, sim-infra failures (no attribution), exhausted budgets, and the
    already-requested duplicates.
    """
    mutate_requested: set[str] = set()  # hypothesis statements already queued

    def classify(result: SimResult) -> Optional[FeedbackEvent]:
        a = _first_pending(getattr(result, "state", None))
        if a is None:
            return None
        if getattr(a, "quality_status", None) != "FAIL":
            return None
        attr = (getattr(a, "metrics", None) or {}).get("_r1a_attribution")

        # Mutate dominates on "both". One mutation per unique failing hypothesis
        # statement (dedupe) — node_hypothesis_mutate self-guards depth/budget,
        # but dedupe stops N same-hypothesis candidates each spawning a mutation.
        if mutate_on and attr in ("hypothesis", "both"):
            hyp = (getattr(a, "hypothesis", "") or "").strip()
            if hyp:
                if hyp not in mutate_requested:
                    mutate_requested.add(hyp)
                    return FeedbackEvent(kind=FEEDBACK_MUTATE, result=result)
                # already mutating this hypothesis → mutate dominates → drop
                return None
            # no hypothesis text → mutate can't fire; a "both" alpha still falls
            # through to the retry path below (a pure "hypothesis" one won't match).

        if retry_on and attr in ("implementation", "both"):
            retries = _sattr(getattr(result, "state", None),
                             "r1b_retries_attempted_this_alpha", 0) or 0
            if retries < max_retries:
                return FeedbackEvent(kind=FEEDBACK_RETRY, result=result)
        return None

    return classify


# --------------------------------------------------------------------------- #
# Handler (producer-side, owns db + wf)                                        #
# --------------------------------------------------------------------------- #
async def _do_retry(event: FeedbackEvent, push, wf, config) -> None:
    st = getattr(event.result, "state", None)
    if st is None:
        return
    new_state = await wf.run_retry(st, config=config)
    a = _first_pending(new_state)
    if a is None:
        return
    # node_code_gen_retry sets PENDING on a real rewrite; a skipped/no-op retry
    # leaves quality_status FAIL. validate then sets is_valid — only re-simulate
    # a genuinely rewritten AND valid alpha.
    if getattr(a, "quality_status", None) != "PENDING":
        return
    if not getattr(a, "is_valid", False):
        return
    ds = (getattr(event.result.candidate, "context", None) or {}).get("dataset_id")
    cand = Candidate(
        expression=getattr(a, "expression", "") or "",
        context={"dataset_id": ds, "r1b_retry": True},
        trace_records=list(_sattr(new_state, "trace_steps", None) or []),
        payload=_sim_ready_payload(new_state, a),
    )
    await push(cand)


async def _do_mutate(event: FeedbackEvent, push, db, wf, config, num_alphas: int,
                     mut_counter: Optional[dict] = None, max_mutations: int = 0) -> None:
    # HARD per-session cap (task 3735 amplification). The DB depth cap is skipped
    # when the failed alpha has no current_hypothesis_id (parent=None — common on
    # fresh FLAT alphas), and the classifier's statement-dedupe can't catch
    # ever-changing LLM statements, so many distinct failing hypotheses could each
    # spawn a FULL (~95s LLM) regeneration unbounded. This guarantees termination.
    if mut_counter is not None and max_mutations and mut_counter["n"] >= max_mutations:
        return
    st = getattr(event.result, "state", None)
    if st is None:
        return
    mut_state = await wf.run_mutate(st, config=config)
    new_hyp = _sattr(mut_state, "r1b_pending_new_hypothesis", None)
    if not (isinstance(new_hyp, dict) and new_hyp.get("statement")):
        return  # no-op mutation (per-cycle/depth/token budget or cross-pillar drift)
    if not new_hyp.get("hypothesis_id"):
        # The Hypothesis row INSERT did not persist (returns None on DB / FK
        # failure) → no DB depth anchor. Regenerating would inject
        # hypothesis_id=None → the new candidates' current_hypothesis_id=None →
        # node_hypothesis_mutate's depth check (gated on a non-None parent) is
        # SKIPPED for every further mutation → the depth cap is disabled and the
        # chain runs unbounded (each mutation yields a fresh statement the dedupe
        # can't catch → quiescence never reached). Drop this mutation instead of
        # risking an unbounded feedback loop. (Legacy tolerates a missing id
        # because the outer round loop bounds it; the pipeline has no such bound.)
        logger.warning(
            "[pipeline] mutate produced a hypothesis with no persisted id "
            "(INSERT failed?); skipping regeneration to keep the mutation chain "
            "depth-bounded"
        )
        return

    task_id = _sattr(st, "task_id", None)
    if task_id is None or db is None:
        return
    from backend.models import MiningTask

    task = await db.get(MiningTask, task_id)
    if task is None:
        return
    # This mutation will regenerate — count it against the session cap.
    if mut_counter is not None:
        mut_counter["n"] += 1
    # Inject via the legacy consumed-slot path: wf.run reads + clears
    # task.config["__r1b_consumed_pending_hypothesis"] and node_hypothesis injects
    # it (skipping the exploration LLM call), so new alphas link to the mutated
    # hypothesis id (→ depth chaining). Rebind (not pop) so run's clear restores it.
    task.config = {**(task.config or {}),
                   "__r1b_consumed_pending_hypothesis": new_hyp}
    result = await wf.run(
        task=task,
        dataset_id=_sattr(st, "dataset_id", "") or "",
        fields=_sattr(st, "fields", None) or [],
        operators=_sattr(st, "operators", None) or [],
        num_alphas=num_alphas,
        config=config,
        generate_only=True,
    )
    gen_state = result.get("state") if isinstance(result, dict) else None
    pending = (result.get("pending_alphas") if isinstance(result, dict) else None) or []
    gen_trace = (result.get("trace_steps") if isinstance(result, dict) else None) or []
    ds = _sattr(st, "dataset_id", None)
    for ac in pending:
        cand = Candidate(
            expression=getattr(ac, "expression", "") or "",
            context={"dataset_id": ds, "r1b_mutate": True},
            trace_records=list(gen_trace),
            payload=_sim_ready_payload(gen_state, ac),
        )
        await push(cand)


def build_feedback_handler(
    *, config: Optional[dict] = None, mutate_num_alphas: int = 4,
    max_mutations: int = 0,
) -> Callable[[FeedbackEvent, Callable[[Candidate], Awaitable[None]], Any, Any], Awaitable[None]]:
    """Build the producer-side dispatching handler. RETRY → rewrite + re-push;
    MUTATE → propose hypothesis + regenerate + push the new candidates. Owns its
    work on the producer's db + wf (run_retry/run_mutate; node_*_mutate/retry open
    their own r1b_retry_log sessions, so this never shares the producer's db).

    ``max_mutations`` is a hard per-session cap on mutate regenerations (0 = off),
    held in a closure counter — the backstop for the parent=None depth-chain gap."""
    mut_counter = {"n": 0}

    async def handle(event: FeedbackEvent, push, db, wf) -> None:
        if event.kind == FEEDBACK_RETRY:
            await _do_retry(event, push, wf, config)
        elif event.kind == FEEDBACK_MUTATE:
            await _do_mutate(event, push, db, wf, config, mutate_num_alphas,
                             mut_counter, max_mutations)

    return handle
