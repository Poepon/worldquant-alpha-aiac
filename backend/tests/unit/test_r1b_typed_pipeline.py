"""Phase 3 R1b.4a: typed AlphaMiningPipeline dispatch helper tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §8.

R1b.4a ships only the dispatch helper — mining_tasks wiring deferred to
R1b.4b/c. These tests verify the helper's flag + variant gating logic +
budget cap behavior + soft-fall paths against mocked task/pipeline.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.graph.nodes.r1b_typed_pipeline import (
    _num_iter_per_round,
    _round_budget_usd,
    is_typed_pipeline_active,
    run_typed_round,
)


def _mk_task(*, hcv=3, config_extra=None):
    cfg = {"hypothesis_centric_variant": hcv}
    if config_extra:
        cfg.update(config_extra)
    return SimpleNamespace(id=42, config=cfg)


def _mk_iter_result(*, should_abandon=False):
    return SimpleNamespace(
        experiment=SimpleNamespace(
            alpha_id="exp-1", expression="rank(close)",
            hypothesis=SimpleNamespace(statement="thesis"),
            explanation="", metrics={"sharpe": 1.2},
            quality_status="PASS",
            status=SimpleNamespace(name="COMPLETED"),
        ),
        feedback=SimpleNamespace(should_abandon=should_abandon),
        knowledge_updated=False,
    )


# ---------------------------------------------------------------------------
# is_typed_pipeline_active gating
# ---------------------------------------------------------------------------

def test_active_requires_flag_on(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", False, raising=False)
    assert is_typed_pipeline_active(_mk_task(hcv=3)) is False


def test_active_requires_hcv_3(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    assert is_typed_pipeline_active(_mk_task(hcv=0)) is False
    assert is_typed_pipeline_active(_mk_task(hcv=2)) is False


def test_active_true_when_both_conditions(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    assert is_typed_pipeline_active(_mk_task(hcv=3)) is True


def test_active_handles_missing_config(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    task = SimpleNamespace(id=1, config=None)
    assert is_typed_pipeline_active(task) is False


def test_active_handles_non_int_hcv(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    task = SimpleNamespace(id=1, config={"hypothesis_centric_variant": "not-a-number"})
    assert is_typed_pipeline_active(task) is False


# ---------------------------------------------------------------------------
# Budget helpers
# ---------------------------------------------------------------------------

def test_round_budget_is_3x_per_alpha_ceiling(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 0.04, raising=False)
    assert abs(_round_budget_usd() - 0.12) < 1e-6


def test_num_iter_per_round_defaults_to_3(monkeypatch):
    from backend.config import settings
    monkeypatch.delattr(settings, "R1B_TYPED_NUM_ITER_PER_ROUND", raising=False)
    assert _num_iter_per_round() == 3


# ---------------------------------------------------------------------------
# run_typed_round soft-fall
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_typed_round_skipped_when_inactive(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", False, raising=False)
    out = await run_typed_round(task=_mk_task(), brain=None, db=None)
    assert out["skipped_disabled"] is True
    assert out["num_iter_executed"] == 0


@pytest.mark.asyncio
async def test_run_typed_round_executes_iterations(monkeypatch):
    """Flag ON + variant=3 + mocked pipeline → iterations advance + alphas accumulate."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    monkeypatch.setattr(settings, "R1B_TYPED_NUM_ITER_PER_ROUND", 2, raising=False)
    monkeypatch.setattr(settings, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 1.0, raising=False)

    fake_pipeline = SimpleNamespace()
    fake_pipeline.run_iteration = AsyncMock(side_effect=[
        _mk_iter_result(), _mk_iter_result(),
    ])
    fake_trace = SimpleNamespace(hist=[], add_experiment=lambda e, f: fake_trace.hist.append(e))
    fake_scenario = SimpleNamespace()
    fake_llm = SimpleNamespace(model="claude-haiku-4-5-20251001")

    with patch(
        "backend.agents.core.integration.create_alpha_pipeline",
        return_value=fake_pipeline,
    ), patch(
        "backend.agents.core.integration.create_scenario",
        return_value=fake_scenario,
    ), patch(
        "backend.agents.core.integration.create_trace",
        return_value=fake_trace,
    ), patch(
        "backend.agents.services.get_llm_service",
        return_value=fake_llm,
    ):
        out = await run_typed_round(task=_mk_task(), brain=None, db=None)

    assert out["skipped_disabled"] is False
    assert out["num_iter_executed"] == 2
    assert len(out["all_alphas"]) == 2
    assert out["all_alphas"][0]["alpha_id"] == "exp-1"
    assert out["abandoned"] is False


