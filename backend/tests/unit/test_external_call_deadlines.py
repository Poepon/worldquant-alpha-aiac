"""Hard-deadline resilience on external calls (2026-05-21).

Root cause being guarded: a DeepSeek LLM HTTP call with no effective timeout
left the asyncio loop parked in `select` forever (py-spy on the frozen worker),
and `--pool=solo` single-threaded → the hung mining task owned the only event
loop → permanent RUNNING zombie. These tests pin the two in-task deadlines:

  A1  LLMService.call() wraps every network await in asyncio.wait_for → a hung
      provider returns LLMResponse(success=False) within the deadline, not hangs.
  A1.4 asyncio.wait_for raises builtin TimeoutError → must be classified as an
      API failure so repeated hangs trip LLM_API_CIRCUIT.
  A2  _run_one_round_inline wraps run_evolution_loop in asyncio.wait_for → a hung
      round returns the soft-fail dict (caught by the existing except), so the
      FLAT loop continues and the worker never freezes.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.agents.services import llm_service as llm_mod
from backend.agents.services.llm_service import LLMService, _llm_error_is_api_failure
from backend.tasks import mining_tasks as mt


# --------------------------------------------------------------------------- A1.4
def test_timeout_error_classified_as_api_failure():
    """asyncio.wait_for raises builtin TimeoutError (== asyncio.TimeoutError);
    it must count toward the LLM_API_CIRCUIT fail counter."""
    assert _llm_error_is_api_failure(TimeoutError()) is True
    assert _llm_error_is_api_failure(asyncio.TimeoutError()) is True
    # content-level errors must NOT be classified as API failures (unchanged)
    assert _llm_error_is_api_failure(ValueError("bad json")) is False


# --------------------------------------------------------------------------- A1
class _HangingCompletions:
    async def create(self, **_kwargs):
        await asyncio.sleep(60)  # never returns within the test deadline


class _HangingChat:
    completions = _HangingCompletions()


class _HangingClient:
    chat = _HangingChat()


@pytest.mark.asyncio
async def test_llm_call_times_out_to_failure(monkeypatch):
    """A hung LLM HTTP call returns success=False within ~the deadline instead
    of blocking the event loop forever."""
    from backend.config import settings

    monkeypatch.setattr(settings, "LLM_CALL_TIMEOUT_SEC", 0.1, raising=False)
    monkeypatch.setattr(settings, "ENABLE_LLM_API_CIRCUIT", False, raising=False)

    svc = LLMService()
    # Force the openai-compatible path + skip the DB credential reload (which
    # would otherwise rebuild self.client and discard our hanging stub).
    svc.provider = "openai"
    svc._credentials_loaded = True
    svc.client = _HangingClient()

    start = time.monotonic()
    resp = await svc.call("system", "user", json_mode=False, max_tokens=16)
    elapsed = time.monotonic() - start

    assert resp.success is False
    # Returned promptly (deadline 0.1s); generous bound to avoid CI flakiness.
    assert elapsed < 5.0, f"call() did not honor the deadline (took {elapsed:.1f}s)"


# --------------------------------------------------------------------------- A2
class _FakeDB:
    async def rollback(self):
        return None

    async def refresh(self, _obj):
        return None


@pytest.mark.asyncio
async def test_round_deadline_soft_fails(monkeypatch):
    """A hung run_evolution_loop is bounded by MINING_ROUND_TIMEOUT_SEC and the
    existing except converts the TimeoutError into the soft-fail result dict, so
    the caller (FLAT loop) reads 0 alphas and continues — worker never freezes."""
    from backend.config import settings

    monkeypatch.setattr(settings, "MINING_ROUND_TIMEOUT_SEC", 0.1, raising=False)

    # Stub the pre-run_evolution_loop setup so the call reaches the wrapped await.
    async def _no_typed(*_a, **_k):
        return None

    async def _fields(*_a, **_k):
        return ["close", "volume"]

    async def _pool(*_a, **_k):
        return ["pv1"]

    monkeypatch.setattr(mt, "_maybe_run_typed_pipeline_round", _no_typed)
    monkeypatch.setattr(mt, "_prepare_round_fields", _fields)
    monkeypatch.setattr(mt, "_build_dataset_pool", _pool)
    monkeypatch.setattr(mt, "_get_active_level", lambda _task: 0)

    async def _hanging_evolution(**_kwargs):
        await asyncio.sleep(60)

    mining_agent = SimpleNamespace(run_evolution_loop=_hanging_evolution)
    task = SimpleNamespace(id=999, config={}, daily_goal=4)
    run = SimpleNamespace(id=1)

    start = time.monotonic()
    result = await mt._run_one_round_inline(
        _FakeDB(), task, run, brain=None, mining_agent=mining_agent, operators=[],
        dataset_id="pv1",
    )
    elapsed = time.monotonic() - start

    assert result.get("all_alphas") == []
    assert result.get("iterations_completed") == 0
    assert "error" in result  # soft-fail path stamped the error string
    assert elapsed < 5.0, f"round did not honor the deadline (took {elapsed:.1f}s)"
