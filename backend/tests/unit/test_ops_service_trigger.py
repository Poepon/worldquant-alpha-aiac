"""Unit tests for OpsService trigger / throttle / whitelist.

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.6.

We mock the Redis client + celery_app.send_task so the tests run with
no infra. The OpsService.db is required by the constructor but unused in
Phase 1 trigger code, so we pass None — when Phase 2/3 adds page composers
the db fixture will come back.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.services.ops_service import (
    GLOBAL_THROTTLE_LIMIT,
    GLOBAL_THROTTLE_WINDOW_SEC,
    PER_TASK_THROTTLE_SEC,
    GlobalThrottledError,
    OpsService,
    OpsTriggerError,
    PerTaskThrottledError,
    UnknownTaskError,
)


VALID = "backend.tasks.run_alpha_health_check"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis():
    """In-memory stand-in for the subset of redis we exercise."""
    class _Redis:
        def __init__(self):
            self.kv = {}
            self.ttls = {}
            self.deleted = []
            self.incrs = {}

        def set(self, key, value, ex=None, nx=False):
            if nx and key in self.kv:
                return None
            self.kv[key] = value
            if ex is not None:
                self.ttls[key] = ex
            return True

        def ttl(self, key):
            return self.ttls.get(key, -2)

        def delete(self, key):
            self.deleted.append(key)
            self.kv.pop(key, None)
            self.ttls.pop(key, None)
            return 1

        def incr(self, key):
            cur = self.incrs.get(key, 0) + 1
            self.incrs[key] = cur
            return cur

        def expire(self, key, sec):
            self.ttls[key] = sec
            return True

        def keys(self, pattern):
            return []

        def get(self, key):
            return self.kv.get(key)

    return _Redis()


@pytest.fixture
def svc(fake_redis):
    s = OpsService(db=None)  # type: ignore[arg-type]
    # Patch the lazy redis factory so all internal _redis() calls go to the fake
    with patch("backend.services.ops_service.OpsService._redis",
               return_value=fake_redis):
        yield s


@pytest.fixture
def fake_send_task():
    """Patch celery_app.send_task to return a deterministic AsyncResult-shape."""
    sent = []

    def _send(name, kwargs=None, **rest):
        sent.append((name, kwargs))
        m = MagicMock()
        m.id = f"task-{len(sent)}"
        return m

    with patch("backend.celery_app.celery_app") as mock_app:
        mock_app.send_task.side_effect = _send
        yield sent


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_task_raises_before_redis(svc, fake_send_task):
    with pytest.raises(UnknownTaskError):
        await svc.trigger_task("backend.tasks.evil_secret_op")
    assert fake_send_task == []


# ---------------------------------------------------------------------------
# Per-task throttle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_trigger_succeeds(svc, fake_send_task):
    result = await svc.trigger_task(VALID)
    assert result.task_id == "task-1"
    assert result.name == VALID
    assert result.throttle_remaining_sec == PER_TASK_THROTTLE_SEC
    assert fake_send_task == [(VALID, {})]


@pytest.mark.asyncio
async def test_second_trigger_within_window_409s(svc, fake_send_task):
    await svc.trigger_task(VALID)
    with pytest.raises(PerTaskThrottledError):
        await svc.trigger_task(VALID)
    # send_task only called once
    assert len(fake_send_task) == 1


@pytest.mark.asyncio
async def test_different_tasks_have_independent_throttles(svc, fake_send_task):
    await svc.trigger_task(VALID)
    other = "backend.tasks.run_pillar_balance_check"
    result = await svc.trigger_task(other)
    assert result.name == other
    assert len(fake_send_task) == 2


# ---------------------------------------------------------------------------
# send_task failure clears the per-task lock so operator can retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_failure_rolls_back_throttle(svc, fake_redis):
    """If celery_app.send_task raises, the per-task lock must be cleared
    so the operator can retry within the same minute."""
    with patch("backend.celery_app.celery_app") as mock_app:
        mock_app.send_task.side_effect = RuntimeError("broker down")
        with pytest.raises(OpsTriggerError):
            await svc.trigger_task(VALID)

    # Lock was rolled back
    from backend.services.ops_service import _PER_TASK_KEY
    key = _PER_TASK_KEY.format(task=VALID)
    assert key in fake_redis.deleted


# ---------------------------------------------------------------------------
# Global throttle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_throttle_kicks_in_at_limit(fake_redis, fake_send_task):
    """Global limit fires after GLOBAL_THROTTLE_LIMIT triggers within the
    window, regardless of per-task locks.

    We bypass per-task throttling entirely (returning 0 = "free") so the
    global counter alone decides — the per-task case has dedicated tests
    above. This isolates the global limit logic.
    """
    s = OpsService(db=None)  # type: ignore[arg-type]
    rotation = [
        "backend.tasks.run_alpha_health_check",
        "backend.tasks.run_hypothesis_health_check",
        "backend.tasks.run_pillar_balance_check",
        "backend.tasks.run_negative_knowledge_extract",
        "backend.tasks.run_macro_narrative_extract",
        "backend.tasks.run_regime_infer",
        "backend.tasks.monitor_llm_op_hallucinations",
        "backend.tasks.run_daily_feedback",
    ]

    with patch.object(OpsService, "_redis", return_value=fake_redis), \
         patch.object(OpsService, "_check_per_task_throttle", return_value=0):
        for i in range(GLOBAL_THROTTLE_LIMIT):
            await s.trigger_task(rotation[i % len(rotation)])
        # The (limit+1)-th trigger fires GlobalThrottledError
        with pytest.raises(GlobalThrottledError):
            await s.trigger_task(rotation[0])


# ---------------------------------------------------------------------------
# Fail-open on Redis outage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_outage_triggers_still_succeed(fake_send_task):
    """If Redis is completely unavailable, the operator must still be able
    to trigger tasks — the alternative is "ops dashboard locked out by
    flaky Redis", which is worse than missing rate-limit protection."""
    s = OpsService(db=None)  # type: ignore[arg-type]
    with patch("backend.services.ops_service.OpsService._redis",
               side_effect=ConnectionError("redis down")):
        result = await s.trigger_task(VALID)
    assert result.name == VALID


# ---------------------------------------------------------------------------
# Result-backend scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recent_runs_returns_empty_when_redis_down():
    s = OpsService(db=None)  # type: ignore[arg-type]
    with patch("backend.services.ops_service.OpsService._redis",
               side_effect=ConnectionError("redis down")):
        out = await s.list_recent_celery_runs()
    assert out == []
