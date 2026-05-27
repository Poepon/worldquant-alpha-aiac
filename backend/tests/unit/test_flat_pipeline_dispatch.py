"""ENABLE_SIM_PIPELINE flag dispatch in _run_flat_iteration (Unit 2c-step2).

Verifies the flag branch only — that flag ON delegates to the isolated pipeline
implementation and flag OFF does not (the legacy loop runs unchanged). The
pipeline body itself is validated by the component tests + the shadow run.
"""

from types import SimpleNamespace

import pytest

import backend.tasks.mining_tasks as m


class _AsyncDB:
    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


@pytest.mark.asyncio
async def test_flag_on_delegates_to_pipeline(monkeypatch):
    called = {}

    async def fake_pipeline(db, task, run, celery_task_id, *, lock_key, lock_token):
        called["args"] = (task.id, lock_key, lock_token)
        return {"pipeline": True}

    monkeypatch.setattr(m, "_run_flat_iteration_pipeline", fake_pipeline)
    monkeypatch.setattr(m.settings, "ENABLE_SIM_PIPELINE", True)

    res = await m._run_flat_iteration(
        _AsyncDB(), SimpleNamespace(id=7), SimpleNamespace(), "cid",
        lock_key="lk", lock_token="tok",
    )
    assert res == {"pipeline": True}
    assert called["args"] == (7, "lk", "tok")


@pytest.mark.asyncio
async def test_per_task_config_delegates_to_pipeline(monkeypatch):
    """Global flag OFF but task.config['enable_sim_pipeline']=True → pipeline
    (isolated single-session shadow)."""
    called = {}

    async def fake_pipeline(db, task, run, celery_task_id, *, lock_key, lock_token):
        called["yes"] = True
        return {"pipeline": True}

    monkeypatch.setattr(m, "_run_flat_iteration_pipeline", fake_pipeline)
    monkeypatch.setattr(m.settings, "ENABLE_SIM_PIPELINE", False)  # global OFF

    res = await m._run_flat_iteration(
        _AsyncDB(),
        SimpleNamespace(id=7, config={"enable_sim_pipeline": True}),
        SimpleNamespace(), "cid", lock_key="lk", lock_token="tok",
    )
    assert res == {"pipeline": True}
    assert called.get("yes") is True


@pytest.mark.asyncio
async def test_flag_off_runs_legacy_not_pipeline(monkeypatch):
    called = {}

    async def fake_pipeline(*a, **k):
        called["hit"] = True
        return {}

    async def _no_datasets(db, task):
        return []

    monkeypatch.setattr(m, "_run_flat_iteration_pipeline", fake_pipeline)
    monkeypatch.setattr(m, "_get_datasets_to_mine", _no_datasets)
    monkeypatch.setattr(m.settings, "ENABLE_SIM_PIPELINE", False)

    run = SimpleNamespace(status=None, finished_at=None, runtime_state={})
    # target_datasets None → _get_datasets_to_mine → [] → legacy bails early
    # (before BrainAdapter) with the no_datasets warning, never touching pipeline.
    res = await m._run_flat_iteration(
        _AsyncDB(), SimpleNamespace(id=7, target_datasets=None, region="USA"),
        run, "cid", lock_key="lk", lock_token="tok",
    )
    assert "hit" not in called  # pipeline NOT invoked
    assert res.get("warning") == "no_datasets"
    assert run.status == "COMPLETED"


class _FakePdb:
    def __init__(self, task, run):
        self._task, self._run = task, run

    async def get(self, model, _id):
        return self._task if model.__name__ == "MiningTask" else self._run

    async def commit(self):
        return None


class _FakeBrain:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @classmethod
    def _current_sim_slot_limit(cls):
        return 3


