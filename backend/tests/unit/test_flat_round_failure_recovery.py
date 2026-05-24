"""FLAT session round-failure recovery (2026-05-25).

Root cause (task 3504): a per-round ``asyncio.wait_for`` timeout cancelled a DB
op mid-flight → SQLAlchemy raised ``greenlet_spawn has not been called`` and the
shared AsyncSession was permanently poisoned → the WHOLE FLAT session died and
the cursor after that round was lost.

Fix: ``_run_flat_iteration`` rebuilds a clean session on round failure (via
``_rebuild_flat_db_session``) and continues, bailing out gracefully only after
``FLAT_MAX_CONSECUTIVE_ROUND_FAILURES`` failures in a row (cursor preserved).

These are real-ORM tests (in-memory aiosqlite) — they exercise the actual
``new_db.get(...)`` re-fetch and DB read-back, not mocks of the session.
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.tasks import mining_tasks
from backend.models import MiningTask, ExperimentRun


def _maker(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_rebuild_returns_clean_session_with_task_and_run(async_engine, monkeypatch):
    maker = _maker(async_engine)
    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", maker)

    old = maker()
    task = MiningTask(
        task_name="t", region="USA", universe="TOP3000", schedule="FLAT", config={}
    )
    old.add(task)
    await old.flush()
    run = ExperimentRun(
        task_id=task.id, status="RUNNING", runtime_state={"flat_cursor": 2}
    )
    old.add(run)
    await old.commit()
    tid, rid = task.id, run.id

    new_db, new_task, new_run, agent = await mining_tasks._rebuild_flat_db_session(
        old, tid, rid, None
    )
    try:
        assert new_db is not old
        assert new_task is not None and new_task.id == tid
        assert new_run is not None and new_run.id == rid
        # runtime_state survives the re-fetch into the fresh session
        assert new_run.runtime_state.get("flat_cursor") == 2
        assert agent is None  # brain=None → no MiningAgent built (finalization path)
        # the new session is actually usable (real DB round-trip)
        await new_db.refresh(new_task)
        assert new_task.task_name == "t"
    finally:
        await new_db.close()


@pytest.mark.asyncio
async def test_rebuild_works_when_old_session_already_closed(async_engine, monkeypatch):
    """A poisoned session may fail both rollback and close — the helper must
    still return a usable clean session and re-fetch task/run."""
    maker = _maker(async_engine)
    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", maker)

    seed = maker()
    task = MiningTask(
        task_name="t2", region="USA", universe="TOP3000", schedule="FLAT", config={}
    )
    seed.add(task)
    await seed.flush()
    run = ExperimentRun(task_id=task.id, status="RUNNING", runtime_state={})
    seed.add(run)
    await seed.commit()
    tid, rid = task.id, run.id
    await seed.close()  # simulate a dead session (rollback/close are no-ops/fail)

    new_db, new_task, new_run, _ = await mining_tasks._rebuild_flat_db_session(
        seed, tid, rid, None
    )
    try:
        assert new_task.id == tid and new_run.id == rid
    finally:
        await new_db.close()


@pytest.mark.asyncio
async def test_flat_loop_bails_out_after_consecutive_failures(async_engine, monkeypatch):
    """A round that keeps returning an error must NOT raise out / crash the
    session: the loop rebuilds, advances the cursor, and exits cleanly after
    exactly FLAT_MAX_CONSECUTIVE_ROUND_FAILURES attempts."""
    maker = _maker(async_engine)
    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", maker)
    monkeypatch.setattr(
        mining_tasks.settings, "FLAT_MAX_CONSECUTIVE_ROUND_FAILURES", 3, raising=False
    )
    monkeypatch.setattr(
        mining_tasks.settings, "ENABLE_DATASET_VALUE_BANDIT", False, raising=False
    )
    monkeypatch.setattr(
        mining_tasks.settings, "FLAT_CONTINUOUS_MAX_ITERATIONS", 50, raising=False
    )

    seed = maker()
    task = MiningTask(
        task_name="loop", region="USA", universe="TOP3000", schedule="FLAT",
        target_datasets=["ds1", "ds2"], config={}, status="RUNNING", daily_goal=4,
    )
    seed.add(task)
    await seed.flush()
    run = ExperimentRun(task_id=task.id, status="RUNNING", runtime_state={})
    seed.add(run)
    await seed.commit()
    tid, rid = task.id, run.id

    db = maker()
    task = await db.get(MiningTask, tid)
    run = await db.get(ExperimentRun, rid)

    class _FakeBrain:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeAgent:
        def __init__(self, *a, **k):
            pass

    async def _fake_ops(_db):
        return []

    calls = {"n": 0}

    async def _always_fail(*a, **k):
        calls["n"] += 1
        return {"all_alphas": [], "error": "simulated greenlet_spawn"}

    monkeypatch.setattr(mining_tasks, "BrainAdapter", _FakeBrain)
    monkeypatch.setattr(mining_tasks, "MiningAgent", _FakeAgent)
    monkeypatch.setattr(mining_tasks, "_get_operators", _fake_ops)
    monkeypatch.setattr(mining_tasks, "_verify_cascade_ownership", lambda *a, **k: True)
    monkeypatch.setattr(mining_tasks, "_run_one_round_inline", _always_fail)

    # Must NOT raise (pre-fix: poisoned session propagated greenlet_spawn).
    result = await mining_tasks._run_flat_iteration(
        db, task, run, "celery-1", lock_key="lk", lock_token="tok"
    )

    assert calls["n"] == 3, "should bail out after exactly 3 consecutive failures"
    assert result["mode"] == "FLAT_CONTINUOUS"
    # cursor advanced past each failed slot so a resume skips them
    assert result["final_cursor"] == 3
    assert result["total_alphas"] == 0
    await db.close()


@pytest.mark.asyncio
async def test_flat_loop_recovers_and_continues_after_transient_failure(async_engine, monkeypatch):
    """A transient round failure (one slow/timed-out round) must reset the
    consecutive counter once a later round succeeds — the session keeps mining
    instead of dying."""
    maker = _maker(async_engine)
    monkeypatch.setattr(mining_tasks, "AsyncSessionLocal", maker)
    monkeypatch.setattr(
        mining_tasks.settings, "FLAT_MAX_CONSECUTIVE_ROUND_FAILURES", 3, raising=False
    )
    monkeypatch.setattr(
        mining_tasks.settings, "ENABLE_DATASET_VALUE_BANDIT", False, raising=False
    )
    monkeypatch.setattr(
        mining_tasks.settings, "FLAT_CONTINUOUS_MAX_ITERATIONS", 50, raising=False
    )
    monkeypatch.setattr(
        mining_tasks.settings, "FLAT_CONTINUOUS_DAILY_GOAL", 4, raising=False
    )

    seed = maker()
    task = MiningTask(
        task_name="recover", region="USA", universe="TOP3000", schedule="FLAT",
        target_datasets=["ds1"], config={}, status="RUNNING", daily_goal=4,
    )
    seed.add(task)
    await seed.flush()
    run = ExperimentRun(task_id=task.id, status="RUNNING", runtime_state={})
    seed.add(run)
    await seed.commit()
    tid, rid = task.id, run.id

    db = maker()
    task = await db.get(MiningTask, tid)
    run = await db.get(ExperimentRun, rid)

    class _FakeBrain:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeAgent:
        def __init__(self, *a, **k):
            pass

    async def _fake_ops(_db):
        return []

    # fail once, then succeed and produce 4 alphas → daily_goal reached → exit.
    seq = iter([
        {"all_alphas": [], "error": "transient timeout"},
        {"all_alphas": [object(), object(), object(), object()]},
    ])

    async def _round(*a, **k):
        try:
            return next(seq)
        except StopIteration:
            return {"all_alphas": []}

    monkeypatch.setattr(mining_tasks, "BrainAdapter", _FakeBrain)
    monkeypatch.setattr(mining_tasks, "MiningAgent", _FakeAgent)
    monkeypatch.setattr(mining_tasks, "_get_operators", _fake_ops)
    monkeypatch.setattr(mining_tasks, "_verify_cascade_ownership", lambda *a, **k: True)
    monkeypatch.setattr(mining_tasks, "_run_one_round_inline", _round)

    result = await mining_tasks._run_flat_iteration(
        db, task, run, "celery-1", lock_key="lk", lock_token="tok"
    )

    # one failed round (cursor +1) then one good round of 4 alphas
    assert result["total_alphas"] == 4
    assert result["final_cursor"] == 2  # failed slot + successful slot
    await db.close()
