"""Phase 3 R1b.2a: node_hypothesis_mutate + mutate prompt unit tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §4.

R1b.2 mutate sub-phase first PR. These tests verify:
  - Mutate prompt renders all sections + defensive defaults
  - Mutate triggers only on FAIL+(hypothesis|both) attribution
  - Dataset-cycle-scoped dedupe (1 LLM call per unique hypothesis,
    picks highest-impact group)
  - Per-cycle mutation budget + token cost ceiling guards
  - LLM soft-fall — counter still bumps to prevent router loop
  - Empty/same statement → no pending_new_hypothesis emission
  - pending_new_hypothesis payload structure
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.graph.nodes.r1b_loop import node_hypothesis_mutate
from backend.agents.prompts.r1b_mutate import build_r1b_mutate_prompt


class _FakeAlpha(SimpleNamespace):
    def model_copy(self):
        clone = _FakeAlpha(**self.__dict__)
        clone.metrics = dict(self.metrics or {})
        return clone


def _mk_alpha(idx_str, expression, *, attribution="hypothesis",
              hypothesis="momentum in low-vol stocks", quality_status="FAIL"):
    return _FakeAlpha(
        alpha_id=f"alpha-{idx_str}",
        expression=expression,
        is_valid=True,
        is_simulated=True,
        simulation_success=False,
        quality_status=quality_status,
        hypothesis=hypothesis,
        metrics={
            "_r1a_attribution": attribution,
            "_r5_c1_reason": "hypothesis doesn't actually predict the signal",
            "sharpe": 0.1, "fitness": 0.0, "turnover": 0.5,
        },
    )


def _mk_state(alphas, *, mutations=0, cost=0.0, dataset_id="us_pv13"):
    return SimpleNamespace(
        pending_alphas=alphas,
        fields=[{"id": "close"}],
        region="USA",
        task_id=42,
        round_idx=1,
        dataset_id=dataset_id,
        current_pillar="momentum",
        r1b_retries_attempted_this_alpha=0,
        r1b_mutations_attempted_this_cycle=mutations,
        r1b_token_cost_this_alpha=cost,
    )


def _mk_llm_mutate_resp(new_statement, *, success=True, tokens=200,
                        rationale="", diff=""):
    parsed = {
        "new_hypothesis": {
            "statement": new_statement,
            "rationale": rationale,
            "expected_signal": "momentum",
            "key_fields": ["close", "volume"],
            "suggested_operators": ["ts_rank", "ts_mean"],
        },
        "diff_from_original": diff,
        "addresses_failure_modes": ["wrong-sign"],
    }
    resp = SimpleNamespace(
        success=success, parsed=parsed, content="(unused)", tokens_used=tokens,
    )
    svc = SimpleNamespace(model="claude-haiku-4-5-20251001")
    svc.call = AsyncMock(return_value=resp)
    return svc


@pytest.fixture(autouse=True)
def _patch_log_writer():
    with patch(
        "backend.agents.graph.nodes.r1b_loop._write_r1b_retry_log_rows",
        new=AsyncMock(return_value=None),
    ):
        yield


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_build_mutate_prompt_renders_all_sections():
    sys_p, user_p = build_r1b_mutate_prompt(
        original_hypothesis="momentum signals work in low-vol stocks",
        original_alpha_outcomes=[
            {"expression": "rank(close)", "sharpe": 0.1, "fitness": 0.0},
            {"expression": "ts_mean(close, 5)", "sharpe": 0.05, "fitness": -0.1},
        ],
        r5_c1_reason="hypothesis predicts wrong sign",
        failure_tree_summary="2 prior mutations failed",
        region="USA", dataset_id="pv13", pillar="momentum",
    )
    assert "quantitative researcher" in sys_p
    assert "low-vol stocks" in user_p
    assert "rank(close)" in user_p
    assert "ts_mean(close, 5)" in user_p
    assert "wrong sign" in user_p
    assert "2 prior mutations failed" in user_p
    assert "USA" in user_p
    assert "pv13" in user_p
    assert "momentum" in user_p
    assert "new_hypothesis" in user_p


def test_build_mutate_prompt_handles_missing_inputs():
    sys_p, user_p = build_r1b_mutate_prompt(
        original_hypothesis="",
        original_alpha_outcomes=[],
        r5_c1_reason="",
    )
    assert "(no hypothesis recorded)" in user_p
    assert "(no alpha outcomes recorded)" in user_p
    assert "(no R5 c1 reason recorded" in user_p
    assert "(no prior failures in this family)" in user_p
    assert "(unspecified)" in user_p  # dataset_id default


# ---------------------------------------------------------------------------
# node_hypothesis_mutate — trigger gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutate_triggers_on_hypothesis_attribution():
    alpha = _mk_alpha("0", "rank(close)", attribution="hypothesis")
    state = _mk_state([alpha])
    llm = _mk_llm_mutate_resp("REVISED: rank low-volume momentum signals")
    out = await node_hypothesis_mutate(state, llm)
    assert "r1b_pending_new_hypothesis" in out
    assert out["r1b_pending_new_hypothesis"]["statement"] == (
        "REVISED: rank low-volume momentum signals"
    )
    assert out["r1b_mutations_attempted_this_cycle"] == 1
    assert out["r1b_token_cost_this_alpha"] > 0


@pytest.mark.asyncio
async def test_mutate_triggers_on_both_attribution():
    alpha = _mk_alpha("0", "rank(close)", attribution="both")
    state = _mk_state([alpha])
    llm = _mk_llm_mutate_resp("NEW thesis")
    out = await node_hypothesis_mutate(state, llm)
    assert "r1b_pending_new_hypothesis" in out


@pytest.mark.asyncio
async def test_mutate_skips_implementation_only():
    alpha = _mk_alpha("0", "rank(close)", attribution="implementation")
    state = _mk_state([alpha])
    llm = _mk_llm_mutate_resp("X")
    out = await node_hypothesis_mutate(state, llm)
    assert out == {}
    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutate_skips_unknown_attribution():
    alpha = _mk_alpha("0", "rank(close)", attribution="unknown")
    state = _mk_state([alpha])
    llm = _mk_llm_mutate_resp("X")
    out = await node_hypothesis_mutate(state, llm)
    assert out == {}


@pytest.mark.asyncio
async def test_mutate_skips_non_fail_alpha():
    alpha = _mk_alpha("0", "rank(close)", quality_status="PASS")
    state = _mk_state([alpha])
    llm = _mk_llm_mutate_resp("X")
    out = await node_hypothesis_mutate(state, llm)
    assert out == {}


# ---------------------------------------------------------------------------
# Dataset-cycle dedupe (plan §4.2 [V1.2-A2-4])
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutate_dedupes_per_unique_hypothesis():
    """3 FAIL+hypothesis alphas all sharing same hypothesis → 1 LLM call."""
    alphas = [
        _mk_alpha(f"{i}", f"rank(close+{i})", hypothesis="same thesis")
        for i in range(3)
    ]
    state = _mk_state(alphas)
    llm = _mk_llm_mutate_resp("new thesis")
    await node_hypothesis_mutate(state, llm)
    llm.call.assert_awaited_once()  # NOT 3 times


@pytest.mark.asyncio
async def test_mutate_picks_highest_impact_group():
    """When multiple hypotheses fail, pick the one with most failed alphas."""
    alphas = [
        _mk_alpha("0", "x", hypothesis="hypothesis_A"),  # 1 alpha
        _mk_alpha("1", "y", hypothesis="hypothesis_B"),  # 3 alphas (winner)
        _mk_alpha("2", "z", hypothesis="hypothesis_B"),
        _mk_alpha("3", "w", hypothesis="hypothesis_B"),
    ]
    state = _mk_state(alphas)
    llm = _mk_llm_mutate_resp("new thesis B")
    out = await node_hypothesis_mutate(state, llm)
    # parent_hypothesis_statement should be hypothesis_B (the highest-impact group)
    assert out["r1b_pending_new_hypothesis"]["parent_hypothesis_statement"] == "hypothesis_B"


# ---------------------------------------------------------------------------
# Budget guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutate_per_cycle_budget_exhausted_returns_early():
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha], mutations=2)  # default max=2
    llm = _mk_llm_mutate_resp("X")
    out = await node_hypothesis_mutate(state, llm)
    assert out == {"r1b_mutations_attempted_this_cycle": 2}
    llm.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutate_token_cost_ceiling_returns_early():
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha], cost=0.10)  # over default ceiling 0.05
    llm = _mk_llm_mutate_resp("X")
    out = await node_hypothesis_mutate(state, llm)
    assert out == {"r1b_token_cost_this_alpha": 0.10}
    llm.call.assert_not_awaited()


# ---------------------------------------------------------------------------
# Soft-fail + no-op outcomes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutate_llm_call_exception_soft_falls():
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha])
    svc = SimpleNamespace(model="claude-haiku-4-5-20251001")
    svc.call = AsyncMock(side_effect=RuntimeError("boom"))
    out = await node_hypothesis_mutate(state, svc)
    # Counter still bumps so router won't loop
    assert out["r1b_mutations_attempted_this_cycle"] == 1
    # No pending_new_hypothesis emitted
    assert "r1b_pending_new_hypothesis" not in out


@pytest.mark.asyncio
async def test_mutate_same_statement_no_pending_emit():
    """LLM returns identical statement → no pending_new_hypothesis emit."""
    alpha = _mk_alpha("0", "rank(close)", hypothesis="momentum")
    state = _mk_state([alpha])
    llm = _mk_llm_mutate_resp("momentum")  # same as original
    out = await node_hypothesis_mutate(state, llm)
    assert out["r1b_mutations_attempted_this_cycle"] == 1
    assert "r1b_pending_new_hypothesis" not in out


@pytest.mark.asyncio
async def test_mutate_empty_statement_no_pending_emit():
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha])
    llm = _mk_llm_mutate_resp("")
    out = await node_hypothesis_mutate(state, llm)
    assert "r1b_pending_new_hypothesis" not in out


@pytest.mark.asyncio
async def test_mutate_pending_payload_structure():
    """pending_new_hypothesis contains all expected fields."""
    alpha = _mk_alpha("0", "rank(close)", hypothesis="parent thesis")
    state = _mk_state([alpha])
    llm = _mk_llm_mutate_resp(
        "child thesis",
        rationale="economic mechanism",
        diff="restricted to subindustry-relative",
    )
    out = await node_hypothesis_mutate(state, llm)
    payload = out["r1b_pending_new_hypothesis"]
    assert payload["statement"] == "child thesis"
    assert payload["rationale"] == "economic mechanism"
    assert payload["expected_signal"] == "momentum"
    assert payload["key_fields"] == ["close", "volume"]
    assert payload["suggested_operators"] == ["ts_rank", "ts_mean"]
    assert payload["parent_hypothesis_statement"] == "parent thesis"
    assert payload["diff_from_original"] == "restricted to subindustry-relative"
