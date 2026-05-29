"""PR5 — task-level model override (contextvar) tests (2026-05-29).

task.config["llm_overrides"] → set_task_function_overrides binds a per-async-context
node→model map that resolve_model_for honours ABOVE the global map and INDEPENDENT
of the global flag — enabling single-task single-node A/B (Phase C attribution).
Must be concurrency-safe (ContextVar, not shared state).
"""
from __future__ import annotations

import asyncio

import pytest

import backend.config as cfg
from backend.config import settings
from backend.agents.services.llm_service import (
    resolve_model_for,
    set_task_function_overrides,
    clear_task_function_overrides,
)


def test_task_override_independent_of_global_flag(monkeypatch):
    # global flag OFF, yet a task override still routes its node.
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", False, raising=False)
    tok = set_task_function_overrides({"code_gen": {"model": "TASK-M", "provider": "openai"}})
    try:
        assert resolve_model_for("code_gen") == {"model": "TASK-M", "provider": "openai"}
        # a node NOT in the task override + flag OFF → no routing
        assert resolve_model_for("hypothesis") is None
    finally:
        clear_task_function_overrides(tok)
    # cleared → back to default (None)
    assert resolve_model_for("code_gen") is None


def test_task_override_wins_over_global_map(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", True, raising=False)
    monkeypatch.setitem(cfg._flag_override_cache, "LLM_FUNCTION_MODEL_MAP",
                        {"code_gen": {"model": "GLOBAL", "provider": "openai"}})
    tok = set_task_function_overrides({"code_gen": {"model": "TASK", "provider": "openai"}})
    try:
        assert resolve_model_for("code_gen")["model"] == "TASK"   # task layer wins
    finally:
        clear_task_function_overrides(tok)
    assert resolve_model_for("code_gen")["model"] == "GLOBAL"      # back to global


def test_malformed_task_entry_falls_through_to_global(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", True, raising=False)
    monkeypatch.setitem(cfg._flag_override_cache, "LLM_FUNCTION_MODEL_MAP",
                        {"code_gen": {"model": "GLOBAL", "provider": "openai"}})
    # task entry missing model → must NOT break; falls through to global map
    tok = set_task_function_overrides({"code_gen": {"provider": "openai"}})
    try:
        assert resolve_model_for("code_gen")["model"] == "GLOBAL"
    finally:
        clear_task_function_overrides(tok)


def test_non_dict_override_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", False, raising=False)
    tok = set_task_function_overrides("not-a-dict")  # coerced to None
    try:
        assert resolve_model_for("code_gen") is None
    finally:
        clear_task_function_overrides(tok)


@pytest.mark.asyncio
async def test_concurrent_tasks_no_override_bleed(monkeypatch):
    # Two concurrent coroutines set DIFFERENT overrides; ContextVar must isolate
    # them (asyncio.gather wraps each coro in its own Task = own context copy).
    monkeypatch.setattr(settings, "ENABLE_PER_FUNCTION_LLM_ROUTING", False, raising=False)
    results = {}

    async def worker(name, model):
        set_task_function_overrides({"code_gen": {"model": model, "provider": "openai"}})
        await asyncio.sleep(0.01)  # yield so the two interleave
        results[name] = resolve_model_for("code_gen")["model"]

    await asyncio.gather(worker("A", "MODEL-A"), worker("B", "MODEL-B"))
    assert results == {"A": "MODEL-A", "B": "MODEL-B"}  # no cross-contamination
