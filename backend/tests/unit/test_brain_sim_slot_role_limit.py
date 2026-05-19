"""Unit tests for role-aware BRAIN concurrent simulation slot limit.

Verifies that ``BrainAdapter._current_sim_slot_limit`` and the surrounding
``_acquire_sim_slot`` loop honour the live ``ENABLE_BRAIN_CONSULTANT_MODE``
flag — flips via ops dashboard (``_flag_override_cache``) take effect on the
very next acquire, no restart. USER stays at 3 in-flight, CONSULTANT lifts to
80 to use the full server-side ceiling.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.adapters.brain_adapter import BrainAdapter
from backend.config import _flag_override_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def test_current_sim_slot_limit_user_default():
    """Flag absent → USER ceiling (3)."""
    assert BrainAdapter._current_sim_slot_limit() == 3


def test_current_sim_slot_limit_consultant_when_flag_on():
    """Flag ON → CONSULTANT ceiling (80) to use the full BRAIN allotment."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    assert BrainAdapter._current_sim_slot_limit() == 80


def test_current_sim_slot_limit_flips_immediately_on_ops_toggle():
    """An ops flip (cache mutation) is observable on the very next call —
    no restart, no cached read. This is the property that makes the limit
    safe to flip back to USER mid-CONSULTANT-session: the next acquire
    re-clamps to 3 instead of letting the existing 4..80 in-flight sims
    overshoot the USER ceiling."""
    assert BrainAdapter._current_sim_slot_limit() == 3
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    assert BrainAdapter._current_sim_slot_limit() == 80
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = False
    assert BrainAdapter._current_sim_slot_limit() == 3


def test_current_sim_slot_limit_falls_back_to_user_on_settings_error():
    """settings access blows up → degrade to the more restrictive USER
    ceiling. Better to under-utilise BRAIN than to push past the User
    ceiling and earn 429 CONCURRENT_SIMULATION_LIMIT_EXCEEDED."""
    with patch(
        "backend.adapters.brain_adapter.settings",
        side_effect=RuntimeError("boom"),
    ):
        # getattr on the patched object still works, but we want the except
        # path — simulate by patching to a sentinel that raises on attribute
        # access.
        class _Boom:
            def __getattribute__(self, name):  # noqa: D401
                raise RuntimeError("settings broken")

        with patch("backend.adapters.brain_adapter.settings", new=_Boom()):
            assert BrainAdapter._current_sim_slot_limit() == 3


class _FakeRedis:
    """Minimal incr/decr/expire stand-in — counter persists across calls so
    we can observe the limit's effect across acquire/release pairs."""

    def __init__(self):
        self.counter = 0

    async def incr(self, _key):
        self.counter += 1
        return self.counter

    async def decr(self, _key):
        self.counter -= 1
        return self.counter

    async def expire(self, _key, _ttl):
        return True

    async def set(self, _key, value):
        self.counter = value
        return True


@pytest.mark.asyncio
async def test_acquire_sim_slot_user_mode_blocks_after_three():
    """USER mode: 3 acquires succeed, the 4th hits over-capacity (decr + wait).
    We collapse the wait by setting a tiny timeout so the 4th returns False
    instead of looping for 30 min."""
    fake = _FakeRedis()
    with (
        patch.object(BrainAdapter, "_get_slot_redis", new=AsyncMock(return_value=fake)),
        patch.object(BrainAdapter, "_SLOT_ACQUIRE_TIMEOUT", 0.05),
        patch.object(BrainAdapter, "_SLOT_POLL_INTERVAL", 0.01),
    ):
        for _ in range(3):
            assert await BrainAdapter._acquire_sim_slot() is True
        # 4th: over-capacity → False after the timeout window
        assert await BrainAdapter._acquire_sim_slot() is False

    # Counter must be back at 3 (the 4th acquire's incr was paired with a decr)
    assert fake.counter == 3


@pytest.mark.asyncio
async def test_acquire_sim_slot_consultant_mode_allows_eighty():
    """CONSULTANT mode: 80 acquires succeed, the 81st hits over-capacity."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    fake = _FakeRedis()
    with (
        patch.object(BrainAdapter, "_get_slot_redis", new=AsyncMock(return_value=fake)),
        patch.object(BrainAdapter, "_SLOT_ACQUIRE_TIMEOUT", 0.05),
        patch.object(BrainAdapter, "_SLOT_POLL_INTERVAL", 0.01),
    ):
        for _ in range(80):
            assert await BrainAdapter._acquire_sim_slot() is True
        # 81st: over-capacity even with Consultant ceiling
        assert await BrainAdapter._acquire_sim_slot() is False

    assert fake.counter == 80


@pytest.mark.asyncio
async def test_acquire_sim_slot_flip_to_consultant_unblocks_waiter():
    """USER session pinned at 3 in-flight → ops flips to CONSULTANT mid-run
    → the next acquire goes through immediately at the new 80 ceiling
    without releasing the 3 USER-era slots first.

    This is the property that justifies *not* gating the limit through a
    task-snapshot (à la sharpe_submit_min): expansion is always safe."""
    fake = _FakeRedis()
    with (
        patch.object(BrainAdapter, "_get_slot_redis", new=AsyncMock(return_value=fake)),
        patch.object(BrainAdapter, "_SLOT_ACQUIRE_TIMEOUT", 0.05),
        patch.object(BrainAdapter, "_SLOT_POLL_INTERVAL", 0.01),
    ):
        # 3 USER acquires fill the ceiling
        for _ in range(3):
            assert await BrainAdapter._acquire_sim_slot() is True
        # 4th would block — confirm
        assert await BrainAdapter._acquire_sim_slot() is False
        assert fake.counter == 3

        # Ops flips to CONSULTANT — next acquire goes straight through
        _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
        assert await BrainAdapter._acquire_sim_slot() is True
        assert fake.counter == 4
