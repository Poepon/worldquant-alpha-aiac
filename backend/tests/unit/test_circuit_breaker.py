"""Unit tests for backend.circuit_breaker (A+ pattern, 2026-05-19).

Coverage:
  - State transitions: CLOSED → trip → OPEN → TTL elapsed → HALF_OPEN
  - clear() takes circuit back to CLOSED
  - is_open() correct for all 3 states
  - trip() idempotent (trip_count++ + TTL refresh)
  - status().to_dict() shape
  - Redis unavailable → fail-open (is_open returns False)
  - Redis exception in trip() → no exception bubbles
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from backend.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    CLOSED_STATUS,
)


@pytest.fixture
def _fake_redis():
    """Build a minimal Redis-shaped dict-backed mock with SET (ex=...) + GET + DELETE."""

    class _R:
        def __init__(self):
            self.store: dict = {}
            self.ttls: dict = {}
            self.set_at: dict = {}

        def get(self, key):
            ts = self.set_at.get(key)
            ttl = self.ttls.get(key)
            if ts is not None and ttl is not None:
                if time.time() - ts >= ttl:
                    # TTL expired — auto-delete
                    self.store.pop(key, None)
                    self.ttls.pop(key, None)
                    self.set_at.pop(key, None)
                    return None
            v = self.store.get(key)
            return v.encode("utf-8") if isinstance(v, str) else v

        def set(self, key, value, ex=None):
            self.store[key] = value
            if ex is not None:
                self.ttls[key] = int(ex)
                self.set_at[key] = time.time()
            return True

        def delete(self, key):
            self.store.pop(key, None)
            self.ttls.pop(key, None)
            self.set_at.pop(key, None)
            return 1

        def force_expire(self, key):
            """Test helper: artificially backdate the SET time so TTL has elapsed."""
            if key in self.set_at:
                self.set_at[key] = 0

    return _R()


@pytest.fixture
def _patched_redis(_fake_redis):
    """Patch backend.tasks.redis_pool.get_redis_client to return our fake."""
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=_fake_redis,
    ):
        yield _fake_redis


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_init_rejects_empty_name():
    with pytest.raises(ValueError):
        CircuitBreaker("")
    with pytest.raises(ValueError):
        CircuitBreaker(None)  # type: ignore[arg-type]


def test_init_sets_redis_key():
    cb = CircuitBreaker("test_thing")
    assert cb._redis_key == "circuit:test_thing"
    assert cb.default_ttl_sec == 300


# ---------------------------------------------------------------------------
# Initial state — CLOSED
# ---------------------------------------------------------------------------


def test_initial_state_is_closed(_patched_redis):
    cb = CircuitBreaker("test_a")
    assert cb.is_open() is False
    s = cb.status()
    assert s.state == CircuitState.CLOSED
    assert s.until_ts is None
    assert s.trip_count == 0


# ---------------------------------------------------------------------------
# trip() — CLOSED → OPEN
# ---------------------------------------------------------------------------


def test_trip_opens_circuit(_patched_redis):
    cb = CircuitBreaker("test_b", default_ttl_sec=60)
    cb.trip(reason="upstream_500")
    assert cb.is_open() is True
    s = cb.status()
    assert s.state == CircuitState.OPEN
    assert s.last_failure_reason == "upstream_500"
    assert s.trip_count == 1
    assert s.until_ts is not None
    assert s.until_ts > time.time()


def test_trip_idempotent_increments_trip_count(_patched_redis):
    cb = CircuitBreaker("test_c")
    cb.trip(reason="r1")
    cb.trip(reason="r2")
    cb.trip(reason="r3")
    s = cb.status()
    assert s.trip_count == 3
    # Latest reason wins
    assert s.last_failure_reason == "r3"


def test_trip_custom_ttl(_patched_redis):
    cb = CircuitBreaker("test_d", default_ttl_sec=300)
    cb.trip(reason="x", ttl_sec=10)
    s = cb.status()
    assert s.until_ts is not None
    # ttl=10 means until_ts within ~10s of now
    assert 8 <= s.until_ts - time.time() <= 11


# ---------------------------------------------------------------------------
# OPEN → HALF_OPEN on TTL elapse
# ---------------------------------------------------------------------------


def test_ttl_elapse_promotes_to_half_open(_patched_redis):
    cb = CircuitBreaker("test_e", default_ttl_sec=60)
    cb.trip(reason="t")
    assert cb.is_open() is True

    # Artificially backdate to simulate TTL elapse
    _patched_redis.force_expire(cb._redis_key)

    # Now Redis returns None on get (TTL deleted), state=CLOSED
    # Note: HALF_OPEN promotion only fires when JSON still present but
    # until_ts past. With Redis's actual TTL deletion the state goes to
    # CLOSED directly. Verify both code paths:
    assert cb.is_open() is False
    assert cb.status().state == CircuitState.CLOSED


def test_ttl_logical_elapse_without_redis_eviction_promotes_half_open(_patched_redis):
    """Stash a manual OPEN row with past until_ts (simulates the brief
    window between TTL elapse and Redis async key eviction)."""
    cb = CircuitBreaker("test_f")
    # Directly write OPEN with until_ts in past
    past = time.time() - 10
    _patched_redis.set(
        cb._redis_key,
        json.dumps({
            "state": "open",
            "until_ts": past,
            "last_failure_at": past - 5,
            "last_failure_reason": "manual",
            "trip_count": 1,
        }),
        ex=600,
    )
    s = cb.status()
    assert s.state == CircuitState.HALF_OPEN
    # is_open() = False in HALF_OPEN (one probe allowed)
    assert cb.is_open() is False


# ---------------------------------------------------------------------------
# clear() — back to CLOSED
# ---------------------------------------------------------------------------


def test_clear_closes_circuit(_patched_redis):
    cb = CircuitBreaker("test_g")
    cb.trip(reason="t")
    assert cb.is_open() is True
    cb.clear(reason="ops_manual")
    assert cb.is_open() is False
    assert cb.status().state == CircuitState.CLOSED


def test_clear_when_already_closed_noop(_patched_redis):
    cb = CircuitBreaker("test_h")
    cb.clear()  # should not raise
    assert cb.is_open() is False


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


def test_status_to_dict_shape(_patched_redis):
    cb = CircuitBreaker("test_i", default_ttl_sec=120)
    cb.trip(reason="api_down")
    d = cb.status().to_dict()
    assert d["state"] == "open"
    assert d["last_failure_reason"] == "api_down"
    assert d["trip_count"] == 1
    assert d["until_ts"] is not None
    assert d["until_iso"] is not None
    assert d["last_failure_at"] is not None
    assert d["last_failure_iso"] is not None
    assert 100 <= d["seconds_until_half_open"] <= 121


def test_status_to_dict_closed():
    """Verify CLOSED_STATUS serializes cleanly (used by /ops endpoint when
    Redis is down)."""
    d = CLOSED_STATUS.to_dict()
    assert d["state"] == "closed"
    assert d["until_ts"] is None
    assert d["until_iso"] is None
    assert d["last_failure_at"] is None
    assert d["last_failure_reason"] is None
    assert d["trip_count"] == 0
    assert d["seconds_until_half_open"] == 0


# ---------------------------------------------------------------------------
# Fail-open semantics on Redis errors
# ---------------------------------------------------------------------------


def test_is_open_fail_open_when_redis_get_raises():
    cb = CircuitBreaker("test_j")
    bad_redis = MagicMock()
    bad_redis.get = MagicMock(side_effect=RuntimeError("redis down"))
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=bad_redis,
    ):
        # Must NOT raise; must default to CLOSED (let traffic through)
        assert cb.is_open() is False
        assert cb.status().state == CircuitState.CLOSED


def test_is_open_fail_open_when_redis_client_unavailable():
    cb = CircuitBreaker("test_k")
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        side_effect=RuntimeError("connection refused"),
    ):
        assert cb.is_open() is False
        assert cb.status().state == CircuitState.CLOSED


def test_trip_swallows_redis_exception():
    cb = CircuitBreaker("test_l")
    bad_redis = MagicMock()
    bad_redis.get = MagicMock(return_value=None)
    bad_redis.set = MagicMock(side_effect=RuntimeError("write failed"))
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=bad_redis,
    ):
        # Must NOT raise (the LLM hot path can't tolerate exceptions here)
        cb.trip(reason="x")


def test_clear_swallows_redis_exception():
    cb = CircuitBreaker("test_m")
    bad_redis = MagicMock()
    bad_redis.delete = MagicMock(side_effect=RuntimeError("delete failed"))
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=bad_redis,
    ):
        cb.clear()  # no exception