@pytest.mark.asyncio
async def test_pipeline_next_round_inputs_smoke(monkeypatch):
    """Run _run_flat_iteration_pipeline far enough to execute next_round_inputs
    once — exercises the (lazy) auth-circuit import, ownership/status guards,
    dataset pick + field prep, and the real-round path (#3). Would have caught
    the wrong BRAIN_AUTH_CIRCUIT import module."""
    async def _datasets(db, task):
        return ["pv1"]

    async def _ops(db):
        return ["rank"]

    async def _fields(db, task, ds):
        return [{"id": "close"}]

    async def _pool(db, task, ds):
        return []

    monkeypatch.setattr(m, "_get_datasets_to_mine", _datasets)
    monkeypatch.setattr(m, "_get_operators", _ops)
    monkeypatch.setattr(m, "_prepare_round_fields", _fields)
    monkeypatch.setattr(m, "_build_dataset_pool", _pool)
    monkeypatch.setattr(m, "_verify_cascade_ownership", lambda *a, **k: True)
    monkeypatch.setattr(m, "BrainAdapter", _FakeBrain)
    monkeypatch.setattr(m.settings, "ENABLE_DATASET_VALUE_BANDIT", False)

    import backend.agents.graph.workflow as wf_mod
    monkeypatch.setattr(wf_mod, "MiningWorkflow", lambda *a, **k: object())

    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    monkeypatch.setattr(BRAIN_AUTH_CIRCUIT, "is_open", lambda: False)

    task = SimpleNamespace(id=7, status="RUNNING", config={}, universe="TOP3000",
                           region="USA", target_datasets=None, daily_goal=0)
    run = SimpleNamespace(id=99, runtime_state={}, status=None, finished_at=None)

    captured = {}

    import backend.agents.pipeline as pipe_mod

    async def _fake_rfps(*, next_round_inputs, **kw):
        captured["inputs"] = await next_round_inputs(_FakePdb(task, run))
        captured["kw"] = kw
        return {"produced": 1, "simulated": 1, "persisted": 1,
                "errors": 0, "slot_timeouts": 0, "persist_failures": 0}

    monkeypatch.setattr(pipe_mod, "run_flat_pipeline_session", _fake_rfps)

    res = await m._run_flat_iteration_pipeline(
        _AsyncDB(), task, run, "cid", lock_key="lk", lock_token="tok",
    )

    # next_round_inputs ran through the auth-circuit import + guards and returned
    # a usable round.
    assert captured["inputs"] is not None
    assert captured["inputs"]["dataset_id"] == "pv1"
    assert captured["inputs"]["fields"] == [{"id": "close"}]
    # No-op slots passed (the #1 deadlock fix) + daily_goal as target.
    assert "acquire_slot" in captured["kw"] and "release_slot" in captured["kw"]
    # Finalization marked COMPLETED (no ownership loss).
    assert run.status == "COMPLETED"
    assert res["pipeline_stats"]["persisted"] == 1


@pytest.mark.asyncio
async def test_pipeline_ownership_loss_closes_run_not_task(monkeypatch):
    """On ownership loss, next_round_inputs returns None and finalization closes
    THIS (superseded) run as STOPPED without marking the task COMPLETED — fixes
    the orphan-RUNNING-run class (run 1196)."""
    async def _datasets(db, task):
        return ["pv1"]

    async def _ops(db):
        return []

    monkeypatch.setattr(m, "_get_datasets_to_mine", _datasets)
    monkeypatch.setattr(m, "_get_operators", _ops)
    monkeypatch.setattr(m, "_verify_cascade_ownership", lambda *a, **k: False)  # lost
    monkeypatch.setattr(m, "BrainAdapter", _FakeBrain)
    monkeypatch.setattr(m.settings, "ENABLE_DATASET_VALUE_BANDIT", False)

    import backend.agents.graph.workflow as wf_mod
    monkeypatch.setattr(wf_mod, "MiningWorkflow", lambda *a, **k: object())
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    monkeypatch.setattr(BRAIN_AUTH_CIRCUIT, "is_open", lambda: False)

    task = SimpleNamespace(id=7, status="RUNNING", config={}, universe="TOP3000",
                           region="USA", target_datasets=None, daily_goal=0)
    run = SimpleNamespace(id=99, runtime_state={}, status="RUNNING", finished_at=None)
    captured = {}

    import backend.agents.pipeline as pipe_mod

    async def _fake_rfps(*, next_round_inputs, **kw):
        captured["inputs"] = await next_round_inputs(_FakePdb(task, run))
        return {"produced": 0, "simulated": 0, "persisted": 0,
                "errors": 0, "slot_timeouts": 0, "persist_failures": 0}

    monkeypatch.setattr(pipe_mod, "run_flat_pipeline_session", _fake_rfps)

    await m._run_flat_iteration_pipeline(
        _AsyncDB(), task, run, "cid", lock_key="lk", lock_token="tok",
    )
    # Producer stopped immediately on lost ownership.
    assert captured["inputs"] is None
    # Superseded run closed; task NOT marked COMPLETED (left for the new owner).
    assert run.status == "STOPPED"
    assert task.status == "RUNNING"
