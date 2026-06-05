"""Phase B (2026-06-05) — LLMResponse token-split + truncation field tests.

Pins the new service-layer fields the per-node LLM benchmark relies on:
  - prompt_tokens / completion_tokens / reasoning_tokens read from usage
    (reasoning from usage.completion_tokens_details.reasoning_tokens)
  - reasoning_tokens is None-safe for non-reasoning models (e.g. kimi)
  - finish_reason exposed + truncated = finish_reason in {length, max_tokens}
  - the split accumulates ACROSS the json parse-retry, consistent with
    tokens_used (a divergent numerator/denominator would corrupt reasoning_share)

These fields live ONLY on the pydantic LLMResponse (service layer) — NOT on the
append-only LLMResponseProtocol or the test mocks — so this is their sole coverage.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.config import settings
from backend.agents.services.llm_service import LLMService


def _usage(prompt, completion, total, reasoning=None, ctd_present=True):
    """Fake OpenAI usage. reasoning=None + ctd_present=True → details object
    exists but reasoning_tokens is None (the real kimi shape). ctd_present=False
    → no completion_tokens_details attribute at all."""
    ns = SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
    )
    if ctd_present:
        ns.completion_tokens_details = SimpleNamespace(reasoning_tokens=reasoning)
    else:
        ns.completion_tokens_details = None
    return ns


class _StatefulCompletions:
    """Returns each scripted (content, usage, finish_reason) once, then repeats
    the last — lets a test force a parse-retry by scripting invalid-then-valid."""

    def __init__(self, responses):
        self._responses = responses
        self._i = 0

    async def create(self, **kw):
        i = min(self._i, len(self._responses) - 1)
        self._i += 1
        content, usage, finish_reason = self._responses[i]
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content, reasoning_content=None),
                finish_reason=finish_reason,
            )],
            usage=usage,
        )


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_LLM_API_CIRCUIT", False, raising=False)
    s = LLMService(provider="openai")
    s._credentials_loaded = True  # skip DB credential reload
    s.client = SimpleNamespace(chat=SimpleNamespace(completions=None))
    return s


def _script(svc, responses):
    svc.client.chat.completions = _StatefulCompletions(responses)


@pytest.mark.asyncio
async def test_token_split_and_reasoning_populated(svc):
    _script(svc, [(json.dumps({"ok": 1}),
                   _usage(prompt=100, completion=990, total=1090, reasoning=980), "stop")])
    r = await svc.call("sys", "user json", json_mode=True, model="qwen3.6-plus")
    assert r.success
    assert r.tokens_used == 1090
    assert r.prompt_tokens == 100
    assert r.completion_tokens == 990
    assert r.reasoning_tokens == 980  # reasoning premium IS measurable
    assert r.finish_reason == "stop"
    assert r.truncated is False


@pytest.mark.asyncio
async def test_reasoning_none_is_zero_safe(svc):
    # kimi shape: details object present, reasoning_tokens=None → coalesce to 0.
    _script(svc, [(json.dumps({"ok": 1}),
                   _usage(prompt=50, completion=20, total=70, reasoning=None), "stop")])
    r = await svc.call("sys", "user json", json_mode=True, model="kimi-k2.5")
    assert r.success
    assert r.reasoning_tokens == 0
    assert r.prompt_tokens == 50 and r.completion_tokens == 20


@pytest.mark.asyncio
async def test_reasoning_details_absent_is_zero_safe(svc):
    # endpoint that omits completion_tokens_details entirely → no crash, 0.
    _script(svc, [(json.dumps({"ok": 1}),
                   _usage(prompt=10, completion=5, total=15, ctd_present=False), "stop")])
    r = await svc.call("sys", "user json", json_mode=True, model="glm-5")
    assert r.success and r.reasoning_tokens == 0


@pytest.mark.asyncio
async def test_truncated_but_parseable_flagged(svc):
    # finish_reason='length' yet valid JSON (schema closed early) → truncated=True.
    _script(svc, [(json.dumps({"ok": 1}),
                   _usage(prompt=10, completion=4096, total=4106, reasoning=0), "length")])
    r = await svc.call("sys", "user json", json_mode=True, model="m")
    assert r.success  # JSON parsed
    assert r.finish_reason == "length"
    assert r.truncated is True


@pytest.mark.asyncio
async def test_split_accumulates_across_parse_retry(svc):
    # 1st attempt invalid JSON → retry; 2nd valid. The split must SUM across both
    # attempts, exactly like tokens_used (consistent reasoning_share).
    _script(svc, [
        ("not json{", _usage(prompt=10, completion=20, total=30, reasoning=5), "stop"),
        (json.dumps({"ok": 1}), _usage(prompt=10, completion=40, total=50, reasoning=8), "stop"),
    ])
    r = await svc.call("sys", "user json", json_mode=True, model="m")
    assert r.success
    assert r.tokens_used == 80         # 30 + 50
    assert r.prompt_tokens == 20       # 10 + 10
    assert r.completion_tokens == 60   # 20 + 40
    assert r.reasoning_tokens == 13    # 5 + 8
