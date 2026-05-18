"""Phase 3 R1b.1b: node_code_gen_retry + prompt unit tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §3.6.

PR1b ships the retry node + prompt module. These tests verify:
  - Prompt build template renders cleanly with defensive defaults
  - node_code_gen_retry triggers only on FAIL+IMPLEMENTATION attribution
  - Per-alpha budget guard (self-check pattern V-26.57)
  - Token cost ceiling enforcement
  - LLM failure soft-fall (single failure does NOT break round)
  - Same-expression-returned counts as no-op
  - Rewrite preserves original_expression + resets validation state
  - _r1b_retry_chain accumulates in metrics

PR1c (router) and PR1d (12 tests + GO gate) come next.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.graph.nodes.r1b_loop import (
    _estimate_cost,
    node_code_gen_retry,
)
from backend.agents.prompts.r1b_retry import build_r1b_retry_prompt


# ---------------------------------------------------------------------------
# Test helpers — minimal AlphaCandidate + MiningState shapes
# ---------------------------------------------------------------------------


class _FakeAlpha(SimpleNamespace):
    """Pydantic-compatible enough for the node — supports model_copy."""
    def model_copy(self):
        clone = _FakeAlpha(**self.__dict__)
        clone.metrics = dict(self.metrics or {})
        return clone


def _mk_alpha(idx_str: str, expression: str, *,
              quality_status: str = "FAIL",
              attribution: str = "implementation",
              hypothesis: str = "momentum"):
    return _FakeAlpha(
        alpha_id=f"alpha-{idx_str}",
        expression=expression,
        original_expression=None,
        is_valid=True,
        validation_error=None,
        is_simulated=True,
        simulation_success=False,
        quality_status=quality_status,
        hypothesis=hypothesis,
        metrics={
            "_r1a_attribution": attribution,
            "_r1a_attribution_evidence": ["sharpe too low", "wrong window"],
            "_r5_c2_reason": "expression uses raw close but hypothesis is rank-based",
            "sharpe": 0.1, "fitness": 0.2, "turnover": 0.4,
        },
    )


def _mk_state(alphas, *, retries=0, mutations=0, cost=0.0):
    return SimpleNamespace(
        pending_alphas=alphas,
        fields=[{"id": "close"}, {"id": "open"}, {"id": "volume"}],
        region="USA",
        task_id=42,
        round_idx=1,
        r1b_retries_attempted_this_alpha=retries,
        r1b_mutations_attempted_this_cycle=mutations,
        r1b_token_cost_this_alpha=cost,
    )


def _mk_llm(response_dict, *, success=True, tokens=200):
    """Build an LLMService mock returning a given parsed dict."""
    resp = SimpleNamespace(
        success=success, parsed=response_dict,
        content="(unused)", tokens_used=tokens,
    )
    svc = SimpleNamespace(model="claude-haiku-4-5-20251001")
    svc.call = AsyncMock(return_value=resp)
    return svc


@pytest.fixture(autouse=True)
def _patch_log_writer():
    """Suppress real DB writes in the retry node throughout this file."""
    with patch(
        "backend.agents.graph.nodes.r1b_loop._write_r1b_retry_log_rows",
        new=AsyncMock(return_value=None),
    ):
        yield


# ---------------------------------------------------------------------------
# prompt module
# ---------------------------------------------------------------------------


def test_build_r1b_retry_prompt_renders_all_sections():
    sys_p, user_p = build_r1b_retry_prompt(
        original_expression="rank(close)",
        original_hypothesis="momentum thesis",
        failure_metrics={"sharpe": 0.1, "fitness": 0.0, "turnover": 0.5},
        r1a_evidence=["evidence 1", "evidence 2"],
        r5_c2_reason="bad neutralization",
        allowed_fields=["close", "open", "volume"],
    )
    assert "quantitative alpha engineer" in sys_p
    assert "rank(close)" in user_p
    assert "momentum thesis" in user_p
    assert "evidence 1" in user_p
    assert "bad neutralization" in user_p
    assert "close, open, volume" in user_p
    assert "fixed_expression" in user_p


def test_build_r1b_retry_prompt_handles_missing_inputs():
    sys_p, user_p = build_r1b_retry_prompt(
        original_expression="",
        original_hypothesis="",
        failure_metrics={},
        r1a_evidence=[],
        r5_c2_reason="",
        allowed_fields=[],
    )
    assert "<EMPTY>" in user_p
    assert "(no hypothesis recorded)" in user_p
    assert "(no heuristic evidence recorded)" in user_p
    assert "(no R5 c2 reason recorded" in user_p
    assert "BRAIN OHLCV defaults" in user_p


# ---------------------------------------------------------------------------
# node_code_gen_retry — trigger gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_triggers_on_implementation_attribution():
    alpha = _mk_alpha("0", "rank(close)")  # FAIL + implementation
    state = _mk_state([alpha])
    llm = _mk_llm({"fixed_expression": "rank(close - open)", "changes_made": "neutralize"})
    out = await node_code_gen_retry(state, llm)
    assert "pending_alphas" in out
    assert out["pending_alphas"][0].expression == "rank(close - open)"
    assert out["pending_alphas"][0].original_expression == "rank(close)"
    assert out["pending_alphas"][0].quality_status == "PENDING"
    assert out["pending_alphas"][0].is_valid is None
    assert out["pending_alphas"][0].is_simulated is False
    assert out["pending_alphas"][0].metrics["_r1b_retry_chain"] == ["rank(close)"]
    assert out["pending_alphas"][0].metrics["_r1b_retry_reason"] == "neutralize"
    assert out["r1b_retries_attempted_this_alpha"] == 1


@pytest.mark.asyncio
async def test_retry_skips_unknown_attribution():
    alpha = _mk_alpha("0", "rank(close)", attribution="unknown")
    state = _mk_state([alpha])
    llm = _mk_llm({"fixed_expression": "x"})
    out = await node_code_gen_retry(state, llm)
    assert out == {}
    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_skips_when_quality_not_fail():
    alpha = _mk_alpha("0", "rank(close)", quality_status="PASS")
    state = _mk_state([alpha])
    llm = _mk_llm({"fixed_expression": "x"})
    out = await node_code_gen_retry(state, llm)
    assert out == {}
    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_handles_both_attribution():
    """BOTH attribution → implementation retry still runs (mutate dominance
    happens at router level, not node level; node sees what router sent)."""
    alpha = _mk_alpha("0", "rank(close)", attribution="both")
    state = _mk_state([alpha])
    llm = _mk_llm({"fixed_expression": "rank(close - open)", "changes_made": "x"})
    out = await node_code_gen_retry(state, llm)
    assert out["pending_alphas"][0].expression == "rank(close - open)"


# ---------------------------------------------------------------------------
# Budget guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_budget_exhausted_returns_early():
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha], retries=3)  # default max=3
    llm = _mk_llm({"fixed_expression": "x"})
    out = await node_code_gen_retry(state, llm)
    assert out == {"r1b_retries_attempted_this_alpha": 3}
    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_token_cost_ceiling_returns_early():
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha], cost=0.10)  # over default ceiling 0.05
    llm = _mk_llm({"fixed_expression": "x"})
    out = await node_code_gen_retry(state, llm)
    assert out == {"r1b_token_cost_this_alpha": 0.10}
    llm.call.assert_not_awaited()


# ---------------------------------------------------------------------------
# Soft-fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_llm_call_exception_soft_falls():
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha])
    svc = SimpleNamespace(model="claude-haiku-4-5-20251001")
    svc.call = AsyncMock(side_effect=RuntimeError("boom"))
    out = await node_code_gen_retry(state, svc)
    # Alpha not rewritten but the budget counter bumps so router won't loop
    assert out["pending_alphas"][0].expression == "rank(close)"
    assert out["r1b_retries_attempted_this_alpha"] == 1


@pytest.mark.asyncio
async def test_retry_same_expression_returned_is_noop():
    """LLM returns identical expression → not rewritten + retry counted."""
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha])
    llm = _mk_llm({"fixed_expression": "rank(close)", "changes_made": "n/a"})
    out = await node_code_gen_retry(state, llm)
    assert out["pending_alphas"][0].expression == "rank(close)"
    assert "_r1b_retry_chain" not in (out["pending_alphas"][0].metrics or {})
    assert out["r1b_retries_attempted_this_alpha"] == 1


@pytest.mark.asyncio
async def test_retry_empty_expression_returned_is_noop():
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha])
    llm = _mk_llm({"fixed_expression": "   ", "changes_made": "n/a"})
    out = await node_code_gen_retry(state, llm)
    assert out["pending_alphas"][0].expression == "rank(close)"


# ---------------------------------------------------------------------------
# _estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_zero_tokens_returns_zero():
    assert _estimate_cost("claude-haiku-4-5", 0) == 0.0
    assert _estimate_cost("claude-haiku-4-5", -5) == 0.0


def test_estimate_cost_haiku_rate_split():
    # 1000 tokens × (0.30·$0.001/k + 0.70·$0.005/k) = $0.0038
    cost = _estimate_cost("claude-haiku-4-5", 1000)
    assert abs(cost - 0.0038) < 1e-6


def test_estimate_cost_unknown_model_uses_default():
    cost = _estimate_cost("unknown-model-x", 1000)
    # Defaults: 0.30·0.001 + 0.70·0.005 = 0.0038 (matches haiku coincidentally)
    assert cost > 0
