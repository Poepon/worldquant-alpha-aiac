"""方向1 (2026-05-20): fleet-wide BRAIN re-auth coalescing.

Two processes (modelled as two BrainAdapter instances sharing one fake Redis)
hitting a 401 at the same time must result in exactly ONE authenticate() —
the loser of the Redis lock waits and reuses the session the winner wrote.
This is the fix for the multi-worker mutual-invalidation thrash that collapsed
simulate throughput (3 solo workers + uvicorn sharing one BRAIN session).
"""
from __future__ import annotations

import asyncio

import pytest


class FakeAsyncRedis:
    """Minimal async-redis stand-in backed by a shared dict.

    Supports the surface _distributed_reauth uses: set(nx, ex), get, eval
    (the token-checked release Lua), aclose. TTL is ignored (tests are fast).
    """

    def __init__(self, store: dict):
        self.store = store

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def eval(self, script, numkeys, key, arg):
        # Emulates the release Lua: del iff current value == token.
        if self.store.get(key) == arg:
            self.store.pop(key, None)
            return 1
        return 0

    async def aclose(self):
        pass


def _make_adapter(store, state):
    from backend.adapters.brain_adapter import BrainAdapter

    a = BrainAdapter(email="x@y.com", password="pw")

    async def _get_redis():
        return FakeAsyncRedis(store)

    async def _quiet():
        return bool(state["valid"])

    async def _invalidate():
        state["valid"] = False

    async def _auth():
        # Simulate a real authenticate round-trip so the race window is real.
        await asyncio.sleep(0.05)
        state["auth_calls"] += 1
        state["valid"] = True
        return True

    a._get_redis = _get_redis
    a._reload_and_validate_quietly = _quiet
    a._invalidate_session_cache = _invalidate
    a.authenticate = _auth
    return a


@pytest.fixture(autouse=True)
def _fast_knobs(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "BRAIN_REAUTH_POLL_INTERVAL_SEC", 0.01, raising=False)
    monkeypatch.setattr(settings, "BRAIN_REAUTH_WAIT_TIMEOUT_SEC", 3.0, raising=False)
    monkeypatch.setattr(settings, "BRAIN_REAUTH_LOCK_TTL_SEC", 30, raising=False)


@pytest.mark.asyncio
async def test_two_processes_race_only_one_authenticates():
    store: dict = {}
    state = {"valid": False, "auth_calls": 0}
    a = _make_adapter(store, state)
    b = _make_adapter(store, state)

    results = await asyncio.gather(a._distributed_reauth(), b._distributed_reauth())

    assert results == [True, True]
    # The loser of the Redis lock reused the winner's session.
    assert state["auth_calls"] == 1
    # Lock released (not left dangling).
    assert "brain_auth:reauth_lock" not in store


@pytest.mark.asyncio
async def test_five_concurrent_processes_still_one_authenticate():
    store: dict = {}
    state = {"valid": False, "auth_calls": 0}
    adapters = [_make_adapter(store, state) for _ in range(5)]
    results = await asyncio.gather(*(ad._distributed_reauth() for ad in adapters))
    assert all(results)
    assert state["auth_calls"] == 1


@pytest.mark.asyncio
async def test_redis_unavailable_falls_back_to_plain_authenticate():
    state = {"valid": False, "auth_calls": 0}
    a = _make_adapter({}, state)

    async def _boom():
        raise RuntimeError("redis down")

    a._get_redis = _boom
    ok = await a._distributed_reauth()
    assert ok is True
    assert state["auth_calls"] == 1


@pytest.mark.asyncio
async def test_waiter_times_out_when_lock_stuck(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "BRAIN_REAUTH_WAIT_TIMEOUT_SEC", 0.08, raising=False)

    # Lock pre-held by a foreign holder that never releases + session never valid.
    store = {"brain_auth:reauth_lock": "foreign-holder-token"}
    state = {"valid": False, "auth_calls": 0}
    a = _make_adapter(store, state)

    ok = await a._distributed_reauth()
    # Deferred to circuit/retry rather than stampeding a parallel authenticate.
    assert ok is False
    assert state["auth_calls"] == 0


@pytest.mark.asyncio
async def test_winner_reuses_session_if_predecessor_already_refreshed():
    """If a sibling refreshed between our 401 and acquiring the lock, the lock
    holder reuses that session instead of re-authing."""
    store: dict = {}
    state = {"valid": True, "auth_calls": 0}  # session already valid in Redis
    a = _make_adapter(store, state)
    ok = await a._distributed_reauth()
    assert ok is True
    assert state["auth_calls"] == 0  # no redundant authenticate
