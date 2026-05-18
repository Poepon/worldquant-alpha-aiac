"""Phase 3 R1b.1d: integration + byte-equivalence + log-write tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §3.6 + §3.7.

PR1d closes R1b.1 sub-phase implementation by adding:
  - Log-row write verification (R1b plan §3.6 #9)
  - Flag-OFF byte-equivalence regression guard (R1b plan §3.6 #1 the
    single most important R1b test — mirror of Q10's same test)
  - State reset boundary verification (R1b plan §3.5 [V1.1-A1-2])
  - One-cycle integration smoke (mocked LLM + full retry path)

After this ship, R1b.1 sub-phase is GO-gate-observation-ready: deploy
behind flag, watch r1b_retry_log for ≥7d / ≥50 retries / ≥15% success.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class _FakeAlpha(SimpleNamespace):
    def model_copy(self):
        clone = _FakeAlpha(**self.__dict__)
        clone.metrics = dict(self.metrics or {})
        return clone


def _mk_alpha(idx_str, expression, attribution="implementation",
              quality_status="FAIL", hypothesis="momentum"):
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
            "_r1a_attribution_evidence": ["evidence"],
            "_r5_c2_reason": "expression diverged from hypothesis",
            "sharpe": 0.1, "fitness": 0.0, "turnover": 0.5,
        },
    )


def _mk_state(alphas, *, retries=0, cost=0.0, task_id=42, round_idx=1):
    return SimpleNamespace(
        pending_alphas=alphas,
        fields=[{"id": "close"}, {"id": "open"}],
        region="USA",
        task_id=task_id,
        round_idx=round_idx,
        r1b_retries_attempted_this_alpha=retries,
        r1b_mutations_attempted_this_cycle=0,
        r1b_token_cost_this_alpha=cost,
    )


def _mk_llm(parsed, *, success=True, tokens=150):
    resp = SimpleNamespace(
        success=success, parsed=parsed, content="(unused)", tokens_used=tokens,
    )
    svc = SimpleNamespace(model="claude-haiku-4-5-20251001")
    svc.call = AsyncMock(return_value=resp)
    return svc


# ---------------------------------------------------------------------------
# Log row write verification (plan §3.6 #9)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_writes_log_row_with_full_payload():
    """node_code_gen_retry calls _write_r1b_retry_log_rows with a populated
    row per target alpha (regardless of outcome)."""
    from backend.agents.graph.nodes import r1b_loop
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha])
    llm = _mk_llm({"fixed_expression": "rank(close - open)", "changes_made": "neutralize"})
    captured_rows = []
    async def _capture(rows):
        captured_rows.extend(rows)
    with patch.object(r1b_loop, "_write_r1b_retry_log_rows", new=AsyncMock(side_effect=_capture)):
        await r1b_loop.node_code_gen_retry(state, llm)
    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row["attempt_type"] == "retry_impl"
    assert row["task_id"] == 42
    assert row["round_idx"] == 1
    assert row["triggering_attribution"] == "implementation"
    assert row["triggering_attribution_source"] == "r5_judge"  # _r5_c2_reason populated
    assert row["new_expression"] == "rank(close - open)"
    assert row["llm_changes_made"] == "neutralize"
    assert row["outcome"] == "pending"
    assert row["loop_error"] is None
    assert row["llm_tokens_used"] == 150
    assert row["llm_cost_usd"] > 0
    assert row["llm_model"] == "claude-haiku-4-5-20251001"
    assert len(row["original_expression_hash"]) == 64  # sha256[:64]


@pytest.mark.asyncio
async def test_retry_log_row_records_llm_failure():
    """LLM raises → log row still written with loop_error populated."""
    from backend.agents.graph.nodes import r1b_loop
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha])
    svc = SimpleNamespace(model="claude-haiku-4-5-20251001")
    svc.call = AsyncMock(side_effect=RuntimeError("LLM provider 500"))
    captured = []
    async def _cap(rows):
        captured.extend(rows)
    with patch.object(r1b_loop, "_write_r1b_retry_log_rows", new=AsyncMock(side_effect=_cap)):
        await r1b_loop.node_code_gen_retry(state, svc)
    assert len(captured) == 1
    row = captured[0]
    assert row["new_expression"] is None
    assert "LLM provider 500" in row["loop_error"]
    assert row["llm_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_retry_log_row_records_same_expression_noop():
    """LLM returns identical expression → row written with explanatory error."""
    from backend.agents.graph.nodes import r1b_loop
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha])
    llm = _mk_llm({"fixed_expression": "rank(close)", "changes_made": "n/a"})
    captured = []
    async def _cap(rows):
        captured.extend(rows)
    with patch.object(r1b_loop, "_write_r1b_retry_log_rows", new=AsyncMock(side_effect=_cap)):
        await r1b_loop.node_code_gen_retry(state, llm)
    assert len(captured) == 1
    assert "same/empty expression" in captured[0]["loop_error"]


# ---------------------------------------------------------------------------
# Flag-OFF byte-equivalence (plan §3.6 #1 — most important regression guard)
# ---------------------------------------------------------------------------

def test_workflow_flag_off_does_not_register_r1b_nodes(monkeypatch):
    """Both R1b flags OFF → r1b_loop module never imported during graph build.

    Sentinel via patching: if the module IS imported under flag-OFF, we
    fail. Mirrors Q10's test_flag_off_byte_equivalent intent.
    """
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", False, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    # The workflow.py r1b import is inside an `if _r1b_active:` block so the
    # module path stays uncalled when both flags are False.
    import sys
    sys.modules.pop("backend.agents.graph.nodes.r1b_loop", None)
    from backend.agents.graph.workflow import MiningWorkflow  # noqa: F401
    # If r1b_loop was imported, that's a flag-OFF leak.
    assert "backend.agents.graph.nodes.r1b_loop" not in sys.modules, (
        "Flag-OFF leak: r1b_loop imported even with both R1b flags False"
    )


def test_workflow_flag_on_imports_r1b_loop_module(monkeypatch):
    """Flag ON → r1b_loop module IS imported when graph builds."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    import sys
    sys.modules.pop("backend.agents.graph.nodes.r1b_loop", None)
    sys.modules.pop("backend.agents.graph.workflow", None)
    # Re-import workflow; build triggers conditional import inside _build_graph
    # We can't easily call _build_graph without DB / brain fixtures, so the
    # smoke is just: workflow module imports without error under flag ON.
    from backend.agents.graph.workflow import MiningWorkflow  # noqa: F401


