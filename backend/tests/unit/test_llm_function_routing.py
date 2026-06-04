"""PR1 — per-functional-block model routing resolver tests (2026-05-29).

resolve_model_for must:
  - return None (→ caller uses its default) in EVERY non-routing path, so
    flag-OFF / empty-map is byte-for-byte legacy
  - read the map body from backend.config._flag_override_cache DIRECTLY (P0-1:
    the Settings.__getattribute__ hook only honours ENABLE_-prefixed overrides,
    so a settings.LLM_FUNCTION_MODEL_MAP read would never see a front-end edit)
  - validate each entry defensively (malformed → None for that node, never raise)
  - return a shallow copy (caller mutation must not poison the shared cache)
"""
from __future__ import annotations

import pytest

import backend.config as cfg
from backend.config import settings
from backend.agents.services.llm_service import resolve_model_for

_VALID = {"model": "deepseek-v4-flash", "provider": "openai"}


@pytest.fixture
def routing_on(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", True, raising=False)


def _set_map(monkeypatch, m):
    monkeypatch.setitem(cfg._flag_override_cache, "LLM_FUNCTION_MODEL_MAP", m)


# --------------------------------------------------------------- flag-OFF / no-op
def test_flag_off_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", False, raising=False)
    _set_map(monkeypatch, {"code_gen": _VALID})  # map present but flag OFF
    assert resolve_model_for("code_gen") is None


def test_none_or_empty_node_key_returns_none(routing_on, monkeypatch):
    # 2026-06-04: the STARTUP default now carries a __default__ catch-all (coding-
    # plan hardening), which by design captures node_key=None/unmapped. To test the
    # "no matching entry → None" contract we set an explicit map WITHOUT __default__.
    _set_map(monkeypatch, {"code_gen": _VALID})
    assert resolve_model_for(None) is None
    assert resolve_model_for("") is None


def test_node_not_in_map_returns_none(routing_on, monkeypatch):
    _set_map(monkeypatch, {"code_gen": _VALID})
    assert resolve_model_for("hypothesis") is None


# --------------------------------------------------------------- happy path
def test_valid_entry_resolved(routing_on, monkeypatch):
    _set_map(monkeypatch, {"code_gen": _VALID})
    assert resolve_model_for("code_gen") == {"model": "deepseek-v4-flash", "provider": "openai"}


def test_extra_fields_passthrough(routing_on, monkeypatch):
    _set_map(monkeypatch, {"code_gen": {
        "model": "m", "provider": "anthropic",
        "base_url": "http://x", "thinking_effort": "low",
    }})
    r = resolve_model_for("code_gen")
    assert r["provider"] == "anthropic"
    assert r["base_url"] == "http://x"
    assert r["thinking_effort"] == "low"


def test_provider_defaults_to_openai(routing_on, monkeypatch):
    _set_map(monkeypatch, {"code_gen": {"model": "m"}})  # no provider key
    assert resolve_model_for("code_gen") == {"model": "m", "provider": "openai"}


# --------------------------------------------------------------- defensive validation
@pytest.mark.parametrize("bad", [
    "not-a-dict",                            # entry not a dict
    {"provider": "openai"},                  # missing model
    {"model": "", "provider": "openai"},     # empty model
    {"model": 123, "provider": "openai"},    # non-str model
    {"model": "m", "provider": "cohere"},    # invalid provider
])
def test_malformed_entry_returns_none(routing_on, monkeypatch, bad):
    _set_map(monkeypatch, {"code_gen": bad})
    assert resolve_model_for("code_gen") is None


def test_malformed_map_returns_none(routing_on, monkeypatch):
    _set_map(monkeypatch, "not-a-dict")
    assert resolve_model_for("code_gen") is None


# --------------------------------------------------------------- P0-1: cache read
def test_override_wins_over_startup_default(routing_on, monkeypatch):
    _set_map(monkeypatch, {"hypothesis": {"model": "OVERRIDE", "provider": "openai"}})
    assert resolve_model_for("hypothesis")["model"] == "OVERRIDE"


def test_startup_default_used_when_no_override(routing_on, monkeypatch):
    monkeypatch.delitem(cfg._flag_override_cache, "LLM_FUNCTION_MODEL_MAP", raising=False)
    monkeypatch.delitem(cfg._flag_override_cache, "LLM_PROVIDERS", raising=False)
    r = resolve_model_for("hypothesis")
    # 2026-06-04 HARDENING: startup default now mirrors the live DB override —
    # every node routes to aliyun_coding_plan (token-plan ran out of budget). The
    # provider_ref expands via the LLM_PROVIDERS startup seed → provider "openai"
    # (coding-plan's sdk) + the coding.dashscope base_url + the per-provider key_ref.
    assert r is not None
    assert r["model"] == "qwen3.6-plus"
    assert r["provider"] == "openai"
    assert r["base_url"] == "https://coding.dashscope.aliyuncs.com/v1"
    assert r["api_key_ref"] == "llm_provider_aliyun_coding_plan"


# --------------------------------------------------------------- shallow copy
def test_returns_shallow_copy_not_cache_ref(routing_on, monkeypatch):
    m = {"code_gen": {"model": "m", "provider": "openai"}}
    _set_map(monkeypatch, m)
    resolve_model_for("code_gen")["model"] = "MUTATED"
    assert m["code_gen"]["model"] == "m"  # cache entry untouched
