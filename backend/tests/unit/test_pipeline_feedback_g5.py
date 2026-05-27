"""F2-4: G5 crossover classifier + handler.

Unit tests for the PASS→PASS_LANDED classifier and the producer-side crossover
handler (pair selection, dedupe, max-crossover cap, offspring validate+push),
with the LLM call + the G5 log write faked.
"""

from types import SimpleNamespace

import pytest

import backend.agents.pipeline.feedback_g5 as g5mod
from backend.agents.pipeline.feedback_g5 import build_g5_classifier, build_g5_handler
from backend.agents.pipeline.types import (
    FEEDBACK_PASS_LANDED,
    FEEDBACK_RETRY,
    Candidate,
    FeedbackEvent,
    SimResult,
)


# --------------------------------------------------------------------------- #
# Classifier                                                                   #
# --------------------------------------------------------------------------- #
def _result(verdict):
    return SimResult(candidate=Candidate("e", {}), ok=(verdict != "FAIL"),
                     state={"task_id": 7, "region": "USA"}, verdict=verdict)


def test_g5_classifier_pass_emits_pass_landed():
    classify = build_g5_classifier()
    assert classify(_result("PASS")).kind == FEEDBACK_PASS_LANDED
    assert classify(_result("PASS_PROVISIONAL")).kind == FEEDBACK_PASS_LANDED
    assert classify(_result("FAIL")) is None
    assert classify(_result("OPTIMIZE")) is None


# --------------------------------------------------------------------------- #
# Handler fakes                                                                #
# --------------------------------------------------------------------------- #
def _alpha(aid, sharpe, hyp_id=None, expr=None):
    return SimpleNamespace(id=aid, is_sharpe=sharpe, is_fitness=1.0, is_turnover=0.2,
                           expression=expr or f"expr_{aid}", hypothesis_id=hyp_id,
                           task_id=7, region="USA")


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _Res(self._rows)


class _FakeWF:
    def __init__(self, *, valid=True):
        self.llm_service = SimpleNamespace(model="x")
        self._valid = valid
        self.validate_calls = 0

    async def run_validate(self, state, config=None):
        self.validate_calls += 1
        pend = state.get("pending_alphas") if isinstance(state, dict) else getattr(state, "pending_alphas", [])
        for a in (pend or []):
            a.is_valid = self._valid
        return state


def _pass_event():
    st = {"task_id": 7, "region": "USA", "dataset_id": "pv1"}
    return FeedbackEvent(kind=FEEDBACK_PASS_LANDED,
                         result=SimResult(candidate=Candidate("p", {"dataset_id": "pv1"}),
                                          ok=True, state=st, verdict="PASS"))


@pytest.fixture(autouse=True)
def _patch_llm_and_log(monkeypatch):
    async def _fake_crossover(*a, **k):
        return [{"expression": "rank(combined)", "combination_strategy": "weighted_sum",
                 "rationale": "blend"}]

    async def _noop_log(*a, **k):
        return None

    import backend.agents.llm_crossover_alpha as _xmod
    monkeypatch.setattr(_xmod, "llm_crossover_alpha", _fake_crossover)
    monkeypatch.setattr(g5mod, "_write_g5_log", _noop_log)
    return _fake_crossover


# --------------------------------------------------------------------------- #
# Handler tests                                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_handle_g5_crosses_pair_and_pushes_offspring():
    pushed = []

    async def push(c):
        pushed.append(c)

    rows = [(_alpha(1, 2.0, hyp_id=10), SimpleNamespace(pillar="momentum")),
            (_alpha(2, 1.8, hyp_id=20), SimpleNamespace(pillar="value"))]
    wf = _FakeWF(valid=True)
    handle = build_g5_handler(run_id=99, require_diff_pillar=True, max_crossovers=20)
    await handle(_pass_event(), push, db=_FakeDB(rows), wf=wf)

    assert wf.validate_calls == 1
    assert len(pushed) == 1
    assert pushed[0].expression == "rank(combined)"
    assert pushed[0].context.get("g5_offspring") is True