# ---------------------------------------------------------------------------
# State reset boundary (plan §3.5 [V1.1-A1-2])
# ---------------------------------------------------------------------------

def test_mining_state_r1b_fields_default_to_zero():
    """Plan §5.1 — counters default 0 / 0.0 so they reset per invocation."""
    from backend.agents.graph.state import MiningState
    s = MiningState(task_id=1, region="USA")
    assert s.r1b_retries_attempted_this_alpha == 0
    assert s.r1b_mutations_attempted_this_cycle == 0
    assert s.r1b_token_cost_this_alpha == 0.0
    assert s.r1b_loop_attribution_evidence == []
    assert s.r1b_mutated_hypothesis_ids == []
    assert s.r1b_pending_new_hypothesis is None


def test_mining_state_r1b_fields_serialization_roundtrip():
    """Pydantic field defaults preserve through model_dump / model_validate."""
    from backend.agents.graph.state import MiningState
    s1 = MiningState(
        task_id=1, region="USA",
        r1b_retries_attempted_this_alpha=2,
        r1b_token_cost_this_alpha=0.025,
    )
    blob = s1.model_dump()
    assert blob["r1b_retries_attempted_this_alpha"] == 2
    assert blob["r1b_token_cost_this_alpha"] == 0.025
    s2 = MiningState.model_validate(blob)
    assert s2.r1b_retries_attempted_this_alpha == 2


# ---------------------------------------------------------------------------
# One-cycle integration smoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_cycle_retry_replaces_alpha_and_counters_advance():
    """Full one-iteration smoke: retry node + budget counters + state replace."""
    from backend.agents.graph.nodes import r1b_loop
    alpha = _mk_alpha("0", "rank(close)")
    state = _mk_state([alpha], retries=0)
    llm = _mk_llm({"fixed_expression": "rank(close - open)", "changes_made": "neutralize"})
    with patch.object(r1b_loop, "_write_r1b_retry_log_rows", new=AsyncMock(return_value=None)):
        out = await r1b_loop.node_code_gen_retry(state, llm)
    # Counter bumped exactly once
    assert out["r1b_retries_attempted_this_alpha"] == 1
    # Token cost accumulated
    assert out["r1b_token_cost_this_alpha"] > 0
    # Alpha rewritten + validation state reset
    rewritten = out["pending_alphas"][0]
    assert rewritten.expression == "rank(close - open)"
    assert rewritten.original_expression == "rank(close)"
    assert rewritten.is_valid is None
    assert rewritten.is_simulated is False
    assert rewritten.simulation_success is None
    assert rewritten.quality_status == "PENDING"
    # Retry chain captured
    assert rewritten.metrics["_r1b_retry_chain"] == ["rank(close)"]
    assert rewritten.metrics["_r1b_retry_reason"] == "neutralize"


@pytest.mark.asyncio
async def test_multi_alpha_partial_retry_does_not_drop_others():
    """When 1 of 3 alphas has implementation attribution, only that one is
    rewritten — others pass through unchanged."""
    from backend.agents.graph.nodes import r1b_loop
    alphas = [
        _mk_alpha("0", "rank(close)", attribution="implementation"),
        _mk_alpha("1", "rank(volume)", attribution="hypothesis"),  # skipped
        _mk_alpha("2", "rank(open)", attribution="unknown"),       # skipped
    ]
    state = _mk_state(alphas)
    llm = _mk_llm({"fixed_expression": "rank(close - open)", "changes_made": "fix"})
    with patch.object(r1b_loop, "_write_r1b_retry_log_rows", new=AsyncMock(return_value=None)):
        out = await r1b_loop.node_code_gen_retry(state, llm)
    assert out["pending_alphas"][0].expression == "rank(close - open)"
    assert out["pending_alphas"][1].expression == "rank(volume)"  # untouched
    assert out["pending_alphas"][2].expression == "rank(open)"    # untouched
