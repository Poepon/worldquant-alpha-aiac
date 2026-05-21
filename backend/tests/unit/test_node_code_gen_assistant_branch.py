"""Phase 4 Sprint 1 A1.3 — node_code_gen assistant-mode branching tests.

These tests replicate the per-alpha branching logic in
``backend.agents.graph.nodes.generation.node_code_gen`` (the loop
inside ``for alpha_data in raw_alphas:``) WITHOUT instantiating the
full LangGraph node (which depends on RAG service, DB, prompt
builders, and the LLM service — too brittle for a unit test).

The branching itself is straight-line if/else; the heavy lifting
lives in ``backend.services.assistant_template.compose_for_hypothesis``
which has its own dedicated tests. These tests pin:

  1. assistant mode + matching hypothesis → expression IS overridden
  2. author mode → expression IS NOT touched (byte-identical fallthrough)
  3. assistant mode + no template match → soft-fall to LLM's expression
  4. assistant mode metadata stamps applied correctly
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Replication of the node_code_gen per-alpha branching logic.
# Keep this in sync with generation.py:_assistant_compose block.
# ---------------------------------------------------------------------------


def _replay_per_alpha_branch(
    *,
    llm_mode_used: str,
    alpha_data: dict,
) -> dict:
    """Replays the assistant-mode branch from node_code_gen.

    Returns the would-be ``candidate.metadata`` dict + final expression
    so the test can assert against both. Does NOT touch the LangGraph
    node — purely exercises the decision logic.
    """
    from backend.services.assistant_template import compose_for_hypothesis

    assistant_mode_active = (llm_mode_used == "assistant")
    hypothesis_text = alpha_data.get(
        "hypothesis_tested", alpha_data.get("hypothesis", "")
    )

    composed_expression = None
    composed_template_id = None
    composed_score = None
    if assistant_mode_active and hypothesis_text:
        pillar_hint = alpha_data.get("pillar") or alpha_data.get("pillar_choice")
        composed = compose_for_hypothesis(
            hypothesis_text,
            pillar=pillar_hint if isinstance(pillar_hint, str) else None,
        )
        if composed is not None and composed.get("expression"):
            composed_expression = composed["expression"]
            composed_template_id = composed.get("template_id")
            composed_score = composed.get("score")

    final_expression = (
        composed_expression
        if composed_expression is not None
        else alpha_data.get("expression", "")
    )
    # F1 fix: legacy in-round metadata (NOT persisted)
    metadata: dict = {
        "fields_used": alpha_data.get("fields_used", []),
        "complexity": alpha_data.get("complexity", "unknown"),
        "novelty_level": alpha_data.get("novelty_level", "unknown"),
    }
    # F1 fix: A1.3 assistant stamps land on candidate.metrics (persisted
    # via evaluation.py:1278 setdefault merge), NOT metadata.
    metrics: dict = {}
    if assistant_mode_active:
        metrics["llm_mode_used"] = "assistant"
        if composed_expression is not None:
            metrics["assistant_template_id"] = composed_template_id
            metrics["assistant_template_score"] = composed_score
            metrics["assistant_template_fallthrough"] = False
        else:
            metrics["assistant_template_fallthrough"] = True

    return {"expression": final_expression, "metadata": metadata, "metrics": metrics}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_assistant_mode_overrides_llm_expression():
    """state.llm_mode_used='assistant' + hypothesis matches momentum
    template → expression is REPLACED by composed DSL.
    F1 fix (post-S1-A): stamps land on candidate.metrics (persisted),
    NOT candidate.metadata (transient)."""
    result = _replay_per_alpha_branch(
        llm_mode_used="assistant",
        alpha_data={
            "expression": "definitely_should_be_overridden(x)",
            "hypothesis": "ts_zscore momentum persistence on recent returns",
        },
    )
    assert result["expression"] != "definitely_should_be_overridden(x)"
    assert "ts_zscore" in result["expression"]
    # F1: stamps on metrics, not metadata
    assert result["metrics"]["llm_mode_used"] == "assistant"
    assert result["metrics"]["assistant_template_fallthrough"] is False
    assert result["metrics"]["assistant_template_id"]
    assert result["metrics"]["assistant_template_score"] > 0
    # metadata 不持有 A1.3 stamps (legacy only)
    assert "llm_mode_used" not in result["metadata"]
    assert "assistant_template_id" not in result["metadata"]


def test_author_mode_byte_identical_to_llm_expression():
    """state.llm_mode_used='author' → expression NOT touched + no
    assistant-mode stamps on metrics or metadata."""
    result = _replay_per_alpha_branch(
        llm_mode_used="author",
        alpha_data={
            "expression": "rank(ts_corr(close, volume, 20))",
            "hypothesis": "ts_zscore momentum persistence on returns",
        },
    )
    assert result["expression"] == "rank(ts_corr(close, volume, 20))"
    # No assistant-mode stamps in author mode
    assert "llm_mode_used" not in result["metrics"]
    assert "assistant_template_id" not in result["metrics"]
    assert "llm_mode_used" not in result["metadata"]


def test_assistant_mode_no_match_falls_through_to_llm_expression():
    """assistant mode + hypothesis with zero overlap → expression stays
    LLM's; metrics records the fallthrough."""
    result = _replay_per_alpha_branch(
        llm_mode_used="assistant",
        alpha_data={
            "expression": "rank(ts_delta(close, 10))",
            "hypothesis": "completely unrelated xyzzy plover frobnication",
        },
    )
    assert result["expression"] == "rank(ts_delta(close, 10))"
    assert result["metrics"]["llm_mode_used"] == "assistant"
    assert result["metrics"]["assistant_template_fallthrough"] is True
    # The template_id field must be ABSENT when fallthrough True
    assert "assistant_template_id" not in result["metrics"]


def test_assistant_mode_with_pillar_hint_prefers_that_pillar():
    """alpha_data['pillar']='value' restricts template to value pillar."""
    result = _replay_per_alpha_branch(
        llm_mode_used="assistant",
        alpha_data={
            "expression": "fallback_expr",
            "hypothesis": "rank book-to-market cheapness",
            "pillar": "value",
        },
    )
    assert "rank" in result["expression"] or "group_neutralize" in result["expression"]
    assert "book_to_market" in result["expression"]
    assert result["metrics"]["assistant_template_fallthrough"] is False


def test_assistant_mode_empty_hypothesis_falls_through():
    """alpha_data.hypothesis is empty → no compose attempt → fallthrough."""
    result = _replay_per_alpha_branch(
        llm_mode_used="assistant",
        alpha_data={"expression": "rank(close)", "hypothesis": ""},
    )
    assert result["expression"] == "rank(close)"
    assert result["metrics"]["assistant_template_fallthrough"] is True


def test_assistant_mode_handles_hypothesis_tested_alias():
    """LLM may emit hypothesis_tested instead of hypothesis (legacy
    field). The composer should still match."""
    result = _replay_per_alpha_branch(
        llm_mode_used="assistant",
        alpha_data={
            "expression": "fallback",
            "hypothesis_tested": "industry neutral momentum residual",
        },
    )
    assert "group_neutralize" in result["expression"]
    assert result["metrics"]["assistant_template_fallthrough"] is False
