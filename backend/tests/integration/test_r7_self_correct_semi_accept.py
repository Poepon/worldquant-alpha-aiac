"""Integration: Phase 2 R7 Co-STEER self-correct 半接受 (2026-05-18).

Tests per master plan §4.4 R7 semantics:
  1. Flag OFF byte-equivalence — legacy "always overwrite + retry"
  2. Flag ON + LLM fix produces VALID expression → accept (overwrite)
  3. Flag ON + LLM fix still INVALID + strictly fewer errors → accept
  4. Flag ON + LLM fix still INVALID + same/more errors → REJECT (keep original)
  5. Flag ON + R7 re-validate raises → fall back to accept (graceful)
  6. Rejected fix does NOT pollute correction KB (record_correction skipped)
  7. _r7_self_correct_rejected metadata correctly stamped

Uses mock LLM + mock semantic validator. Verifies flow at integration level.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.config import _flag_override_cache


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def _make_state(invalid_expr: str = "BROKEN(x", validation_error: str = "syntax error",
                metrics: dict = None) -> MiningState:
    """Build a minimal MiningState with 1 invalid alpha ready for self_correct."""
    alpha = AlphaCandidate(
        expression=invalid_expr,
        hypothesis="test hypothesis",
        explanation="test explanation",
        is_valid=False,
        validation_error=validation_error,
        metrics=metrics or {},
    )
    return MiningState(
        task_id=99999,
        region="USA",
        universe="TOP3000",
        dataset_id="pv1",
        fields=[{"id": "close", "name": "close"}, {"id": "volume", "name": "volume"}],
        patterns=[],
        pending_alphas=[alpha],
        current_alpha_index=0,
        retry_count=0,
        max_retries=3,
        current_round=1,
        num_alphas_target=4,
    )


def _mock_llm_response(fixed_expr: str):
    """Build a mock LLM that returns a parsed self-correct response."""
    mock = MagicMock()
    mock.call = AsyncMock(return_value=SimpleNamespace(
        success=True,
        parsed={
            "fix": {"fixed_expression": fixed_expr, "changes_made": "fixed"},
            "knowledge_extracted": {"rule": "test"},
        },
        content=f'{{"fix": {{"fixed_expression": "{fixed_expr}"}}}}',
    ))
    return mock


# ---------------------------------------------------------------------------
# Test 1: Flag OFF — legacy "always overwrite"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_off_always_overwrites_legacy_behavior(monkeypatch):
    """Flag OFF: LLM fix always overwrites expression regardless of validity."""
    from backend.agents.graph.nodes.validation import node_self_correct
    state = _make_state(invalid_expr="BROKEN(x", validation_error="paren error")
    fixed = "rank(close)"
    llm = _mock_llm_response(fixed)

    # Stub _record_correction so we don't hit Redis
    with patch("backend.agents.graph.nodes.validation._record_correction"):
        result = await node_self_correct(state, llm, config={"configurable": {"trace_service": None}})

    # node_self_correct returns updated state via dict (LangGraph pattern)
    updated = result["pending_alphas"][0]
    assert updated.expression == fixed
    assert "_r7_self_correct_rejected" not in (updated.metrics or {})


# ---------------------------------------------------------------------------
# Test 2: Flag ON + new VALID → accept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_on_new_valid_accepted(monkeypatch):
    """Flag ON: new expression VALID after R7 re-validate → accept overwrite."""
    _flag_override_cache["ENABLE_SELF_CORRECT_SEMI_ACCEPT"] = True
    from backend.agents.graph.nodes.validation import node_self_correct

    state = _make_state(invalid_expr="BROKEN(x", validation_error="paren")
    fixed = "rank(close)"
    llm = _mock_llm_response(fixed)

    with patch("backend.agents.graph.nodes.validation._record_correction"), \
         patch("backend.alpha_semantic_validator.validate_alpha_semantically",
               return_value={"valid": True, "errors": []}):
        result = await node_self_correct(state, llm, config={"configurable": {"trace_service": None}})

    updated = result["pending_alphas"][0]
    assert updated.expression == fixed
    assert "_r7_self_correct_rejected" not in (updated.metrics or {})


# ---------------------------------------------------------------------------
# Test 3: Flag ON + new INVALID + strictly fewer errs → accept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_on_strictly_fewer_errs_accepted(monkeypatch):
    """Flag ON: new invalid but error count strictly lower than original → accept."""
    _flag_override_cache["ENABLE_SELF_CORRECT_SEMI_ACCEPT"] = True
    from backend.agents.graph.nodes.validation import node_self_correct

    # Original has 3 hard findings
    metrics = {"_validation_findings": [
        {"severity": "hard", "rule_id": "x"},
        {"severity": "hard", "rule_id": "y"},
        {"severity": "hard", "rule_id": "z"},
    ]}
    state = _make_state(invalid_expr="bad1(bad2(bad3))", metrics=metrics)
    fixed = "almost_rank(close)"
    llm = _mock_llm_response(fixed)

    # New has 1 error → 1 < 3, accept
    with patch("backend.agents.graph.nodes.validation._record_correction"), \
         patch("backend.alpha_semantic_validator.validate_alpha_semantically",
               return_value={"valid": False, "errors": ["one error"]}):
        result = await node_self_correct(state, llm, config={"configurable": {"trace_service": None}})

    updated = result["pending_alphas"][0]
    assert updated.expression == fixed


# ---------------------------------------------------------------------------
# Test 4: Flag ON + new INVALID + same/more errs → REJECT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_on_same_or_more_errs_rejected(monkeypatch):
    """Flag ON: new invalid AND errs not strictly fewer → REJECT keep original."""
    _flag_override_cache["ENABLE_SELF_CORRECT_SEMI_ACCEPT"] = True
    from backend.agents.graph.nodes.validation import node_self_correct

    # Original has 1 hard finding
    metrics = {"_validation_findings": [{"severity": "hard", "rule_id": "x"}]}
    original_expr = "BROKEN(x"
    state = _make_state(invalid_expr=original_expr, metrics=metrics)
    fixed = "ALSO_BROKEN(y"
    llm = _mock_llm_response(fixed)

    # New also has 1 error → not strictly fewer, REJECT
    with patch("backend.agents.graph.nodes.validation._record_correction") as mock_rec, \
         patch("backend.alpha_semantic_validator.validate_alpha_semantically",
               return_value={"valid": False, "errors": ["one"]}):
        result = await node_self_correct(state, llm, config={"configurable": {"trace_service": None}})

    updated = result["pending_alphas"][0]
    # Original expression preserved
    assert updated.expression == original_expr
    # Reject marker stamped
    assert updated.metrics.get("_r7_self_correct_rejected") is True
    assert "rejected" in updated.metrics.get("_r7_self_correct_reason", "")
    assert updated.metrics.get("_r7_rejected_candidate") == fixed
    # _record_correction NOT called (rejected fixes don't pollute KB)
    mock_rec.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: R7 re-validate raises → graceful accept (fallback to legacy)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_on_revalidate_exception_falls_back_to_accept(monkeypatch):
    """R7 validator throws → don't block correction, default-accept with reason."""
    _flag_override_cache["ENABLE_SELF_CORRECT_SEMI_ACCEPT"] = True
    from backend.agents.graph.nodes.validation import node_self_correct

    state = _make_state(invalid_expr="BROKEN(x")
    fixed = "rank(close)"
    llm = _mock_llm_response(fixed)

    with patch("backend.agents.graph.nodes.validation._record_correction"), \
         patch("backend.alpha_semantic_validator.validate_alpha_semantically",
               side_effect=RuntimeError("validator crashed")):
        result = await node_self_correct(state, llm, config={"configurable": {"trace_service": None}})

    updated = result["pending_alphas"][0]
    # Defaults to accept on exception
    assert updated.expression == fixed
    assert "_r7_self_correct_rejected" not in (updated.metrics or {})