@pytest.mark.asyncio
async def test_run_typed_round_breaks_on_abandon_feedback(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    monkeypatch.setattr(settings, "R1B_TYPED_NUM_ITER_PER_ROUND", 5, raising=False)
    monkeypatch.setattr(settings, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 1.0, raising=False)

    fake_pipeline = SimpleNamespace()
    fake_pipeline.run_iteration = AsyncMock(side_effect=[
        _mk_iter_result(), _mk_iter_result(should_abandon=True),
        _mk_iter_result(),  # would-be 3rd, should NEVER run
    ])
    fake_trace = SimpleNamespace(hist=[], add_experiment=lambda e, f: fake_trace.hist.append(e))

    with patch(
        "backend.agents.core.integration.create_alpha_pipeline",
        return_value=fake_pipeline,
    ), patch(
        "backend.agents.core.integration.create_scenario",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.agents.core.integration.create_trace",
        return_value=fake_trace,
    ), patch(
        "backend.agents.services.get_llm_service",
        return_value=SimpleNamespace(model="m"),
    ):
        out = await run_typed_round(task=_mk_task(), brain=None, db=None)

    assert out["abandoned"] is True
    assert out["num_iter_executed"] == 2
    assert fake_pipeline.run_iteration.await_count == 2


@pytest.mark.asyncio
async def test_run_typed_round_soft_falls_on_pipeline_exception(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    monkeypatch.setattr(settings, "R1B_TYPED_NUM_ITER_PER_ROUND", 3, raising=False)

    fake_pipeline = SimpleNamespace()
    fake_pipeline.run_iteration = AsyncMock(side_effect=RuntimeError("LLM 500"))

    with patch(
        "backend.agents.core.integration.create_alpha_pipeline",
        return_value=fake_pipeline,
    ), patch(
        "backend.agents.core.integration.create_scenario",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.agents.core.integration.create_trace",
        return_value=SimpleNamespace(hist=[], add_experiment=lambda e, f: None),
    ), patch(
        "backend.agents.services.get_llm_service",
        return_value=SimpleNamespace(model="m"),
    ):
        try:
            out = await run_typed_round(task=_mk_task(), brain=None, db=None)
        except Exception as e:
            pytest.fail(f"run_typed_round must never raise; got {e}")
    assert out["num_iter_executed"] == 0


@pytest.mark.asyncio
async def test_run_typed_round_respects_budget_ceiling(monkeypatch):
    """When cost_usd accumulates past 3x per-alpha ceiling, loop breaks."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    monkeypatch.setattr(settings, "R1B_TYPED_NUM_ITER_PER_ROUND", 10, raising=False)
    # Tiny ceiling so even one iteration trips the cap on the next check
    monkeypatch.setattr(settings, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 0.001, raising=False)

    fake_pipeline = SimpleNamespace()
    fake_pipeline.run_iteration = AsyncMock(side_effect=[
        _mk_iter_result() for _ in range(10)
    ])
    with patch(
        "backend.agents.core.integration.create_alpha_pipeline",
        return_value=fake_pipeline,
    ), patch(
        "backend.agents.core.integration.create_scenario",
        return_value=SimpleNamespace(),
    ), patch(
        "backend.agents.core.integration.create_trace",
        return_value=SimpleNamespace(hist=[], add_experiment=lambda e, f: None),
    ), patch(
        "backend.agents.services.get_llm_service",
        return_value=SimpleNamespace(model="claude-haiku-4-5-20251001"),
    ):
        out = await run_typed_round(task=_mk_task(), brain=None, db=None)

    # Iteration 1 runs, accumulates cost; iteration 2's pre-check should break
    assert out["num_iter_executed"] == 1
    assert fake_pipeline.run_iteration.await_count == 1
