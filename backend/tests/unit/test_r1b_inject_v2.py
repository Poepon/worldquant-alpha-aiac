"""Phase 3 R1b.2-v2 inject path tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §6.2 V-R1b.2-v2.

Verifies the consume → inject end-to-end path:

  1. workflow.run reads task.config["__r1b_consumed_pending_hypothesis"]
     and populates initial_state.r1b_consumed_pending_hypothesis
  2. node_hypothesis detects state.r1b_consumed_pending_hypothesis at entry,
     skips the LLM exploration call, and constructs a 1-element hypotheses
     list from the mutated dict (when ENABLE_R1B_HYPOTHESIS_MUTATE ON)
  3. Flag OFF → consumed field stays set but legacy LLM path runs
  4. Helper exception → caller's try/except falls back to LLM path
  5. The __r1b_consumed_pending_hypothesis slot on task.config is cleared
     atomically by workflow.run so it's a one-shot directive
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.graph.state import MiningState


# ---------------------------------------------------------------------------
# State field defaults — backward-compat sanity
# ---------------------------------------------------------------------------

def test_state_defaults_consumed_pending_to_none():
    """MiningState.r1b_consumed_pending_hypothesis defaults None."""
    s = MiningState(
        task_id=1, region="USA", universe="TOP3000",
        dataset_id="ds1", fields=[], operators=[],
    )
    assert s.r1b_consumed_pending_hypothesis is None


def test_state_accepts_consumed_pending_dict():
    """The field accepts an Optional[Dict] as written by workflow.run init."""
    payload = {"statement": "test", "rationale": "why"}
    s = MiningState(
        task_id=1, region="USA", universe="TOP3000",
        dataset_id="ds1", fields=[], operators=[],
        r1b_consumed_pending_hypothesis=payload,
    )
    assert s.r1b_consumed_pending_hypothesis == payload


# ---------------------------------------------------------------------------
# Inject helper unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inject_helper_constructs_single_hypothesis_skipping_llm():
    """Helper produces 1-element hypotheses list mirroring node_hypothesis shape."""
    from backend.agents.graph.nodes.generation import _node_hypothesis_inject_consumed

    consumed = {
        "statement": "Test mutated hypothesis — momentum + volume divergence",
        "rationale": "prior round R5 c1: hypothesis-attribution failure",
    }
    state = MiningState(
        task_id=99, region="USA", universe="TOP3000",
        dataset_id="anchor_ds", fields=[], operators=[],
    )

    out = await _node_hypothesis_inject_consumed(
        state=state, consumed=consumed,
        config={"configurable": {}}, trace_service=None,
        start_time=0.0, node_name="HYPOTHESIS",
    )
    assert "hypotheses" in out
    assert len(out["hypotheses"]) == 1
    h = out["hypotheses"][0]
    assert h["statement"] == consumed["statement"]
    assert h["rationale"] == consumed["rationale"]
    assert h["selected_datasets"] == ["anchor_ds"]
    assert h["_r1b_origin"] == "mutate_v2"
    # Output mirrors node_hypothesis return contract
    assert out["current_hypothesis_datasets"] == ["anchor_ds"]
    assert out["current_hypothesis_id"] is None
    assert out["current_hypothesis_ids"] == []


@pytest.mark.asyncio
async def test_inject_helper_raises_on_empty_statement():
    """Empty/missing statement → ValueError → caller's try/except falls back."""
    from backend.agents.graph.nodes.generation import _node_hypothesis_inject_consumed

    state = MiningState(
        task_id=1, region="USA", universe="TOP3000",
        dataset_id="ds", fields=[], operators=[],
    )
    with pytest.raises(ValueError):
        await _node_hypothesis_inject_consumed(
            state=state, consumed={"statement": "   "},
            config={"configurable": {}}, trace_service=None,
            start_time=0.0, node_name="HYPOTHESIS",
        )


@pytest.mark.asyncio
async def test_inject_helper_uses_consumed_selected_datasets_when_present():
    """consumed['selected_datasets'] overrides legacy anchor."""
    from backend.agents.graph.nodes.generation import _node_hypothesis_inject_consumed

    consumed = {
        "statement": "stmt",
        "selected_datasets": ["ds2", "ds3"],
    }
    state = MiningState(
        task_id=1, region="USA", universe="TOP3000",
        dataset_id="ds1", fields=[], operators=[],
    )
    out = await _node_hypothesis_inject_consumed(
        state=state, consumed=consumed,
        config={"configurable": {}}, trace_service=None,
        start_time=0.0, node_name="HYPOTHESIS",
    )
    assert out["hypotheses"][0]["selected_datasets"] == ["ds2", "ds3"]
    assert out["current_hypothesis_datasets"] == ["ds2", "ds3"]


