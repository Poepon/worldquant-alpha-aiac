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
