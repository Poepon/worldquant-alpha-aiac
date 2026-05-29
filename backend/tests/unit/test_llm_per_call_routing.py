"""PR2 — per-call model routing + client cache + fallback tests (2026-05-29).

Covers the call()-level routing seam:
  - client cache keyed by (provider, base_url, sha256(api_key)[:16]) — no plaintext
  - _get_client cache reuse / per-endpoint isolation / lazy anthropic build (P0-2)
  - call() honours explicit model / routed model / flag-OFF default
  - concurrent calls with different models don't bleed via self.model
  - routed model API failure falls back to the construction default ONCE
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import backend.config as cfg
from backend.config import settings
from backend.agents.services.llm_service import LLMService


# --------------------------------------------------------------------------- fakes
class _FakeCompletions:
    def __init__(self, fail_models=None):
        self.fail_models = set(fail_models or ())
        self.models_seen = []

    async def create(self, **kw):
        m = kw["model"]
        self.models_seen.append(m)
        if m in self.fail_models:
            raise TimeoutError(f"simulated API failure for {m}")  # _llm_error_is_api_failure → True
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"model": m}), reasoning_content=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(total_tokens=10),
        )


class _FakeOpenAIClient:
    def __init__(self, fail_models=None):
        self.chat = SimpleNamespace(completions=_FakeCompletions(fail_models))


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_LLM_API_CIRCUIT", False, raising=False)
    s = LLMService(provider="openai")
    s._credentials_loaded = True  # skip DB credential reload
    s.client = _FakeOpenAIClient()
    return s


def _route(monkeypatch, m):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", True, raising=False)
    monkeypatch.setitem(cfg._flag_override_cache, "LLM_FUNCTION_MODEL_MAP", m)


# --------------------------------------------------------------------------- cache key
def test_client_cache_key_no_plaintext():
    k = LLMService._client_cache_key("openai", "http://x", "super-secret-key")
    assert "super-secret-key" not in k
    assert k[0] == "openai" and k[1] == "http://x" and len(k[2]) == 16
    # same inputs → same key; different key material → different hash
    assert k == LLMService._client_cache_key("openai", "http://x", "super-secret-key")
    assert k != LLMService._client_cache_key("openai", "http://x", "other-key")


# --------------------------------------------------------------------------- _get_client
def test_get_client_openai_cached(svc):
    c1 = svc._get_client("openai", "http://ep1", None)
    c2 = svc._get_client("openai", "http://ep1", None)
    assert c1 is c2  # cache hit
    c3 = svc._get_client("openai", "http://ep2", None)
    assert c3 is not c1  # different endpoint → different client


def test_clear_client_cache_drops_routed(svc):
    svc._get_client("openai", "http://ep1", None)
    assert svc._client_cache
    svc.invalidate_credentials_cache()  # must clear client cache too
    assert svc._client_cache == {}


def test_per_call_anthropic_lazy_build(svc, monkeypatch):
    # default-openai service has anthropic_client=None; routing to anthropic must
    # lazily build one (P0-2) rather than crash on None.
    assert svc.anthropic_client is None
    svc.anthropic_api_key = "k-anthropic"
    sentinel = object()
    monkeypatch.setattr(svc, "_build_anthropic_client", lambda key, burl: sentinel)
    got = svc._get_client("anthropic", None, None)
    assert got is sentinel
    assert svc._get_client("anthropic", None, None) is sentinel  # cached


def test_anthropic_no_key_raises(svc, monkeypatch):
    svc.anthropic_api_key = ""
    with pytest.raises(RuntimeError):
        svc._get_client("anthropic", None, None)


# --------------------------------------------------------------------------- call() routing
@pytest.mark.asyncio
async def test_call_explicit_model_used(svc):
    resp = await svc.call("sys", "user json", model="explicit-model")
    assert resp.success and resp.model == "explicit-model"
    assert resp.parsed == {"model": "explicit-model"}


@pytest.mark.asyncio
async def test_call_flag_off_uses_default(svc, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", False, raising=False)
    _route(monkeypatch, {"code_gen": {"model": "ROUTED", "provider": "openai"}})
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", False, raising=False)  # ensure OFF
    resp = await svc.call("sys", "user json", node_key="code_gen")
    assert resp.model == svc.model  # NOT routed — flag OFF


@pytest.mark.asyncio
async def test_call_routed_model_used(svc, monkeypatch):
    _route(monkeypatch, {"code_gen": {"model": "ROUTED-cg", "provider": "openai"}})
    resp = await svc.call("sys", "user json", node_key="code_gen")
    assert resp.success and resp.model == "ROUTED-cg"


@pytest.mark.asyncio
async def test_concurrent_calls_no_model_bleed(svc):
    before = svc.model
    r_a, r_b = await asyncio.gather(
        svc.call("sys", "user json", model="MODEL-A"),
        svc.call("sys", "user json", model="MODEL-B"),
    )
    assert r_a.model == "MODEL-A" and r_b.model == "MODEL-B"
    assert svc.model == before  # self.model never mutated (concurrency-safe)


@pytest.mark.asyncio
async def test_fallback_to_default_on_routed_failure(svc, monkeypatch):
    # routed model fails at API level → falls back to construction default ONCE.
    svc.client = _FakeOpenAIClient(fail_models={"BAD-routed"})
    _route(monkeypatch, {"code_gen": {"model": "BAD-routed", "provider": "openai"}})
    resp = await svc.call("sys", "user json", node_key="code_gen")
    assert resp.success and resp.model == svc.model  # fell back to default
    seen = svc.client.chat.completions.models_seen
    assert "BAD-routed" in seen and svc.model in seen  # tried routed, then default


@pytest.mark.asyncio
async def test_no_fallback_loop_when_default_also_fails(svc, monkeypatch):
    # default also failing must NOT recurse infinitely — one fallback attempt only.
    svc.client = _FakeOpenAIClient(fail_models={"BAD-routed", svc.model})
    _route(monkeypatch, {"code_gen": {"model": "BAD-routed", "provider": "openai"}})
    resp = await svc.call("sys", "user json", node_key="code_gen")
    assert resp.success is False
    # exactly two create attempts: routed + one fallback
    assert svc.client.chat.completions.models_seen == ["BAD-routed", svc.model]
