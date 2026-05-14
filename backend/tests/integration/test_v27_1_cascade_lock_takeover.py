"""V-27.1 — cascade lock takeover + round-boundary ownership self-check.

RCA: docs/rca_2026-05-14_v27_1_cascade_lock_race.md — the watchdog's
force_clear + re-acquire let a falsely-presumed-dead worker keep running
alongside its replacement (double-run, burns BRAIN quota, concurrent writes).
Fix: structured lock value + atomic takeover + the cascade main loop
self-checks ownership at every round boundary and exits gracefully if it
has been taken over.

Test layout:
  - TestLockValueCodec / TestAcquireRelease / TestTakeover /
    TestVerifyOwnership — lock primitives against a REAL Redis (test-prefixed
    keys, torn down per test).
  - TestCascadeOwnershipSelfCheck — _verify_cascade_ownership decisions
    against real Redis state, incl. the UNKNOWN safety floor + flag-off.
  - TestWatchdogTakeover — _redispatch_task against the real PostgreSQL DB:
    cascade revive uses atomic takeover (not force_clear), threads the token
    via config_snapshot, and discrete revive never touches a lock.

The full run_mining_task claim path (worker reads config_snapshot token →
verify_lock_ownership → OWNED/MISSING/LOST/UNKNOWN branches) is covered at
the invariant level here (a freshly-handed token verifies OWNED) and
end-to-end by the cascade smoke run — driving the whole celery task with a
mocked BrainAdapter/MiningAgent is out of scope for this suite.

Run:
    pytest backend/tests/integration/test_v27_1_cascade_lock_takeover.py -v

Requires: a reachable Redis (REDIS_URL); TestWatchdogTakeover additionally
needs PostgreSQL on POSTGRES_PORT (5433).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

# TestWatchdogTakeover targets the real PG DB.
os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.tasks.redis_pool import (  # noqa: E402
    _encode_lock_value,
    _decode_lock_value,
    acquire_cascade_lock,
    release_cascade_lock,
    takeover_cascade_lock,
    verify_lock_ownership,
    peek_lock_holder,
    get_redis_client,
)


@pytest.fixture
def lock_key():
    """A unique test lock key, deleted on teardown."""
    key = f"cascade_lock:test:{uuid.uuid4().hex}"
    yield key
    try:
        get_redis_client().delete(key)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lock value codec — v2 JSON ⇄ dict, plus legacy (pre-V-27.1) compatibility
# ---------------------------------------------------------------------------

class TestLockValueCodec:
    def test_encode_decode_roundtrip(self):
        v = _encode_lock_value("tok1", run_id=7, worker_pid=123, lineage="WORKER")
        d = _decode_lock_value(v)
        assert d["token"] == "tok1"
        assert d["run_id"] == 7
        assert d["worker_pid"] == 123
        assert d["v"] == 2
        assert d["lineage"] == "WORKER"
        assert "acquired_at" in d

    def test_decode_legacy_plain_string(self):
        d = _decode_lock_value("celery-abc-123")
        assert d == {"token": "celery-abc-123", "v": 1, "lineage": "LEGACY"}

    def test_decode_legacy_bytes(self):
        d = _decode_lock_value(b"celery-abc-123")
        assert d["token"] == "celery-abc-123"
        assert d["v"] == 1

    def test_decode_none(self):
        assert _decode_lock_value(None) is None

    def test_decode_json_without_token_is_legacy(self):
        # A JSON blob that isn't our shape → treated wholesale as a legacy
        # token so it can never silently lose CAS identity.
        raw = '{"foo": 1}'
        d = _decode_lock_value(raw)
        assert d["v"] == 1
        assert d["token"] == raw


# ---------------------------------------------------------------------------
# acquire / release — SET NX EX + Lua CAS, v2 and legacy values
# ---------------------------------------------------------------------------

class TestAcquireRelease:
    def test_acquire_stores_v2_value(self, lock_key):
        assert acquire_cascade_lock(lock_key, "tok-a", 60, run_id=1, worker_pid=9)
        holder = peek_lock_holder(lock_key)
        assert holder["token"] == "tok-a"
        assert holder["v"] == 2
        assert holder["run_id"] == 1

    def test_acquire_nx_blocks_second(self, lock_key):
        assert acquire_cascade_lock(lock_key, "tok-a", 60)
        assert not acquire_cascade_lock(lock_key, "tok-b", 60)

    def test_release_cas_match(self, lock_key):
        acquire_cascade_lock(lock_key, "tok-a", 60)
        assert release_cascade_lock(lock_key, "tok-a") is True
        assert get_redis_client().get(lock_key) is None

    def test_release_cas_mismatch_is_noop(self, lock_key):
        acquire_cascade_lock(lock_key, "tok-a", 60)
        assert release_cascade_lock(lock_key, "tok-WRONG") is False
        assert get_redis_client().get(lock_key) is not None

    def test_release_legacy_value(self, lock_key):
        # A pre-V-27.1 worker stored a bare-string token; the new Lua must
        # still CAS-match and release it.
        get_redis_client().set(lock_key, "legacy-token", ex=60)
        assert release_cascade_lock(lock_key, "legacy-token") is True
        assert get_redis_client().get(lock_key) is None


# ---------------------------------------------------------------------------
# takeover — atomic overwrite regardless of prior holder / format
# ---------------------------------------------------------------------------

class TestTakeover:
    def test_takeover_overwrites_v2(self, lock_key):
        acquire_cascade_lock(lock_key, "old-tok", 60, run_id=1)
        res = takeover_cascade_lock(lock_key, "new-tok", 60, run_id=2)
        assert res["ok"] is True
        assert res["created"] is False
        assert res["prev"]["token"] == "old-tok"
        holder = peek_lock_holder(lock_key)
        assert holder["token"] == "new-tok"
        assert holder["lineage"] == "WATCHDOG_TAKEOVER"

    def test_takeover_overwrites_legacy(self, lock_key):
        get_redis_client().set(lock_key, "legacy-token", ex=60)
        res = takeover_cascade_lock(lock_key, "new-tok", 60)
        assert res["ok"] is True
        assert res["prev"]["token"] == "legacy-token"
        assert peek_lock_holder(lock_key)["token"] == "new-tok"

    def test_takeover_missing_key_created(self, lock_key):
        res = takeover_cascade_lock(lock_key, "new-tok", 60)
        assert res["ok"] is True
        assert res["created"] is True
        assert res["prev"] is None
        assert peek_lock_holder(lock_key)["token"] == "new-tok"

    def test_takeover_then_old_release_is_noop(self, lock_key):
        # RCA core: the old (falsely-presumed-dead) worker eventually runs
        # its finally:release with its STALE token — that must NOT delete the
        # replacement's lock.
        acquire_cascade_lock(lock_key, "old-tok", 60)
        takeover_cascade_lock(lock_key, "new-tok", 60)
        assert release_cascade_lock(lock_key, "old-tok") is False
        assert peek_lock_holder(lock_key)["token"] == "new-tok"
        # the rightful new owner can still release it
        assert release_cascade_lock(lock_key, "new-tok") is True

    def test_takeover_resets_ttl(self, lock_key):
        acquire_cascade_lock(lock_key, "old-tok", 5)
        takeover_cascade_lock(lock_key, "new-tok", 600)
        assert get_redis_client().ttl(lock_key) > 100


# ---------------------------------------------------------------------------
# verify_lock_ownership — OWNED / LOST / MISSING / UNKNOWN
# ---------------------------------------------------------------------------

class TestVerifyOwnership:
    def test_owned(self, lock_key):
        acquire_cascade_lock(lock_key, "tok-a", 60)
        assert verify_lock_ownership(lock_key, "tok-a") == "OWNED"

    def test_lost_after_takeover(self, lock_key):
        acquire_cascade_lock(lock_key, "tok-a", 60)
        takeover_cascade_lock(lock_key, "tok-b", 60)
        assert verify_lock_ownership(lock_key, "tok-a") == "LOST"
        assert verify_lock_ownership(lock_key, "tok-b") == "OWNED"

    def test_missing(self, lock_key):
        assert verify_lock_ownership(lock_key, "tok-a") == "MISSING"

    def test_owned_legacy_value(self, lock_key):
        get_redis_client().set(lock_key, "legacy-token", ex=60)
        assert verify_lock_ownership(lock_key, "legacy-token") == "OWNED"

    def test_unknown_on_redis_error(self, lock_key, monkeypatch):
        # SAFETY FLOOR: a Redis failure must surface as UNKNOWN, never LOST.
        import backend.tasks.redis_pool as rp

        class _BoomClient:
            def get(self, *a, **k):
                raise ConnectionError("redis down")

        monkeypatch.setattr(rp, "get_redis_client", lambda: _BoomClient())
        assert verify_lock_ownership(lock_key, "tok-a") == "UNKNOWN"


# ---------------------------------------------------------------------------
# _verify_cascade_ownership — the main-loop self-check decision
# ---------------------------------------------------------------------------

class TestCascadeOwnershipSelfCheck:
    def test_owned_returns_true(self, lock_key):
        from backend.tasks.mining_tasks import _verify_cascade_ownership
        acquire_cascade_lock(lock_key, "tok-a", 60)
        assert _verify_cascade_ownership(lock_key, "tok-a", where="t") is True

    def test_lost_returns_false(self, lock_key):
        from backend.tasks.mining_tasks import _verify_cascade_ownership
        acquire_cascade_lock(lock_key, "tok-a", 60)
        takeover_cascade_lock(lock_key, "tok-b", 60)
        assert _verify_cascade_ownership(lock_key, "tok-a", where="t") is False

    def test_missing_returns_false(self, lock_key):
        from backend.tasks.mining_tasks import _verify_cascade_ownership
        assert _verify_cascade_ownership(lock_key, "tok-a", where="t") is False

    def test_unknown_returns_true(self, lock_key, monkeypatch):
        # SAFETY FLOOR: a Redis blip must NOT make a live worker self-exit.
        from backend.tasks.mining_tasks import _verify_cascade_ownership
        import backend.tasks.redis_pool as rp
        monkeypatch.setattr(rp, "verify_lock_ownership", lambda k, t: "UNKNOWN")
        assert _verify_cascade_ownership(lock_key, "tok-a", where="t") is True

    def test_flag_off_is_noop(self, lock_key, monkeypatch):
        # Flag off → self-check always returns True, even on a LOST lock.
        from backend.tasks.mining_tasks import _verify_cascade_ownership
        from backend.config import settings
        monkeypatch.setattr(settings, "CASCADE_LOCK_TAKEOVER_ENABLED", False)
        acquire_cascade_lock(lock_key, "tok-a", 60)
        takeover_cascade_lock(lock_key, "tok-b", 60)
        assert _verify_cascade_ownership(lock_key, "tok-a", where="t") is True


# ---------------------------------------------------------------------------
# Watchdog takeover — _redispatch_task against the real PG DB
# ---------------------------------------------------------------------------

TEST_PREFIX = f"v27test-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_session():
    """Real-PG session per test. Cleans up TEST_PREFIX rows after."""
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from backend.config import settings
    from backend.models import Alpha, ExperimentRun, MiningTask

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        yield db
        async with Session() as cleanup:
            tasks = (
                await cleanup.execute(
                    select(MiningTask.id).where(
                        MiningTask.task_name.like(f"{TEST_PREFIX}%")
                    )
                )
            ).scalars().all()
            if tasks:
                await cleanup.execute(delete(Alpha).where(Alpha.task_id.in_(tasks)))
                await cleanup.execute(
                    delete(ExperimentRun).where(ExperimentRun.task_id.in_(tasks))
                )
                await cleanup.execute(
                    delete(MiningTask).where(MiningTask.id.in_(tasks))
                )
                await cleanup.commit()
    await engine.dispose()


def _make_task(pg_session, name_suffix, mining_mode, region):
    # A test-only region so we never collide with a real production
    # CONTINUOUS_CASCADE task via ix_mining_tasks_active_cascade_per_region.
    from backend.models import MiningTask
    return MiningTask(
        task_name=f"{TEST_PREFIX}-{name_suffix}",
        region=region,
        universe="TOP3000",
        dataset_strategy="AUTO",
        target_datasets=[],
        agent_mode="AUTONOMOUS",
        mining_mode=mining_mode,
        status="RUNNING",
        daily_goal=4,
        progress_current=0,
        current_iteration=0,
        max_iterations=10,
        config={},
    )


class _FakeCeleryResult:
    id = "fake-celery-id"


def _patch_delay(monkeypatch, dispatched):
    """Mock run_mining_task.delay so _redispatch_task doesn't really enqueue."""
    import backend.tasks as _bt

    def _fake_delay(task_id, run_id):
        dispatched["task_id"] = task_id
        dispatched["run_id"] = run_id
        return _FakeCeleryResult()

    monkeypatch.setattr(_bt.run_mining_task, "delay", _fake_delay)


