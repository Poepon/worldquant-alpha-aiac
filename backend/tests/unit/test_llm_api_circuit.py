"""Unit tests for Phase 4 Sprint 0 PR0 — LLM_API_CIRCUIT.

Coverage:
  - _llm_record_fail increments counter; trips circuit at threshold
  - _llm_record_success resets counter + clears circuit
  - _llm_error_is_api_failure correctly classifies API vs content errors
  - LLMService.call() fast-fails when circuit is OPEN (returns success=False
    error='llm_api_circuit_open', no HTTP traffic)
  - ENABLE_LLM_API_CIRCUIT=False disables the whole mechanism

Mirrors backend/tests/unit/test_circuit_breaker.py fixture style + extends
fake Redis with incr/expire (the framework only uses set/get/delete; the LLM
counter path additionally needs incr/expire).
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest


@pytest.fixture
def _fake_redis():
    """Minimal Redis-shaped dict-backed mock supporting set/get/delete + incr/expire.

    Needed by both the CircuitBreaker framework (set/get/delete + TTL) AND
    the LLM_API_CIRCUIT helpers (incr + expire on the fail counter key).
    """

    class _R:
        def __init__(self):
            self.store: dict = {}
            self.ttls: dict = {}
            self.set_at: dict = {}

        def _expired(self, key):
            ts = self.set_at.get(key)
            ttl = self.ttls.get(key)
            if ts is not None and ttl is not None and time.time() - ts >= ttl:
                self.store.pop(key, None)
                self.ttls.pop(key, None)
                self.set_at.pop(key, None)
                return True
            return False

        def get(self, key):
            if self._expired(key):
                return None
            v = self.store.get(key)
            return v.encode("utf-8") if isinstance(v, str) else v

        def set(self, key, value, ex=None):
            self.store[key] = value
            if ex is not None:
                self.ttls[key] = int(ex)
                self.set_at[key] = time.time()
            return True

        def incr(self, key):
            if self._expired(key):
                pass  # already cleared by _expired
            cur = int(self.store.get(key) or 0)
            cur += 1
            self.store[key] = cur
            return cur

        def expire(self, key, seconds):
            if key in self.store:
                self.ttls[key] = int(seconds)
                self.set_at[key] = time.time()
                return 1
            return 0

        def delete(self, key):
            self.store.pop(key, None)
            self.ttls.pop(key, None)
            self.set_at.pop(key, None)
            return 1

    return _R()


@pytest.fixture
def _patched_redis(_fake_redis):
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=_fake_redis,
    ):
        yield _fake_redis


@pytest.fixture(autouse=True)
def _reset_llm_circuit(_patched_redis):
    """Make sure each test starts from CLOSED state + empty counter."""
    from backend.agents.services import llm_service as ls
    ls.LLM_API_CIRCUIT.clear(reason="test_setup")
    # Counter not strictly needed (_patched_redis is per-test) but clear anyway.
    _patched_redis.delete(ls._LLM_API_FAIL_COUNTER_KEY)
    yield


# ---------------------------------------------------------------------------
# _llm_record_fail / _llm_record_success
# ---------------------------------------------------------------------------


def test_record_fail_increments_counter_does_not_trip_below_threshold(_patched_redis):
    """Calling fail < threshold times should NOT trip the circuit."""
    from backend.agents.services.llm_service import (
        LLM_API_CIRCUIT, _llm_record_fail, _LLM_API_FAIL_COUNTER_KEY,
    )
    for _ in range(4):  # default threshold = 5
        _llm_record_fail(error_kind="APITimeoutError")
    assert int(_patched_redis.store.get(_LLM_API_FAIL_COUNTER_KEY) or 0) == 4
    assert LLM_API_CIRCUIT.is_open() is False


def test_record_fail_trips_circuit_at_threshold(_patched_redis):
    """5 consecutive fails should trip the circuit + reset counter."""
    from backend.agents.services.llm_service import (
        LLM_API_CIRCUIT, _llm_record_fail, _LLM_API_FAIL_COUNTER_KEY,
    )
    for _ in range(5):  # threshold = 5
        _llm_record_fail(error_kind="APITimeoutError")
    assert LLM_API_CIRCUIT.is_open() is True
    # Counter reset after trip so the next 5 post-clear can re-trip
    assert _patched_redis.store.get(_LLM_API_FAIL_COUNTER_KEY) is None


def test_record_success_resets_counter_and_clears_circuit(_patched_redis):
    """A success after some fails should reset counter + clear OPEN circuit."""
    from backend.agents.services.llm_service import (
        LLM_API_CIRCUIT, _llm_record_fail, _llm_record_success,
        _LLM_API_FAIL_COUNTER_KEY,
    )
    # Build up some failures + trip
    for _ in range(5):
        _llm_record_fail(error_kind="APIConnectionError")
    assert LLM_API_CIRCUIT.is_open() is True

    _llm_record_success()
    assert LLM_API_CIRCUIT.is_open() is False
    assert _patched_redis.store.get(_LLM_API_FAIL_COUNTER_KEY) is None


def test_record_success_when_circuit_closed_noop(_patched_redis):
    """Success when circuit already CLOSED should still reset counter
    (called every successful LLM call — must be cheap + idempotent)."""
    from backend.agents.services.llm_service import (
        LLM_API_CIRCUIT, _llm_record_fail, _llm_record_success,
        _LLM_API_FAIL_COUNTER_KEY,
    )
    _llm_record_fail(error_kind="APITimeoutError")
    assert int(_patched_redis.store.get(_LLM_API_FAIL_COUNTER_KEY) or 0) == 1

    _llm_record_success()
    assert LLM_API_CIRCUIT.is_open() is False
    assert _patched_redis.store.get(_LLM_API_FAIL_COUNTER_KEY) is None


def test_flag_off_disables_record_fail(_patched_redis):
    """ENABLE_LLM_API_CIRCUIT=False must short-circuit both record helpers."""
    from backend.agents.services.llm_service import (
        LLM_API_CIRCUIT, _llm_record_fail, _LLM_API_FAIL_COUNTER_KEY,
    )
    with patch("backend.config.settings.ENABLE_LLM_API_CIRCUIT", False):
        for _ in range(10):
            _llm_record_fail(error_kind="APITimeoutError")
    # Counter never written; circuit never trips
    assert _patched_redis.store.get(_LLM_API_FAIL_COUNTER_KEY) is None
    assert LLM_API_CIRCUIT.is_open() is False


# ---------------------------------------------------------------------------
# _llm_error_is_api_failure classification
# ---------------------------------------------------------------------------


def test_classify_api_failures():
    """openai/anthropic SDK exception class names should classify True."""
    from backend.agents.services.llm_service import _llm_error_is_api_failure

    class APITimeoutError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class InternalServerError(Exception):
        pass

    assert _llm_error_is_api_failure(APITimeoutError("...")) is True
    assert _llm_error_is_api_failure(APIConnectionError("...")) is True
    assert _llm_error_is_api_failure(RateLimitError("...")) is True
    assert _llm_error_is_api_failure(InternalServerError("...")) is True


def test_classify_content_errors_as_not_api_failure():
    """JSON parse / ValueError / KeyError shouldn't trip the circuit."""
    import json
    from backend.agents.services.llm_service import _llm_error_is_api_failure

    try:
        json.loads("not json")
    except json.JSONDecodeError as e:
        assert _llm_error_is_api_failure(e) is False

    assert _llm_error_is_api_failure(ValueError("Empty content")) is False
    assert _llm_error_is_api_failure(KeyError("missing")) is False
    assert _llm_error_is_api_failure(TypeError("...")) is False


