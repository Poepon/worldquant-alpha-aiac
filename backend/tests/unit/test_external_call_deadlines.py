"""Hard-deadline resilience on external calls (2026-05-21).

Root cause being guarded: a DeepSeek LLM HTTP call with no effective timeout
left the asyncio loop parked in `select` forever (py-spy on the frozen worker),
and `--pool=solo` single-threaded → the hung mining task owned the only event
loop → permanent RUNNING zombie. These tests pin the two in-task deadlines:

  A1  LLMService.call() wraps every network await in asyncio.wait_for → a hung
      provider returns LLMResponse(success=False) within the deadline, not hangs.
  A1.4 asyncio.wait_for raises builtin TimeoutError → must be classified as an
      API failure so repeated hangs trip LLM_API_CIRCUIT.

(A2 — the serial _run_one_round_inline round-deadline test — was removed when
the serial loop was retired 2026-05-29; the pipeline path bounds rounds via
SIM_PIPELINE_OP_TIMEOUT_SEC + heartbeat-abort.)
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.agents.services import llm_service as llm_mod
from backend.agents.services.llm_service import LLMService, _llm_error_is_api_failure


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