class TestWatchdogTakeover:
    @pytest.mark.asyncio
    async def test_cascade_redispatch_uses_takeover(
        self, pg_session, monkeypatch
    ):
        from backend.models import ExperimentRun
        from backend.tasks import session_watchdog

        task = _make_task(pg_session, "cascade", "CONTINUOUS_CASCADE", region="ZZ1")
        pg_session.add(task)
        await pg_session.commit()
        await pg_session.refresh(task)

        lock_key = f"cascade_lock:task:{task.id}"
        # An old (presumed-dead) worker still holds the lock.
        acquire_cascade_lock(lock_key, "dead-worker-token", 60)

        dispatched: dict = {}
        _patch_delay(monkeypatch, dispatched)

        revived: list = []
        await session_watchdog._redispatch_task(
            pg_session,
            task,
            datetime.now(timezone.utc),
            reason_payload={"kind": "CONTINUOUS_CASCADE"},
            revived=revived,
        )
        assert revived, "redispatch should have succeeded"

        # Lock was TAKEN OVER (still present, new token) — not force_cleared.
        holder = peek_lock_holder(lock_key)
        assert holder is not None, "takeover must leave a lock in place"
        assert holder["token"].startswith("watchdog-takeover:")
        assert holder["lineage"] == "WATCHDOG_TAKEOVER"

        # The new run carries the takeover token in config_snapshot so the
        # fresh worker can claim it.
        run = await pg_session.get(ExperimentRun, dispatched["run_id"])
        assert run.config_snapshot.get("cascade_lock_token") == holder["token"]
        # Invariant of the worker claim path: that handed token verifies OWNED.
        assert verify_lock_ownership(lock_key, holder["token"]) == "OWNED"

        get_redis_client().delete(lock_key)

    @pytest.mark.asyncio
    async def test_cascade_redispatch_flag_off_uses_force_clear(
        self, pg_session, monkeypatch
    ):
        from backend.config import settings
        from backend.models import ExperimentRun
        from backend.tasks import session_watchdog

        monkeypatch.setattr(settings, "CASCADE_LOCK_TAKEOVER_ENABLED", False)

        task = _make_task(
            pg_session, "cascade-flagoff", "CONTINUOUS_CASCADE", region="ZZ2"
        )
        pg_session.add(task)
        await pg_session.commit()
        await pg_session.refresh(task)

        lock_key = f"cascade_lock:task:{task.id}"
        acquire_cascade_lock(lock_key, "dead-worker-token", 60)

        dispatched: dict = {}
        _patch_delay(monkeypatch, dispatched)

        revived: list = []
        await session_watchdog._redispatch_task(
            pg_session,
            task,
            datetime.now(timezone.utc),
            reason_payload={"kind": "CONTINUOUS_CASCADE"},
            revived=revived,
        )
        assert revived, "redispatch should have succeeded"

        # Flag off → legacy force_clear path: lock deleted, no token threaded.
        assert get_redis_client().get(lock_key) is None
        run = await pg_session.get(ExperimentRun, dispatched["run_id"])
        assert "cascade_lock_token" not in (run.config_snapshot or {})

    @pytest.mark.asyncio
    async def test_discrete_redispatch_does_not_takeover(
        self, pg_session, monkeypatch
    ):
        from backend.models import ExperimentRun
        from backend.tasks import session_watchdog

        task = _make_task(pg_session, "discrete", "DISCRETE", region="ZZ3")
        pg_session.add(task)
        await pg_session.commit()
        await pg_session.refresh(task)

        lock_key = f"cascade_lock:task:{task.id}"
        get_redis_client().delete(lock_key)  # discrete tasks hold no lock

        dispatched: dict = {}
        _patch_delay(monkeypatch, dispatched)

        revived: list = []
        await session_watchdog._redispatch_task(
            pg_session,
            task,
            datetime.now(timezone.utc),
            reason_payload={"kind": "DISCRETE"},
            revived=revived,
        )
        assert revived, "redispatch should have succeeded"

        # Discrete revive must NOT create a zombie takeover lock.
        assert get_redis_client().get(lock_key) is None
        run = await pg_session.get(ExperimentRun, dispatched["run_id"])
        assert "cascade_lock_token" not in (run.config_snapshot or {})
