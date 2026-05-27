"""F2-2: R1b retry wired through the pipeline feedback channel.

Two ends of one loop (assembled by ``run_flat_pipeline_session`` /
``_run_flat_iteration_pipeline`` only when ``ENABLE_R1B_RETRY_LOOP`` is on):

- **classifier** (persister-side, sync, DB-free): a FAIL+IMPLEMENTATION
  SimResult with retry budget remaining → a RETRY ``FeedbackEvent``.
- **handler** (producer-side, owns db+wf): rewrites the expression
  (``wf.run_retry`` → node_code_gen_retry), re-validates it, and pushes the
  valid rewrite back onto the work queue as a fresh candidate to re-simulate.

Retry depth rides on the candidate's MiningState
(``r1b_retries_attempted_this_alpha``), which node_code_gen_retry increments and
self-guards at ``R1B_MAX_RETRIES_PER_ALPHA`` — the same cap (and r1b_retry_log
telemetry) as the legacy in-graph cycle, so behaviour can't drift. The depth cap
also bounds feedback fan-out, which the runner's quiescence termination needs.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from backend.agents.pipeline.producer import _sim_ready_payload
from backend.agents.pipeline.types import (
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


def build_retry_classifier(
    *, max_retries: int
) -> Callable[[SimResult], Optional[FeedbackEvent]]:
    """Persister-side classifier (sync, DB-free).

    Emits a RETRY event for a FAIL alpha whose R1a attribution is
    implementation-side and whose per-alpha retry budget is not yet spent.
    Returns None for PASS, sim-infra failures (no attribution), hypothesis-side
    failures (those are mutate, F2-3), and budget-exhausted alphas.
    """

    def classify(result: SimResult) -> Optional[FeedbackEvent]:
        a = _first_pending(getattr(result, "state", None))
        if a is None:
            return None
        if getattr(a, "quality_status", None) != "FAIL":
            return None
        attr = (getattr(a, "metrics", None) or {}).get("_r1a_attribution")
        if attr not in ("implementation", "both"):
            return None
        retries = _sattr(getattr(result, "state", None), "r1b_retries_attempted_this_alpha", 0) or 0
        if retries >= max_retries:
            return None
        return FeedbackEvent(kind=FEEDBACK_RETRY, result=result)

    return classify


def build_retry_handler(
    *, config: Optional[dict] = None
) -> Callable[[FeedbackEvent, Callable[[Candidate], Awaitable[None]], Any, Any], Awaitable[None]]:
    """Producer-side handler.

    Runs ``wf.run_retry`` (code_gen_retry → validate; node_code_gen_retry owns
    its own r1b_retry_log session, so this does not touch the producer's ``db``)
    and pushes the valid rewrite as a fresh candidate. No-op when the rewrite was
    skipped (budget exhausted → alpha stays FAIL) or failed validation.
    """

    async def handle(event: FeedbackEvent, push, db, wf) -> None:
        if event.kind != FEEDBACK_RETRY:
            return
        st = getattr(event.result, "state", None)
        if st is None:
            return
        new_state = await wf.run_retry(st, config=config)
        a = _first_pending(new_state)
        if a is None:
            return
        # node_code_gen_retry sets PENDING on a real rewrite; a skipped/no-op
        # retry leaves quality_status FAIL. validate then sets is_valid — only
        # re-simulate a genuinely rewritten AND valid alpha.
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

    return handle
