"""F2-2/F2-3: unified R1b feedback classifier + dispatching handler.

Focused unit tests for the retry + mutate feedback logic. The runner's cyclic-
termination is proven in test_pipeline_feedback.py; here we test attribution
routing (incl. mutate-dominates-"both" + dedupe), the retry rewrite→validate→push
path, and the mutate propose→regenerate→push path.
"""

from types import SimpleNamespace

import pytest

from backend.agents.pipeline.feedback_r1b import (
    build_feedback_classifier,
    build_feedback_handler,
)
from backend.agents.pipeline.types import (
    FEEDBACK_MUTATE,
    FEEDBACK_RETRY,
    Candidate,
    FeedbackEvent,
    SimResult,
)


def _result(*, quality="FAIL", attr="implementation", retries=0, hyp="h1",
            has_pending=True, has_state=True):
    metrics = {"_r1a_attribution": attr} if attr is not None else {}
    a = SimpleNamespace(quality_status=quality, metrics=metrics, hypothesis=hyp)
    if not has_state:
        state = None
    else:
        state = SimpleNamespace(
            pending_alphas=[a] if has_pending else [],
            r1b_retries_attempted_this_alpha=retries,
        )
    return SimResult(candidate=Candidate("expr", {"dataset_id": "pv1"}),
                     ok=(quality != "FAIL"), state=state, verdict=quality)


# --------------------------------------------------------------------------- #
# Classifier — retry routing                                                   #
# --------------------------------------------------------------------------- #
def test_classify_retry_implementation_within_budget():
    classify = build_feedback_classifier(retry_on=True, mutate_on=False, max_retries=3)
    assert classify(_result(attr="implementation", retries=0)).kind == FEEDBACK_RETRY
    # mutate OFF → "both" falls through to retry
    assert classify(_result(attr="both", retries=2)).kind == FEEDBACK_RETRY


def test_classify_retry_budget_exhausted():
    classify = build_feedback_classifier(retry_on=True, mutate_on=False, max_retries=3)
    assert classify(_result(attr="implementation", retries=3)) is None
    assert classify(_result(attr="implementation", retries=4)) is None


def test_classify_non_feedback_cases():
    classify = build_feedback_classifier(retry_on=True, mutate_on=False, max_retries=3)
    assert classify(_result(attr=None)) is None                 # no attribution
    assert classify(_result(quality="PASS", attr="implementation")) is None
    assert classify(_result(has_state=False)) is None
    assert classify(_result(has_pending=False)) is None
    # hypothesis-side failure with mutate OFF → nothing (not a retry case)
    assert classify(_result(attr="hypothesis")) is None


# --------------------------------------------------------------------------- #
# Classifier — mutate routing + dominance + dedupe                             #
# --------------------------------------------------------------------------- #
def test_classify_mutate_hypothesis():
    classify = build_feedback_classifier(retry_on=False, mutate_on=True, max_retries=3)
    ev = classify(_result(attr="hypothesis", hyp="momentum"))
    assert ev is not None and ev.kind == FEEDBACK_MUTATE


def test_classify_mutate_dominates_both():
    classify = build_feedback_classifier(retry_on=True, mutate_on=True, max_retries=3)
    # "both" → MUTATE (dominates retry) when mutate is on
    assert classify(_result(attr="both", hyp="h-both")).kind == FEEDBACK_MUTATE
    # pure implementation still → RETRY
    assert classify(_result(attr="implementation", hyp="h-impl")).kind == FEEDBACK_RETRY


def test_classify_mutate_dedupes_by_hypothesis():
    classify = build_feedback_classifier(retry_on=False, mutate_on=True, max_retries=3)
    first = classify(_result(attr="hypothesis", hyp="same-hyp"))
    second = classify(_result(attr="hypothesis", hyp="same-hyp"))
    assert first is not None and first.kind == FEEDBACK_MUTATE
    assert second is None                       # same hypothesis → deduped
    # a different hypothesis still mutates
    assert classify(_result(attr="hypothesis", hyp="other-hyp")).kind == FEEDBACK_MUTATE


def test_classify_mutate_empty_hypothesis_text_skipped():
    classify = build_feedback_classifier(retry_on=False, mutate_on=True, max_retries=3)
    assert classify(_result(attr="hypothesis", hyp="")) is None


# --------------------------------------------------------------------------- #
# Handler — retry path                                                         #
# --------------------------------------------------------------------------- #
class _RetryWF:
    def __init__(self, ret_state):
        self._ret = ret_state
        self.calls = 0

    async def run_retry(self, state, config=None):
        self.calls += 1
        return self._ret


def _retry_event():
    fail_state = SimpleNamespace(pending_alphas=[SimpleNamespace(quality_status="FAIL")])
    res = SimResult(candidate=Candidate("orig", {"dataset_id": "pv1"}),
                    state=fail_state, verdict="FAIL")
    return FeedbackEvent(kind=FEEDBACK_RETRY, result=res)


def _rewritten_state(quality="PENDING", valid=True, expr="rewritten"):
    a = SimpleNamespace(quality_status=quality, is_valid=valid, expression=expr, metrics={})
    return {"pending_alphas": [a], "trace_steps": [{"step_type": "CODE_GEN"}]}


