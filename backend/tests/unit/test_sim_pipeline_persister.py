"""Unit tests for the pipeline persister stage (Sub-phase 0 / Unit 2b)."""

from types import SimpleNamespace

import pytest

from backend.agents.pipeline import SimResult, build_persister


def _state(**kw):
    base = dict(
        task_id=42, region="USA", universe="TOP3000", dataset_id="pv1",
        pending_alphas=[object()], current_hypothesis_id=7,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _result(state, ok=True):
    return SimResult(candidate=SimpleNamespace(), ok=ok, state=state)


class _RecordingSave:
    def __init__(self, returns=1, raise_on=None):
        self.calls = []
        self.returns = returns
        self.raise_on = raise_on  # dataset_id to raise on

    async def __call__(self, session, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on is not None and kwargs.get("dataset_id") == self.raise_on:
            raise RuntimeError("save boom")
        # Return `returns` AlphaResult stand-ins.
        return [object()] * self.returns


class _RecordingFailures:
    def __init__(self):
        self.calls = []

    async def __call__(self, session, **kwargs):
        self.calls.append(kwargs)
        return 0


async def _noop_failures(session, **kwargs):
    return 0


class _RecordingFlush:
    def __init__(self):
        self.calls = []

    async def __call__(self, session, task_id, run_id, iteration, steps):
        self.calls.append({"task_id": task_id, "run_id": run_id,
                           "iteration": iteration, "steps": list(steps)})
        return len(steps)


async def _noop_flush(session, task_id, run_id, iteration, steps):
    return 0


@pytest.mark.asyncio
async def test_persist_calls_save_per_result_with_context():
    save = _RecordingSave(returns=1)
    persist = build_persister(run_id=99, save_fn=save, save_failures_fn=_noop_failures)
    session = object()

    n = await persist(session, [_result(_state()), _result(_state(dataset_id="anl4"))])

    assert n == 2
    assert len(save.calls) == 2
    c0 = save.calls[0]
    assert c0["task_id"] == 42
    assert c0["run_id"] == 99
    assert c0["region"] == "USA"
    assert c0["universe"] == "TOP3000"
    assert c0["dataset_id"] == "pv1"
    assert c0["hypothesis_id"] == 7
    assert len(c0["pending_alphas"]) == 1
    assert save.calls[1]["dataset_id"] == "anl4"


@pytest.mark.asyncio
async def test_persist_counts_saved_alpha_rows():
    save = _RecordingSave(returns=3)  # each call persists 3 rows
    persist = build_persister(run_id=1, save_fn=save, save_failures_fn=_noop_failures)
    n = await persist(object(), [_result(_state()), _result(_state())])
    assert n == 6


@pytest.mark.asyncio
async def test_persist_skips_none_state_and_empty_pending():
    save = _RecordingSave()
    persist = build_persister(run_id=1, save_fn=save, save_failures_fn=_noop_failures)
    results = [
        SimResult(candidate=SimpleNamespace(), ok=False, state=None),       # slot timeout
        _result(_state(pending_alphas=[])),                                  # nothing to save
        _result(_state()),                                                   # real
    ]
    n = await persist(object(), results)
    assert n == 1
    assert len(save.calls) == 1  # only the real one reached save_fn


@pytest.mark.asyncio
async def test_persist_hypothesis_id_falls_back_to_list():
    save = _RecordingSave()
    persist = build_persister(run_id=1, save_fn=save, save_failures_fn=_noop_failures)
    st = _state(current_hypothesis_id=None, current_hypothesis_ids=[11, 12])
    await persist(object(), [_result(st)])
    assert save.calls[0]["hypothesis_id"] == 11


@pytest.mark.asyncio
async def test_persist_one_failure_does_not_drop_batch():
    save = _RecordingSave(returns=1, raise_on="bad")
    persist = build_persister(run_id=1, save_fn=save, save_failures_fn=_noop_failures)
    results = [
        _result(_state(dataset_id="good1")),
        _result(_state(dataset_id="bad")),    # save raises
        _result(_state(dataset_id="good2")),
    ]
    n = await persist(object(), results)
    # The two good ones still persisted; the failing one is skipped, not fatal.
    assert n == 2
    assert len(save.calls) == 3


@pytest.mark.asyncio
async def test_persist_also_writes_failure_log_per_result():
    save = _RecordingSave(returns=1)
    fails = _RecordingFailures()
    persist = build_persister(run_id=55, save_fn=save, save_failures_fn=fails)
    st = _state(rag_ab_arm="armB")
    await persist(object(), [_result(st), _result(_state(dataset_id="anl4"))])

    # Failure log written once per result, with run_id + resolved hypothesis_id
    # + rag_ab_arm + the pending list (it classifies non-PASS internally).
    assert len(fails.calls) == 2
    f0 = fails.calls[0]
    assert f0["task_id"] == 42
    assert f0["run_id"] == 55
    assert f0["hypothesis_id"] == 7
    assert f0["rag_ab_arm"] == "armB"
    assert "pending_alphas" in f0
    # rag_ab_arm absent on the 2nd state → None.
    assert fails.calls[1]["rag_ab_arm"] is None


@pytest.mark.asyncio
async def test_failure_log_error_does_not_block_pass_persist():
    save = _RecordingSave(returns=2)

    async def boom_failures(session, **kwargs):
        raise RuntimeError("alpha_failures down")

    persist = build_persister(run_id=1, save_fn=save, save_failures_fn=boom_failures)
    n = await persist(object(), [_result(_state())])
    # PASS persistence still succeeds even if the failure-log write blows up.
    assert n == 2


@pytest.mark.asyncio
async def test_persist_flushes_trace_one_iteration_per_candidate():
    flush = _RecordingFlush()
    persist = build_persister(
        run_id=77, save_fn=_RecordingSave(), save_failures_fn=_noop_failures,
        flush_trace_fn=flush,
    )

    def _res(gen_steps, sim_steps):
        cand = SimpleNamespace(trace_records=gen_steps, payload=_state())
        return SimResult(candidate=cand, ok=True, state=_state(), trace_records=sim_steps)

    r1 = _res([{"step_type": "RAG_QUERY"}, {"step_type": "CODE_GEN"}],
              [{"step_type": "SIMULATE"}, {"step_type": "EVALUATE"}])
    r2 = _res([{"step_type": "RAG_QUERY"}], [{"step_type": "SIMULATE"}])
    await persist(object(), [r1, r2])

    assert len(flush.calls) == 2
    # Candidate 1 = iteration 1, full trajectory gen + sim/eval (combined order).
    assert flush.calls[0]["iteration"] == 1
    assert flush.calls[0]["task_id"] == 42 and flush.calls[0]["run_id"] == 77
    assert [s["step_type"] for s in flush.calls[0]["steps"]] == \
        ["RAG_QUERY", "CODE_GEN", "SIMULATE", "EVALUATE"]
    # Candidate 2 = iteration 2 (per-candidate, monotonic).
    assert flush.calls[1]["iteration"] == 2
    assert [s["step_type"] for s in flush.calls[1]["steps"]] == ["RAG_QUERY", "SIMULATE"]


@pytest.mark.asyncio
async def test_trace_flush_error_does_not_block_alpha_persist():
    save = _RecordingSave(returns=2)

    async def boom_flush(session, task_id, run_id, iteration, steps):
        raise RuntimeError("trace_steps down")

    persist = build_persister(
        run_id=1, save_fn=save, save_failures_fn=_noop_failures, flush_trace_fn=boom_flush,
    )
    cand = SimpleNamespace(trace_records=[{"step_type": "RAG_QUERY"}], payload=_state())
    r = SimResult(candidate=cand, ok=True, state=_state(), trace_records=[{"step_type": "SIMULATE"}])
    n = await persist(object(), [r])
    assert n == 2  # alpha persist unaffected by trace flush blowing up


@pytest.mark.asyncio
async def test_persist_skips_trace_when_no_records():
    flush = _RecordingFlush()
    persist = build_persister(
        run_id=1, save_fn=_RecordingSave(), save_failures_fn=_noop_failures, flush_trace_fn=flush,
    )
    # _state()'s candidate has no trace_records and SimResult none → nothing to flush.
    await persist(object(), [_result(_state())])
    assert flush.calls == []


@pytest.mark.asyncio
async def test_default_save_fns_bind_real_node_persistence():
    # Smoke: with no injected fns it binds the real node persistence fns
    # (no call made — empty results).
    persist = build_persister(run_id=1)
    n = await persist(object(), [])
    assert n == 0


def test_classify_alpha_failure_taxonomy():
    from backend.agents.graph.state import AlphaCandidate
    from backend.agents.graph.nodes.persistence import _classify_alpha_failure

    def _c(**kw):
        return _classify_alpha_failure(AlphaCandidate(expression="x", **kw), persist_fail=False)

    # Not recorded: PASS / PROVISIONAL / retryable.
    assert _c(quality_status="PASS") is None
    assert _c(quality_status="PASS_PROVISIONAL") is None
    assert _c(quality_status="FAIL", metrics={"_sim_retryable": True}) is None
    # Classified failures.
    assert _c(is_valid=False, validation_error="bad syntax")[0] == "SYNTAX_ERROR"
    assert _c(is_simulated=True, simulation_success=False,
              simulation_error="brain 400")[0] == "SIMULATION_ERROR"
    assert _c(quality_status="FAIL")[0] == "QUALITY_CHECK_FAILED"
    assert _c(metrics={"_pre_brain_skip": True, "_skip_kind": "dedup"})[0] == "DEDUP_SKIP"
    assert _c(metrics={"_pre_brain_skip": True})[0] == "PRESIM_SKIP"
