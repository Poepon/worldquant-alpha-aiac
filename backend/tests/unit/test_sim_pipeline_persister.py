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


@pytest.mark.asyncio
async def test_persist_calls_save_per_result_with_context():
    save = _RecordingSave(returns=1)
    persist = build_persister(run_id=99, save_fn=save)
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
    persist = build_persister(run_id=1, save_fn=save)
    n = await persist(object(), [_result(_state()), _result(_state())])
    assert n == 6


@pytest.mark.asyncio
async def test_persist_skips_none_state_and_empty_pending():
    save = _RecordingSave()
    persist = build_persister(run_id=1, save_fn=save)
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
    persist = build_persister(run_id=1, save_fn=save)
    st = _state(current_hypothesis_id=None, current_hypothesis_ids=[11, 12])
    await persist(object(), [_result(st)])
    assert save.calls[0]["hypothesis_id"] == 11


@pytest.mark.asyncio
async def test_persist_one_failure_does_not_drop_batch():
    save = _RecordingSave(returns=1, raise_on="bad")
    persist = build_persister(run_id=1, save_fn=save)
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
async def test_default_save_fn_is_incremental_save_alphas():
    # Smoke: with no injected save_fn it binds the real node persistence fn
    # (no call made — empty results).
    persist = build_persister(run_id=1)
    n = await persist(object(), [])
    assert n == 0