@pytest.mark.asyncio
async def test_handle_retry_valid_rewrite_pushes():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _RetryWF(_rewritten_state(quality="PENDING", valid=True, expr="rank(x)"))
    handle = build_feedback_handler(config={"configurable": {"run_id": 5}})
    await handle(_retry_event(), push, db=None, wf=wf)
    assert wf.calls == 1 and len(pushed) == 1
    assert pushed[0].expression == "rank(x)"
    assert pushed[0].context.get("r1b_retry") is True


@pytest.mark.asyncio
async def test_handle_retry_no_rewrite_or_invalid_no_push():
    pushed = []

    async def push(c):
        pushed.append(c)

    handle = build_feedback_handler()
    await handle(_retry_event(), push, db=None, wf=_RetryWF(_rewritten_state(quality="FAIL")))
    await handle(_retry_event(), push, db=None,
                 wf=_RetryWF(_rewritten_state(quality="PENDING", valid=False)))
    assert pushed == []


# --------------------------------------------------------------------------- #
# Handler — mutate path                                                        #
# --------------------------------------------------------------------------- #
class _MutateWF:
    def __init__(self, mut_state, gen_result):
        self._mut = mut_state
        self._gen = gen_result
        self.run_kwargs = None
        self.mutate_called = 0

    async def run_mutate(self, state, config=None):
        self.mutate_called += 1
        return self._mut

    async def run(self, **kwargs):
        self.run_kwargs = kwargs
        return self._gen


class _FakeDB:
    def __init__(self, task):
        self._task = task

    async def get(self, model, _id):
        return self._task


def _mutate_event(hyp="momentum on revisions"):
    a = SimpleNamespace(quality_status="FAIL", hypothesis=hyp,
                        metrics={"_r1a_attribution": "hypothesis"})
    st = SimpleNamespace(pending_alphas=[a], task_id=7, dataset_id="pv1",
                         fields=[{"id": "close"}], operators=["rank"])
    res = SimResult(candidate=Candidate("orig", {"dataset_id": "pv1"}),
                    state=st, verdict="FAIL")
    return FeedbackEvent(kind=FEEDBACK_MUTATE, result=res)


@pytest.mark.asyncio
async def test_handle_mutate_generates_and_pushes():
    pushed = []

    async def push(c):
        pushed.append(c)

    mut_state = {"r1b_pending_new_hypothesis": {"statement": "revised hyp", "hypothesis_id": 42}}
    gen_result = {
        "pending_alphas": [SimpleNamespace(expression="rank(new)")],
        "trace_steps": [{"step_type": "CODE_GEN"}],
        "state": {"dataset_id": "pv1"},
    }
    wf = _MutateWF(mut_state, gen_result)
    task = SimpleNamespace(id=7, config={})
    handle = build_feedback_handler(
        config={"configurable": {"run_id": 5}}, mutate_num_alphas=2)
    await handle(_mutate_event(), push, db=_FakeDB(task), wf=wf)

    assert wf.mutate_called == 1
    # generation driven with the mutated hypothesis injected via task.config
    assert task.config.get("__r1b_consumed_pending_hypothesis", {}).get("statement") == "revised hyp"
    assert wf.run_kwargs["generate_only"] is True
    assert wf.run_kwargs["num_alphas"] == 2
    assert len(pushed) == 1
    assert pushed[0].expression == "rank(new)"
    assert pushed[0].context.get("r1b_mutate") is True


@pytest.mark.asyncio
async def test_handle_mutate_noop_when_no_new_hypothesis():
    """Depth/budget/cross-pillar no-op → run_mutate returns no statement →
    no regeneration, no push."""
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _MutateWF({"r1b_pending_new_hypothesis": None}, {"pending_alphas": []})
    handle = build_feedback_handler()
    await handle(_mutate_event(), push, db=_FakeDB(SimpleNamespace(id=7, config={})), wf=wf)
    assert wf.mutate_called == 1
    assert wf.run_kwargs is None      # generation never driven
    assert pushed == []


@pytest.mark.asyncio
async def test_handle_mutate_missing_task_no_push():
    pushed = []

    async def push(c):
        pushed.append(c)

    mut_state = {"r1b_pending_new_hypothesis": {"statement": "x", "hypothesis_id": 1}}
    wf = _MutateWF(mut_state, {"pending_alphas": []})
    handle = build_feedback_handler()
    await handle(_mutate_event(), push, db=_FakeDB(None), wf=wf)   # task not found
    assert wf.run_kwargs is None and pushed == []


@pytest.mark.asyncio
async def test_handle_ignores_unknown_kind():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _MutateWF({"r1b_pending_new_hypothesis": {"statement": "x"}}, {"pending_alphas": []})
    handle = build_feedback_handler()
    await handle(FeedbackEvent(kind="SOMETHING_ELSE", result=_mutate_event().result),
                 push, db=_FakeDB(None), wf=wf)
    assert wf.mutate_called == 0 and pushed == []