def test_classify_status_code_5xx_true():
    """Generic exception with status_code>=500 → API failure.
    F-S1: 401 / 403 / 429 also trip; other 4xx do not."""
    from backend.agents.services.llm_service import _llm_error_is_api_failure

    class FakeStatus(Exception):
        def __init__(self, code):
            self.status_code = code

    assert _llm_error_is_api_failure(FakeStatus(500)) is True
    assert _llm_error_is_api_failure(FakeStatus(503)) is True
    assert _llm_error_is_api_failure(FakeStatus(429)) is True
    # F-S1: 401 (auth) + 403 (permission) → trip
    assert _llm_error_is_api_failure(FakeStatus(401)) is True
    assert _llm_error_is_api_failure(FakeStatus(403)) is True
    # Other 4xx → not transient API outage; bad request, not found etc.
    assert _llm_error_is_api_failure(FakeStatus(400)) is False
    assert _llm_error_is_api_failure(FakeStatus(404)) is False


def test_classify_auth_permission_class_names():
    """F-S1: openai/anthropic AuthenticationError + PermissionDeniedError
    class names classify True (mirrors BRAIN_AUTH_CIRCUIT 401 trip)."""
    from backend.agents.services.llm_service import _llm_error_is_api_failure

    class AuthenticationError(Exception):
        pass

    class PermissionDeniedError(Exception):
        pass

    assert _llm_error_is_api_failure(AuthenticationError("revoked key")) is True
    assert _llm_error_is_api_failure(PermissionDeniedError("quota")) is True


# ---------------------------------------------------------------------------
# LLMService.call() fast-fail when circuit OPEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_fast_fails_when_circuit_open(_patched_redis):
    """When LLM_API_CIRCUIT.is_open() returns True, call() must return
    success=False / error='llm_api_circuit_open' WITHOUT making any HTTP
    request to the underlying client."""
    from backend.agents.services.llm_service import LLMService, LLM_API_CIRCUIT

    # Trip the circuit
    LLM_API_CIRCUIT.trip(reason="test_setup", ttl_sec=300)
    assert LLM_API_CIRCUIT.is_open() is True

    service = LLMService.__new__(LLMService)  # bypass __init__
    service.model = "test-model"
    service.provider = "openai"
    # Mock client.chat.completions.create — should NOT be called
    class _MockClient:
        called = False

        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    _MockClient.called = True
                    raise AssertionError("should never be called")
    service.client = _MockClient()
    service.anthropic_client = None
    # Service helpers used by call()
    service._resolve_effort = lambda nk, te: None  # type: ignore
    service._emit_metrics = lambda *a, **k: None  # type: ignore

    async def _ensure_no_op():
        return None
    service._ensure_credentials_loaded = _ensure_no_op  # type: ignore

    resp = await service.call(
        system_prompt="sys",
        user_prompt="usr",
        temperature=0.5,
        json_mode=False,
        max_tokens=100,
    )
    assert resp.success is False
    assert resp.error == "llm_api_circuit_open"
    assert resp.content == ""
    assert _MockClient.called is False  # NO HTTP request was made