@pytest.mark.asyncio
async def test_handle_g5_fewer_than_two_pass_no_op():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF()
    handle = build_g5_handler(run_id=99)
    await handle(_pass_event(), push, db=_FakeDB([(_alpha(1, 2.0), None)]), wf=wf)
    assert pushed == [] and wf.validate_calls == 0


@pytest.mark.asyncio
async def test_handle_g5_dedupes_same_pair():
    pushed = []

    async def push(c):
        pushed.append(c)

    rows = [(_alpha(1, 2.0, hyp_id=10), SimpleNamespace(pillar="momentum")),
            (_alpha(2, 1.8, hyp_id=20), SimpleNamespace(pillar="value"))]
    wf = _FakeWF()
    handle = build_g5_handler(run_id=99, max_crossovers=20)
    await handle(_pass_event(), push, db=_FakeDB(rows), wf=wf)
    await handle(_pass_event(), push, db=_FakeDB(rows), wf=wf)   # same only pair
    assert len(pushed) == 1            # second call deduped (pair already crossed)


@pytest.mark.asyncio
async def test_handle_g5_max_crossovers_cap():
    pushed = []

    async def push(c):
        pushed.append(c)

    rows = [(_alpha(1, 2.0, hyp_id=10), None), (_alpha(2, 1.8, hyp_id=20), None)]
    wf = _FakeWF()
    handle = build_g5_handler(run_id=99, require_diff_pillar=False, max_crossovers=0)
    await handle(_pass_event(), push, db=_FakeDB(rows), wf=wf)
    assert pushed == [] and wf.validate_calls == 0   # cap 0 → no crossover


@pytest.mark.asyncio
async def test_handle_g5_invalid_offspring_not_pushed():
    pushed = []

    async def push(c):
        pushed.append(c)

    rows = [(_alpha(1, 2.0, hyp_id=10), None), (_alpha(2, 1.8, hyp_id=20), None)]
    wf = _FakeWF(valid=False)             # validate marks offspring invalid
    handle = build_g5_handler(run_id=99, require_diff_pillar=False)
    await handle(_pass_event(), push, db=_FakeDB(rows), wf=wf)
    assert wf.validate_calls == 1 and pushed == []


@pytest.mark.asyncio
async def test_handle_g5_pushes_only_stamped_offspring():
    """Only validated alphas carrying _g5_crossover_parent_ids are re-simulated —
    a non-offspring left in pending_alphas (state-copy edge / validate hook) is
    NOT pushed (else it would re-simulate an already-PASSED parent)."""
    pushed = []

    async def push(c):
        pushed.append(c)

    class _WFExtra:
        def __init__(self):
            self.llm_service = SimpleNamespace(model="x")
            self.validate_calls = 0

        async def run_validate(self, state, config=None):
            self.validate_calls += 1
            pend = state["pending_alphas"] if isinstance(state, dict) else state.pending_alphas
            for a in pend:
                a.is_valid = True
            # inject a non-G5 valid alpha (no _g5 stamp) — must be filtered out
            pend.append(SimpleNamespace(expression="not_offspring", is_valid=True, metrics={}))
            return state

    rows = [(_alpha(1, 2.0, hyp_id=10), None), (_alpha(2, 1.8, hyp_id=20), None)]
    handle = build_g5_handler(run_id=99, require_diff_pillar=False)
    await handle(_pass_event(), push, db=_FakeDB(rows), wf=_WFExtra())
    assert len(pushed) == 1                       # only the stamped offspring
    assert pushed[0].expression == "rank(combined)"


@pytest.mark.asyncio
async def test_handle_g5_ignores_non_pass_landed():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF()
    handle = build_g5_handler(run_id=99)
    ev = FeedbackEvent(kind=FEEDBACK_RETRY, result=_pass_event().result)
    await handle(ev, push, db=_FakeDB([]), wf=wf)
    assert pushed == [] and wf.validate_calls == 0
