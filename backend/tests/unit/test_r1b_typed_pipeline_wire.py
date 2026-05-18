"""Phase 3 R1b.4b: typed pipeline mining_tasks wire tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §8.2.

R1b.4b wires the typed dispatcher into _run_one_round_inline via the
_maybe_run_typed_pipeline_round wrapper. These tests verify:
  - Returns None when typed inactive (legacy fall-through)
  - Returns None when run_typed_round signals skipped_disabled
  - Maps typed result shape to legacy round shape on success
  - Returns None on pipeline exception (graceful fallback per plan §8.6)
  - Returns 'skipped' dict when dataset has no fields
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _mk_task(*, hcv=3, config_extra=None, region="USA", universe="TOP3000"):
    cfg = {"hypothesis_centric_variant": hcv}
    if config_extra:
        cfg.update(config_extra)
    return SimpleNamespace(id=42, config=cfg, region=region, universe=universe)


# ---------------------------------------------------------------------------
# Dispatch behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wire_returns_none_when_typed_inactive(monkeypatch):
    """ENABLE_R1B_TYPED_PIPELINE=False → returns None so caller falls through."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", False, raising=False)
    from backend.tasks.mining_tasks import _maybe_run_typed_pipeline_round
    out = await _maybe_run_typed_pipeline_round(
        db=None, task=_mk_task(hcv=3), brain=None, operators=[],
        dataset_id="pv1",
    )
    assert out is None


@pytest.mark.asyncio
async def test_wire_returns_none_when_hcv_not_3(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    from backend.tasks.mining_tasks import _maybe_run_typed_pipeline_round
    out = await _maybe_run_typed_pipeline_round(
        db=None, task=_mk_task(hcv=2), brain=None, operators=[],
        dataset_id="pv1",
    )
    assert out is None


@pytest.mark.asyncio
async def test_wire_returns_skipped_when_no_fields(monkeypatch):
    """Active but dataset has no fields → returns 'skipped' dict (matches
    _run_one_round_inline's no-fields short-circuit shape)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    from backend.tasks import mining_tasks
    with patch.object(
        mining_tasks, "_prepare_round_fields",
        new=AsyncMock(return_value=None),
    ):
        out = await mining_tasks._maybe_run_typed_pipeline_round(
            db=None, task=_mk_task(hcv=3), brain=None, operators=[],
            dataset_id="pv1",
        )
    assert out == {"all_alphas": [], "iterations_completed": 0, "skipped": True}


@pytest.mark.asyncio
async def test_wire_maps_typed_result_to_legacy_shape(monkeypatch):
    """Successful typed round → mapped to legacy result shape."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    from backend.tasks import mining_tasks

    typed_result = {
        "skipped_disabled": False,
        "all_alphas": [{"alpha_id": "a1", "expression": "rank(close)"}],
        "num_iter_executed": 2,
        "abandoned": False,
        "cost_usd": 0.012,
        "trace_size": 2,
    }
    with patch.object(
        mining_tasks, "_prepare_round_fields",
        new=AsyncMock(return_value=[{"id": "close"}]),
    ), patch(
        "backend.agents.graph.nodes.r1b_typed_pipeline.run_typed_round",
        new=AsyncMock(return_value=typed_result),
    ):
        out = await mining_tasks._maybe_run_typed_pipeline_round(
            db=None, task=_mk_task(hcv=3), brain=None, operators=[{"name": "rank"}],
            dataset_id="pv1",
        )
    assert out is not None
    assert out["iterations_completed"] == 2
    assert len(out["all_alphas"]) == 1
    assert out["all_alphas"][0]["alpha_id"] == "a1"
    assert out["skipped"] is False
    # Typed telemetry under _r1b_typed_* keys
    assert out["_r1b_typed_cost_usd"] == 0.012
    assert out["_r1b_typed_trace_size"] == 2
    assert out["_r1b_typed_abandoned"] is False


@pytest.mark.asyncio
async def test_wire_returns_none_when_typed_signals_skipped(monkeypatch):
    """run_typed_round returning skipped_disabled=True → caller falls through."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    from backend.tasks import mining_tasks

    typed_result = {"skipped_disabled": True, "all_alphas": []}
    with patch.object(
        mining_tasks, "_prepare_round_fields",
        new=AsyncMock(return_value=[{"id": "close"}]),
    ), patch(
        "backend.agents.graph.nodes.r1b_typed_pipeline.run_typed_round",
        new=AsyncMock(return_value=typed_result),
    ):
        out = await mining_tasks._maybe_run_typed_pipeline_round(
            db=None, task=_mk_task(hcv=3), brain=None, operators=[],
            dataset_id="pv1",
        )
    assert out is None


@pytest.mark.asyncio
async def test_wire_soft_falls_on_pipeline_exception(monkeypatch):
    """run_typed_round raising → returns None → legacy path takes over.
    Mirrors plan §8.6 graceful-fallback contract."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    from backend.tasks import mining_tasks
    with patch.object(
        mining_tasks, "_prepare_round_fields",
        new=AsyncMock(return_value=[{"id": "close"}]),
    ), patch(
        "backend.agents.graph.nodes.r1b_typed_pipeline.run_typed_round",
        new=AsyncMock(side_effect=RuntimeError("pipeline blew up")),
    ):
        try:
            out = await mining_tasks._maybe_run_typed_pipeline_round(
                db=None, task=_mk_task(hcv=3), brain=None, operators=[],
                dataset_id="pv1",
            )
        except Exception as e:
            pytest.fail(f"wire must never raise; got {e}")
    assert out is None


@pytest.mark.asyncio
async def test_run_one_round_inline_calls_typed_wire_first(monkeypatch):
    """_run_one_round_inline tries the typed wire before invoking mining_agent."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_TYPED_PIPELINE", True, raising=False)
    from backend.tasks import mining_tasks

    typed_result_mapped = {
        "all_alphas": [{"alpha_id": "x"}], "iterations_completed": 1,
        "skipped": False, "_r1b_typed_cost_usd": 0.0,
    }
    fake_mining_agent = SimpleNamespace()
    fake_mining_agent.run_evolution_loop = AsyncMock(
        side_effect=AssertionError("legacy path should NOT run when typed active"),
    )
    with patch.object(
        mining_tasks, "_maybe_run_typed_pipeline_round",
        new=AsyncMock(return_value=typed_result_mapped),
    ):
        out = await mining_tasks._run_one_round_inline(
            db=None, task=_mk_task(hcv=3), run=None, brain=None,
            mining_agent=fake_mining_agent, operators=[],
            dataset_id="pv1",
        )
    assert out["iterations_completed"] == 1
    assert out["all_alphas"][0]["alpha_id"] == "x"
    # Crucially: legacy mining_agent.run_evolution_loop was never invoked
    fake_mining_agent.run_evolution_loop.assert_not_awaited()
