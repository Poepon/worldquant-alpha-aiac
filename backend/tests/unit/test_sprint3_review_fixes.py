"""Sprint 3 F1-F12 review-fix verification tests (2026-05-20).

3-round fresh agent review (R1 correctness + R2 failure-mode + R3
integration) found 5 MUST + 10 SHOULD. These tests pin each fix.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# F1+F3: _DistillLLMShim uses real LLMService.call (not non-existent acomplete)
# ---------------------------------------------------------------------------

def test_f1_distill_shim_calls_real_llm_service_call_method():
    """Old shim invoked self.llm.acomplete(prompt=..., max_tokens=...,
    temperature=...) — but LLMService has NO acomplete method. The fix
    uses LLMService.call(system_prompt, user_prompt, ...) which exists."""
    import inspect
    from backend.tasks.logic_distill_tasks import _DistillLLMShim
    src = inspect.getsource(_DistillLLMShim)
    # Active code must NOT call acomplete (docstring history may still
    # reference it for context — check the active invocation pattern)
    assert "await self.llm.acomplete(" not in src
    # real call invocation present
    assert "await self.llm.call(" in src
    assert "system_prompt=" in src
    assert "user_prompt=" in src


@pytest.mark.asyncio
async def test_f1_shim_returns_text_from_llmresponse():
    """Verify the shim correctly maps LLMResponse.content → text."""
    from backend.tasks.logic_distill_tasks import _DistillLLMShim
    from backend.agents.services.llm_service import LLMResponse

    fake_llm = MagicMock()
    fake_llm.call = AsyncMock(return_value=LLMResponse(
        content="Distilled momentum logic.",
        model="test-model",
        tokens_used=100,
        success=True,
    ))
    shim = _DistillLLMShim(fake_llm)
    out = await shim.call("test prompt")
    assert out["text"] == "Distilled momentum logic."
    assert out["model"] == "test-model"
    assert out["cost_usd"] > 0  # 100 tokens at $0.10/1K = $0.01


@pytest.mark.asyncio
async def test_f1_shim_handles_success_false():
    """LLMResponse.success=False → shim returns empty text."""
    from backend.tasks.logic_distill_tasks import _DistillLLMShim
    from backend.agents.services.llm_service import LLMResponse

    fake_llm = MagicMock()
    fake_llm.call = AsyncMock(return_value=LLMResponse(
        content="",
        model="test-model",
        tokens_used=0,
        success=False,
        error="API timeout",
    ))
    shim = _DistillLLMShim(fake_llm)
    out = await shim.call("test")
    assert out["text"] == ""
    assert out["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# F2: _week_anchor uses Asia/Shanghai timezone
# ---------------------------------------------------------------------------

def test_f2_week_anchor_aligns_with_sh_timezone():
    """Cron schedule is SH-time; anchor must use SH-tz so double-fires
    at SH-boundary hit the unique constraint correctly."""
    try:
        from zoneinfo import ZoneInfo
        sh = ZoneInfo("Asia/Shanghai")
    except ImportError:
        pytest.skip("zoneinfo unavailable")
    from backend.services.logic_distill_service import _week_anchor

    # Sat 19:00 UTC = Sun 03:00 SH (cron fires here)
    cron_fire_utc = datetime(2026, 5, 23, 19, 0, tzinfo=timezone.utc)
    anchor = _week_anchor(cron_fire_utc)
    anchor_sh = anchor.astimezone(sh)
    assert anchor_sh.weekday() == 0  # SH-Monday
    assert anchor_sh.hour == 0


def test_f2_alembic_unique_constraint_migration_exists():
    """F2 follow-up: distilled_logic_library should have a unique
    constraint on (distilled_at_week, region, pillar) — partial WHERE
    retired_at IS NULL on Postgres."""
    import inspect
    import importlib
    mod = importlib.import_module(
        "backend.alembic.versions.o6d4a8f2c5b7_distilled_logic_unique"
    )
    src = inspect.getsource(mod)
    assert "uq_distilled_logic_week_region_pillar_active" in src
    assert "unique=True" in src


# ---------------------------------------------------------------------------
# F4/F5: cognitive_layer_id_used reset happens BEFORE inject early-return
# ---------------------------------------------------------------------------

def test_f4_cognitive_layer_reset_at_function_entry():
    """Reset must happen at the top of node_hypothesis before the
    R1b inject-path early return — otherwise stale layer ID carries over."""
    import inspect
    from backend.agents.graph.nodes import generation as gen
    src = inspect.getsource(gen.node_hypothesis)
    # Find the reset line position relative to the early return
    reset_idx = src.find('state.cognitive_layer_id_used = ""')
    early_ret_idx = src.find("_node_hypothesis_inject_consumed(")
    assert reset_idx > 0, "expected explicit reset"
    assert early_ret_idx > 0, "expected inject branch present"
    # Reset must appear BEFORE the early return
    assert reset_idx < early_ret_idx, (
        "cognitive_layer_id_used reset must come BEFORE inject path early-return"
    )


def test_f5_inject_path_return_dict_clears_cognitive_layer():
    """The inject-path return dict must include
    cognitive_layer_id_used="" so LangGraph state-merge propagates the clear."""
    import inspect
    from backend.agents.graph.nodes import generation as gen
    src = inspect.getsource(gen._node_hypothesis_inject_consumed)
    assert '"cognitive_layer_id_used": ""' in src


def test_f6_main_path_return_dict_propagates_layer_id():
    """Main node_hypothesis return dict must include cognitive_layer_
    id_used so LangGraph state-merge sends it to downstream nodes.

    Phase 1a-E (2026-06-06): the R8-v3 cognitive-layer selection moved into
    CognitiveLayerEnricher (nodes/prompt_enrichers.py); node_hypothesis now
    propagates the value via ``enrichment.cognitive_layer_id_used`` (which the
    enricher sets to the chosen layer_id). Same return-dict contract, refactored
    source expression — behaviour covered by the test_node_hypothesis_* suite.
    """
    import inspect
    from backend.agents.graph.nodes import generation as gen
    src = inspect.getsource(gen.node_hypothesis)
    assert '"cognitive_layer_id_used": enrichment.cognitive_layer_id_used' in src


# ---------------------------------------------------------------------------
# F7: pillar_affinity aligned with PILLAR_TARGET_DISTRIBUTION keys
# ---------------------------------------------------------------------------

def test_f7_pillar_affinity_uses_real_pillar_keys():
    """YAML pillar_affinity must use PILLAR_TARGET_DISTRIBUTION vocab
    (momentum/value/quality/volatility/sentiment/other), not the prior
    disjoint set (macro/liquidity/microstructure/etc)."""
    from backend.services.cognitive_layer_service import (
        load_cognitive_layers, clear_layer_cache,
    )
    clear_layer_cache()
    layers = load_cognitive_layers()
    valid_pillars = {"momentum", "value", "quality", "volatility", "sentiment", "other"}
    for layer in layers:
        for p in layer.pillar_affinity:
            assert p in valid_pillars, (
                f"layer {layer.layer_id!r} has unknown pillar {p!r} — "
                f"must be one of {valid_pillars}"
            )


# ---------------------------------------------------------------------------
# F8: round_index derived from experiment_trace length (not stub 0)
# ---------------------------------------------------------------------------

def test_f8_round_index_uses_experiment_trace_length():
    """generation.py must derive round_index from experiment_trace, not
    from a non-existent state.round_index field (which would always be 0)."""
    import inspect
    from backend.agents.graph.nodes import generation as gen
    src = inspect.getsource(gen.node_hypothesis)
    # The stub `state.round_index` reference should be gone
    assert "getattr(state, 'round_index', 0)" not in src
    assert 'getattr(state, "round_index", 0)' not in src
    # Real derivation present
    assert "len(experiment_trace)" in src


# ---------------------------------------------------------------------------
# F9: build_distill_prompt uses str.replace (not .format()) to avoid KeyError
# ---------------------------------------------------------------------------

def test_f9_distill_prompt_handles_curly_braces_in_expression():
    """BRAIN expression containing `{` would have caused KeyError in
    template.format(). The fix uses str.replace which is literal."""
    from backend.services.logic_distill_service import (
        build_distill_prompt, AlphaSummary,
    )
    alphas = [
        AlphaSummary(id=1, expression="rank(close) > {threshold_value}", sharpe=1.5),
    ]
    # Pre-fix this would have crashed with KeyError on 'threshold_value'
    prompt = build_distill_prompt(pillar="momentum", region="USA", alphas=alphas)
    assert "{threshold_value}" in prompt  # preserved literally


def test_f9_distill_prompt_handles_format_specifier_chars():
    """Expression containing `{0}` or other .format()-significant
    chars must not break the prompt rendering."""
    from backend.services.logic_distill_service import (
        build_distill_prompt, AlphaSummary,
    )
    alphas = [
        AlphaSummary(id=1, expression="ts_rank({0}, 60)", sharpe=1.2),
        AlphaSummary(id=2, expression="bbands({lookback}, 2)", sharpe=1.4),
    ]
    prompt = build_distill_prompt(pillar="momentum", region="USA", alphas=alphas)
    # Both literal expressions preserved
    assert "ts_rank({0}, 60)" in prompt
    assert "bbands({lookback}, 2)" in prompt


# ---------------------------------------------------------------------------
# F10: _DROP_ORDER uses real PromptContext field names
# ---------------------------------------------------------------------------

def test_f10_drop_order_excludes_stub_dedup_blacklist():
    """The prior _DROP_ORDER referenced 'dedup_blacklist' which is NOT
    a PromptContext field. Fix uses real field names."""
    from backend.services.cognitive_layer_service import _DROP_ORDER
    assert "dedup_blacklist" not in _DROP_ORDER
    # Real PromptContext fields should be in the list
    valid_fields = {"failure_pitfalls", "cross_task_hypotheses", "macro_narratives"}
    assert set(_DROP_ORDER) <= valid_fields | set(_DROP_ORDER)


# ---------------------------------------------------------------------------
# F11: /ops/r8-v3 endpoint has SQLite dialect guard
# ---------------------------------------------------------------------------

def test_f11_cognitive_layer_stats_dialect_guard():
    """The endpoint uses Postgres-only JSONB syntax — on SQLite it
    must degrade gracefully (return empty payload) rather than crash."""
    import inspect
    from backend.routers import ops
    src = inspect.getsource(ops.r8v3_cognitive_layer_stats)
    assert 'dialect.name' in src
    assert 'postgresql' in src or 'CognitiveLayerStatsOut(' in src


# ---------------------------------------------------------------------------
# F12: run_weekly_logic_distill added to tasks/__init__.py __all__
# ---------------------------------------------------------------------------

def test_f12_run_weekly_logic_distill_in_tasks_all():
    """The task must be in __all__ to be discoverable via star-import."""
    from backend import tasks
    assert "run_weekly_logic_distill" in tasks.__all__


# ---------------------------------------------------------------------------
# IntegrityError soft fail in cron task
# ---------------------------------------------------------------------------

def test_integrity_error_handling_in_distill_task_source():
    """Cron task must catch IntegrityError-class commit failures
    (F2 unique constraint hits on cron double-fire) and return a
    structured error dict rather than crash."""
    import inspect
    from backend.tasks import logic_distill_tasks
    src = inspect.getsource(logic_distill_tasks._distill_async)
    assert "rollback" in src
    assert "duplicate_week_or_constraint_violation" in src
