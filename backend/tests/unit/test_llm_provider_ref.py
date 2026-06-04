"""Named-provider expansion in resolve_model_for (2026-06-04).

A routing entry may reference a pre-configured provider profile via
``provider_ref`` instead of carrying provider/base_url inline. _expand_provider_ref
must, at resolve time:
  - look up LLM_PROVIDERS (runtime override in _flag_override_cache wins) and
    expand sdk→provider, base_url, and a derived/explicit api_key_ref
  - let the profile WIN over any inline provider/base_url on the entry
  - derive api_key_ref = ``llm_provider_<name>`` when the profile pins none
  - pass an unknown ref through unchanged (→ default provider, no crash)
  - never raise — a bad registry must not break a round
"""
from __future__ import annotations

import pytest

import backend.config as cfg
from backend.config import settings
from backend.agents.services.llm_service import resolve_model_for


@pytest.fixture
def routing_on(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", True, raising=False)


def _set_map(monkeypatch, m):
    monkeypatch.setitem(cfg._flag_override_cache, "LLM_FUNCTION_MODEL_MAP", m)


def _set_providers(monkeypatch, p):
    monkeypatch.setitem(cfg._flag_override_cache, "LLM_PROVIDERS", p)


# --------------------------------------------------------------- happy path
def test_provider_ref_expands_openai(routing_on, monkeypatch):
    _set_providers(monkeypatch, {
        "moonshot": {"label": "Moonshot", "sdk": "openai",
                     "base_url": "https://api.moonshot.cn/v1"},
    })
    _set_map(monkeypatch, {"code_gen": {"model": "kimi", "provider_ref": "moonshot"}})
    r = resolve_model_for("code_gen")
    assert r == {
        "model": "kimi",
        "provider": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_ref": "llm_provider_moonshot",   # derived
    }
    assert "provider_ref" not in r  # consumed during expansion


def test_provider_ref_anthropic_empty_base_url(routing_on, monkeypatch):
    _set_providers(monkeypatch, {
        "claude": {"label": "Anthropic", "sdk": "anthropic", "base_url": ""},
    })
    _set_map(monkeypatch, {"hypothesis": {"model": "claude-opus", "provider_ref": "claude"}})
    r = resolve_model_for("hypothesis")
    assert r["provider"] == "anthropic"
    assert "base_url" not in r  # empty profile base_url → SDK default
    assert r["api_key_ref"] == "llm_provider_claude"


def test_profile_explicit_api_key_ref_wins(routing_on, monkeypatch):
    _set_providers(monkeypatch, {
        "x": {"sdk": "openai", "base_url": "http://x", "api_key_ref": "custom_key"},
    })
    _set_map(monkeypatch, {"code_gen": {"model": "m", "provider_ref": "x"}})
    assert resolve_model_for("code_gen")["api_key_ref"] == "custom_key"


def test_profile_wins_over_inline(routing_on, monkeypatch):
    # Legacy inline base_url/provider must be overridden by the referenced profile.
    _set_providers(monkeypatch, {
        "p": {"sdk": "anthropic", "base_url": "http://profile"},
    })
    _set_map(monkeypatch, {"code_gen": {
        "model": "m", "provider": "openai",
        "base_url": "http://inline", "provider_ref": "p",
    }})
    r = resolve_model_for("code_gen")
    assert r["provider"] == "anthropic"
    assert r["base_url"] == "http://profile"


def test_thinking_effort_survives_expansion(routing_on, monkeypatch):
    _set_providers(monkeypatch, {"p": {"sdk": "openai", "base_url": "http://x"}})
    _set_map(monkeypatch, {"code_gen": {
        "model": "m", "provider_ref": "p", "thinking_effort": "low",
    }})
    assert resolve_model_for("code_gen")["thinking_effort"] == "low"


# --------------------------------------------------------------- defensive
def test_unknown_provider_ref_passes_through(routing_on, monkeypatch):
    _set_providers(monkeypatch, {"known": {"sdk": "openai", "base_url": "http://x"}})
    _set_map(monkeypatch, {"code_gen": {"model": "m", "provider_ref": "ghost"}})
    r = resolve_model_for("code_gen")
    # Unknown ref → no expansion; entry still valid, defaults to openai, no key.
    assert r == {"model": "m", "provider": "openai"}


def test_provider_ref_with_empty_registry(routing_on, monkeypatch):
    monkeypatch.delitem(cfg._flag_override_cache, "LLM_PROVIDERS", raising=False)
    _set_map(monkeypatch, {"code_gen": {"model": "m", "provider_ref": "anything"}})
    r = resolve_model_for("code_gen")
    assert r == {"model": "m", "provider": "openai"}


def test_bad_profile_type_passes_through(routing_on, monkeypatch):
    _set_providers(monkeypatch, {"p": "not-a-dict"})
    _set_map(monkeypatch, {"code_gen": {"model": "m", "provider_ref": "p"}})
    assert resolve_model_for("code_gen") == {"model": "m", "provider": "openai"}


def test_inline_only_still_works(routing_on, monkeypatch):
    # No provider_ref → legacy inline path unchanged (backward compat).
    _set_providers(monkeypatch, {})
    _set_map(monkeypatch, {"code_gen": {
        "model": "m", "provider": "anthropic", "base_url": "http://inline",
        "api_key_ref": "k",
    }})
    r = resolve_model_for("code_gen")
    assert r == {
        "model": "m", "provider": "anthropic",
        "base_url": "http://inline", "api_key_ref": "k",
    }


# --------------------------------------------------------------- __default__ catch-all
def test_unmapped_node_falls_back_to_default(routing_on, monkeypatch):
    _set_providers(monkeypatch, {"p": {"sdk": "openai", "base_url": "http://x"}})
    _set_map(monkeypatch, {
        "code_gen": {"model": "kimi", "provider_ref": "p"},
        "__default__": {"model": "qwen-flash", "provider_ref": "p"},
    })
    # distill_context is NOT mapped → __default__ wins.
    r = resolve_model_for("distill_context")
    assert r == {
        "model": "qwen-flash", "provider": "openai",
        "base_url": "http://x", "api_key_ref": "llm_provider_p",
    }


def test_mapped_node_wins_over_default(routing_on, monkeypatch):
    _set_providers(monkeypatch, {"p": {"sdk": "openai", "base_url": "http://x"}})
    _set_map(monkeypatch, {
        "code_gen": {"model": "kimi", "provider_ref": "p"},
        "__default__": {"model": "qwen-flash", "provider_ref": "p"},
    })
    assert resolve_model_for("code_gen")["model"] == "kimi"


def test_untagged_call_uses_default(routing_on, monkeypatch):
    # node_key=None (a call that didn't tag itself) ALSO routes via __default__.
    _set_providers(monkeypatch, {"p": {"sdk": "openai", "base_url": "http://x"}})
    _set_map(monkeypatch, {"__default__": {"model": "qwen-flash", "provider_ref": "p"}})
    r = resolve_model_for(None)
    assert r is not None and r["model"] == "qwen-flash" and r["base_url"] == "http://x"


def test_no_default_unmapped_returns_none(routing_on, monkeypatch):
    # Without __default__, an unmapped node → None (caller uses construction default).
    _set_providers(monkeypatch, {"p": {"sdk": "openai", "base_url": "http://x"}})
    _set_map(monkeypatch, {"code_gen": {"model": "kimi", "provider_ref": "p"}})
    assert resolve_model_for("distill_context") is None
    assert resolve_model_for(None) is None  # untagged + no default = legacy


def test_default_ignored_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", False, raising=False)
    _set_providers(monkeypatch, {"p": {"sdk": "openai", "base_url": "http://x"}})
    _set_map(monkeypatch, {"__default__": {"model": "qwen-flash", "provider_ref": "p"}})
    assert resolve_model_for("distill_context") is None
    assert resolve_model_for(None) is None