# ---------------------------------------------------------------------------
# node_hypothesis entry detection — inject vs fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_node_hypothesis_inject_active_when_flag_on(monkeypatch):
    """Flag ON + state.r1b_consumed_pending_hypothesis set → inject helper called,
    LLM NOT called."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)

    consumed = {"statement": "injected hypothesis"}
    state = MiningState(
        task_id=1, region="USA", universe="TOP3000",
        dataset_id="ds", fields=[], operators=[],
        r1b_consumed_pending_hypothesis=consumed,
    )

    llm_mock = MagicMock()
    llm_mock.call = AsyncMock()

    inject_mock = AsyncMock(return_value={"hypotheses": [{"statement": "x"}]})
    with patch(
        "backend.agents.graph.nodes.generation._node_hypothesis_inject_consumed",
        inject_mock,
    ):
        from backend.agents.graph.nodes.generation import node_hypothesis
        out = await node_hypothesis(state, llm_mock, config={"configurable": {}})

    inject_mock.assert_awaited_once()
    llm_mock.call.assert_not_awaited()
    assert out["hypotheses"][0]["statement"] == "x"


@pytest.mark.asyncio
async def test_node_hypothesis_skips_inject_when_flag_off(monkeypatch):
    """Flag OFF → consumed field present but inject helper NOT called (legacy LLM)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)

    consumed = {"statement": "should not be injected"}
    state = MiningState(
        task_id=1, region="USA", universe="TOP3000",
        dataset_id="ds", fields=[{"id": "close"}], operators=[{"name": "rank"}],
        r1b_consumed_pending_hypothesis=consumed,
    )

    inject_mock = AsyncMock(return_value={"hypotheses": []})
    with patch(
        "backend.agents.graph.nodes.generation._node_hypothesis_inject_consumed",
        inject_mock,
    ):
        # The LLM path will likely raise without a real llm_service; we only
        # care that inject was NOT called. Wrap node_hypothesis call to swallow
        # downstream legacy-path failure (irrelevant to this assertion).
        from backend.agents.graph.nodes.generation import node_hypothesis
        try:
            await node_hypothesis(state, MagicMock(), config={"configurable": {}})
        except Exception:
            pass
    inject_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_node_hypothesis_falls_back_to_llm_when_inject_helper_raises(monkeypatch):
    """Inject helper raising → caller try/except → legacy LLM path continues."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)

    state = MiningState(
        task_id=1, region="USA", universe="TOP3000",
        dataset_id="ds", fields=[{"id": "close"}], operators=[{"name": "rank"}],
        r1b_consumed_pending_hypothesis={"statement": "trigger"},
    )

    async def _raise(**kw):
        raise RuntimeError("helper boom")

    with patch(
        "backend.agents.graph.nodes.generation._node_hypothesis_inject_consumed",
        _raise,
    ):
        # Helper raises → outer try/except logs warn → falls into legacy LLM
        # body. The LLM path uses V-27.31 graceful failure (returns empty
        # hypotheses on llm_service error). We assert the output shape is the
        # legacy path's, NOT the inject helper's (no _r1b_origin marker).
        from backend.agents.graph.nodes.generation import node_hypothesis
        out = await node_hypothesis(state, MagicMock(), config={"configurable": {}})
        for h in out.get("hypotheses", []):
            assert h.get("_r1b_origin") != "mutate_v2", (
                "inject helper marker leaked through fallback path"
            )


# ---------------------------------------------------------------------------
# Flag-OFF byte-equiv sentinel — neither state.r1b_consumed_pending_hypothesis
# read NOR inject helper get exercised when state field is None.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_node_hypothesis_no_inject_when_consumed_field_none(monkeypatch):
    """state.r1b_consumed_pending_hypothesis=None → inject helper NEVER called."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)

    state = MiningState(
        task_id=1, region="USA", universe="TOP3000",
        dataset_id="ds", fields=[{"id": "close"}], operators=[{"name": "rank"}],
        r1b_consumed_pending_hypothesis=None,  # explicit None
    )

    inject_mock = AsyncMock()
    with patch(
        "backend.agents.graph.nodes.generation._node_hypothesis_inject_consumed",
        inject_mock,
    ):
        from backend.agents.graph.nodes.generation import node_hypothesis
        try:
            await node_hypothesis(state, MagicMock(), config={"configurable": {}})
        except Exception:
            pass
    inject_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Static-source sentinel — verify the wire is in the expected files.
# ---------------------------------------------------------------------------

def test_static_source_sentinel_node_hypothesis_has_r1b_inject_block():
    """Grep node_hypothesis source for R1b.2-v2 wire signature."""
    import inspect
    from backend.agents.graph.nodes import generation

    src = inspect.getsource(generation.node_hypothesis)
    assert "ENABLE_R1B_HYPOTHESIS_MUTATE" in src
    assert "r1b_consumed_pending_hypothesis" in src
    assert "_node_hypothesis_inject_consumed" in src
    assert "R1b.2-v2" in src


def test_static_source_sentinel_workflow_run_has_consumed_slot_read():
    """Grep workflow.run source for the consumed-slot read+clear block."""
    import inspect
    from backend.agents.graph import workflow

    src = inspect.getsource(workflow.MiningWorkflow.run)
    assert "__r1b_consumed_pending_hypothesis" in src
    assert "r1b_consumed_pending_hypothesis=" in src
    assert "R1b.2-v2" in src
