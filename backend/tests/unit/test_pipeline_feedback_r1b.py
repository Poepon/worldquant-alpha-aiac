"""F2-2: R1b retry classify (persister-side) + handle (producer-side).

Focused unit tests for the retry feedback logic. The runner's cyclic-termination
with a multi-hop retry loop is already proven in test_pipeline_feedback.py; here
we test the attribution/budget gating and the rewrite→validate→push handler.
"""

from types import SimpleNamespace

import pytest

from backend.agents.pipeline.feedback_r1b import (
    build_retry_classifier,
    build_retry_handler,
)
from backend.agents.pipeline.types import (
    FEEDBACK_MUTATE,
    FEEDBACK_RETRY,
    Candidate,
    FeedbackEvent,
    SimResult,
)


# --------------------------------------------------------------------------- #
# Classifier                                                                   #
# --------------------------------------------------------------------------- #
def _result(*, quality="FAIL", attr="implementation", retries=0, has_pending=True,
            has_state=True):
    metrics = {"_r1a_attribution": attr} if attr is not None else {}
    a = SimpleNamespace(quality_status=quality, metrics=metrics)
    if not has_state:
        state = None
    else:
        state = SimpleNamespace(
            pending_alphas=[a] if has_pending else [],
            r1b_retries_attempted_this_alpha=retries,
        )
    return SimResult(candidate=Candidate("expr", {"dataset_id": "pv1"}),
                     ok=(quality != "FAIL"), state=state, verdict=quality)


def test_classify_fail_implementation_within_budget_emits_retry():
    classify = build_retry_classifier(max_retries=3)
    ev = classify(_result(quality="FAIL", attr="implementation", retries=0))
    assert ev is not None and ev.kind == FEEDBACK_RETRY
    # "both" attribution also retries (implementation component present).
    assert classify(_result(attr="both", retries=2)) is not None


def test_classify_budget_exhausted_returns_none():
    classify = build_retry_classifier(max_retries=3)
    assert classify(_result(attr="implementation", retries=3)) is None
    assert classify(_result(attr="implementation", retries=4)) is None


def test_classify_non_retry_cases_return_none():
    classify = build_retry_classifier(max_retries=3)
    # hypothesis-side failure → mutate's job (F2-3), not retry
    assert classify(_result(attr="hypothesis")) is None
    # no attribution at all (e.g. sim-infra failure)
    assert classify(_result(attr=None)) is None
    # not a quality FAIL (PASS / PROVISIONAL)
    assert classify(_result(quality="PASS", attr="implementation")) is None
    # no state / no pending alpha
    assert classify(_result(has_state=False)) is None
    assert classify(_result(has_pending=False)) is None


# --------------------------------------------------------------------------- #
# Handler                                                                      #
# --------------------------------------------------------------------------- #
class _FakeWF:
    def __init__(self, ret_state):
        self._ret = ret_state
        self.calls = []

    async def run_retry(self, state, config=None):
        self.calls.append((state, config))
        return self._ret


def _retry_event(dataset_id="pv1"):
    fail_state = SimpleNamespace(pending_alphas=[SimpleNamespace(quality_status="FAIL")])
    res = SimResult(candidate=Candidate("orig", {"dataset_id": dataset_id}),
                    ok=True, state=fail_state, verdict="FAIL")
    return FeedbackEvent(kind=FEEDBACK_RETRY, result=res)


def _rewritten_state(quality="PENDING", valid=True, expr="rewritten"):
    a = SimpleNamespace(quality_status=quality, is_valid=valid, expression=expr,
                        metrics={})
    return {"pending_alphas": [a], "trace_steps": [{"step_type": "CODE_GEN"}]}


@pytest.mark.asyncio
async def test_handle_valid_rewrite_pushes_fresh_candidate():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(_rewritten_state(quality="PENDING", valid=True, expr="rank(x)"))
    handle = build_retry_handler(config={"configurable": {"run_id": 5}})
    await handle(_retry_event("pv1"), push, db=None, wf=wf)

    assert len(wf.calls) == 1                          # run_retry invoked
    assert len(pushed) == 1
    cand = pushed[0]
    assert cand.expression == "rank(x)"
    assert cand.context["dataset_id"] == "pv1"         # carried from the parent
    assert cand.context.get("r1b_retry") is True
    assert cand.trace_records                          # rewrite trace carried


@pytest.mark.asyncio
async def test_handle_no_rewrite_does_not_push():
    """Budget-exhausted / no-op retry leaves the alpha FAIL → no re-sim."""
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(_rewritten_state(quality="FAIL", valid=True))
    handle = build_retry_handler()
    await handle(_retry_event(), push, db=None, wf=wf)
    assert pushed == []


@pytest.mark.asyncio
async def test_handle_invalid_rewrite_does_not_push():
    """A rewrite that fails validation is not re-simulated."""
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(_rewritten_state(quality="PENDING", valid=False))
    handle = build_retry_handler()
    await handle(_retry_event(), push, db=None, wf=wf)
    assert pushed == []


@pytest.mark.asyncio
async def test_handle_ignores_non_retry_event():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(_rewritten_state())
    handle = build_retry_handler()
    await handle(FeedbackEvent(kind=FEEDBACK_MUTATE, result=_retry_event().result),
                 push, db=None, wf=wf)
    assert wf.calls == []   # never ran retry
    assert pushed == []


@pytest.mark.asyncio
async def test_handle_none_state_no_push():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(_rewritten_state())
    handle = build_retry_handler()
    ev = FeedbackEvent(kind=FEEDBACK_RETRY,
                       result=SimResult(candidate=Candidate("o", {}), state=None))
    await handle(ev, push, db=None, wf=wf)
    assert wf.calls == [] and pushed == []
