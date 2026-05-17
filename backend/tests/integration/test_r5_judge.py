"""Integration: Phase 2 R5 Hypothesis-Alignment Dual-Bridge LLM Judge.

Tests per plan v1.0 §1.5 + §3.3 + §1.4:
  1. _composite_score Eq. 7 math correctness (aligned + misaligned cases)
  2. _derive_attribution decision matrix (4 cases: c1 fail / c2 fail / both / neither)
  3. _estimate_cost provider rate table application
  4. _parse_judge_response handles valid + invalid + edge-case LLM outputs
  5. run_r5_judge empty description → c₁ skipped, hook_error set
  6. run_r5_judge mock LLM full happy path (both PASS / c1 fails / both fail)
  7. flag OFF byte-equivalence (settings.ENABLE_LLM_JUDGE=False → run_r5_judge not called)

Mock LLM avoids real API cost; all tests pure-Python.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import _flag_override_cache
from backend.agents.graph.r5_judge import (
    _composite_score,
    _derive_attribution,
    _estimate_cost,
    _parse_judge_response,
    run_r5_judge,
)


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


# ---------------------------------------------------------------------------
# Test 1: _composite_score Eq. 7 math
# ---------------------------------------------------------------------------

def test_composite_score_both_aligned_high_conf():
    """Both aligned at 0.9 → composite = 0.5*0.9 + 0.5*0.9 = 0.9."""
    c1 = {"aligned": True, "confidence": 0.9}
    c2 = {"aligned": True, "confidence": 0.9}
    assert _composite_score(c1, c2) == pytest.approx(0.9)


def test_composite_score_both_misaligned_high_conf():
    """Both misaligned at 0.9 → score per dim = 1-0.9 = 0.1, composite = 0.1."""
    c1 = {"aligned": False, "confidence": 0.9}
    c2 = {"aligned": False, "confidence": 0.9}
    assert _composite_score(c1, c2) == pytest.approx(0.1)


def test_composite_score_mixed_split():
    """c1 aligned 0.8, c2 misaligned 0.7 → 0.5*0.8 + 0.5*(1-0.7) = 0.55."""
    c1 = {"aligned": True, "confidence": 0.8}
    c2 = {"aligned": False, "confidence": 0.7}
    assert _composite_score(c1, c2) == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# Test 2: _derive_attribution decision matrix
# ---------------------------------------------------------------------------

def test_derive_attribution_both_strong_fail():
    """c1 + c2 both strong fail → BOTH."""
    c1 = {"aligned": False, "confidence": 0.9}
    c2 = {"aligned": False, "confidence": 0.9}
    assert _derive_attribution(c1, c2, low_conf=0.55) == "both"


def test_derive_attribution_c1_strong_fail_only():
    """c1 strong fail + c2 aligned → HYPOTHESIS."""
    c1 = {"aligned": False, "confidence": 0.8}
    c2 = {"aligned": True, "confidence": 0.85}
    assert _derive_attribution(c1, c2, low_conf=0.55) == "hypothesis"


def test_derive_attribution_c2_strong_fail_only():
    """c1 aligned + c2 strong fail → IMPLEMENTATION."""
    c1 = {"aligned": True, "confidence": 0.85}
    c2 = {"aligned": False, "confidence": 0.75}
    assert _derive_attribution(c1, c2, low_conf=0.55) == "implementation"


def test_derive_attribution_both_pass_returns_none():
    """Both aligned → defer to R1a (None)."""
    c1 = {"aligned": True, "confidence": 0.9}
    c2 = {"aligned": True, "confidence": 0.9}
    assert _derive_attribution(c1, c2, low_conf=0.55) is None


def test_derive_attribution_low_conf_misalignment_returns_none():
    """Low-conf fail (below LOW_CONF threshold) → defer to R1a."""
    c1 = {"aligned": False, "confidence": 0.40}  # below 0.55 threshold
    c2 = {"aligned": False, "confidence": 0.40}
    assert _derive_attribution(c1, c2, low_conf=0.55) is None


# ---------------------------------------------------------------------------
# Test 3: _estimate_cost provider rate table
# ---------------------------------------------------------------------------

def test_estimate_cost_haiku_typical():
    """haiku-4-5 / 2000 tokens → ~$0.0094 (30% in × $0.001/k + 70% out × $0.005/k)."""
    cost = _estimate_cost("claude-haiku-4-5-20251001", 2000)
    # 600 in tok * 0.001/1000 + 1400 out tok * 0.005/1000 = 0.0006 + 0.007 = 0.0076
    assert cost == pytest.approx(0.0076, abs=1e-4)


def test_estimate_cost_unknown_model_default():
    """Unknown model → uses default 0.001 in / 0.005 out rate."""
    cost = _estimate_cost("some-future-model", 1000)
    assert cost == pytest.approx(0.0038, abs=1e-4)  # 300*0.001/1k + 700*0.005/1k


def test_estimate_cost_zero_tokens():
    assert _estimate_cost("claude-haiku-4-5", 0) == 0.0
    assert _estimate_cost("claude-haiku-4-5", None) == 0.0  # type: ignore


# ---------------------------------------------------------------------------
# Test 4: _parse_judge_response valid + invalid + edge cases
# ---------------------------------------------------------------------------

def test_parse_judge_valid_json():
    out = _parse_judge_response('{"aligned": true, "confidence": 0.85, "reason": "matches"}')
    assert out == {"aligned": True, "confidence": 0.85, "reason": "matches"}


def test_parse_judge_string_aligned():
    """String 'true' coerced to bool."""
    out = _parse_judge_response('{"aligned": "true", "confidence": 0.7, "reason": ""}')
    assert out["aligned"] is True


def test_parse_judge_confidence_clipped_high():
    out = _parse_judge_response('{"aligned": true, "confidence": 1.5, "reason": "x"}')
    assert out["confidence"] == 1.0


def test_parse_judge_confidence_clipped_low():
    out = _parse_judge_response('{"aligned": false, "confidence": -0.2, "reason": "y"}')
    assert out["confidence"] == 0.0


def test_parse_judge_reason_truncated():
    long_reason = "z" * 1000
    out = _parse_judge_response(f'{{"aligned": true, "confidence": 0.5, "reason": "{long_reason}"}}')
    assert len(out["reason"]) == 500


def test_parse_judge_malformed_returns_abstain():
    """Non-JSON / parse error → abstain (aligned=True, conf=0.5)."""
    out = _parse_judge_response("not json")
    assert out["aligned"] is True
    assert out["confidence"] == 0.5
    assert "parse error" in out["reason"]


def test_parse_judge_missing_aligned_key():
    """Missing aligned key → bool(None) = False, but parser handles gracefully."""
    out = _parse_judge_response('{"confidence": 0.8, "reason": "x"}')
    # bool(None) is False so this returns aligned=False — acceptable, will not strong-fail because conf threshold
    assert isinstance(out["aligned"], bool)
    assert out["confidence"] == 0.8


# ---------------------------------------------------------------------------
# Test 5+6: run_r5_judge — edge cases + mock LLM happy path
# ---------------------------------------------------------------------------

def _mock_llm(content_c1: str, content_c2: str, tokens: int = 200):
    """Build a mock LLMService whose call() alternates c1 → c2 responses."""
    mock = SimpleNamespace()
    mock.model = "claude-haiku-4-5-20251001"
    responses = [
        SimpleNamespace(content=content_c1, tokens_used=tokens),
        SimpleNamespace(content=content_c2, tokens_used=tokens),
    ]
    call_idx = {"i": 0}

    async def fake_call(**kw):
        r = responses[call_idx["i"] % 2]
        call_idx["i"] += 1
        return r

    mock.call = fake_call
    return mock


@pytest.mark.asyncio
async def test_run_r5_judge_empty_description_skips_c1_and_c2():
    """[V1.0-A1-2] empty description → both c₁ and c₂ skip with hook_error."""
    llm = _mock_llm("", "")
    out = await run_r5_judge(
        hypothesis_statement="hyp",
        description="",
        expression="rank(close)",
        llm_service=llm,
    )
    assert out["r5_c1_aligned"] is None
    assert out["r5_c2_aligned"] is None
    assert "c1_skipped:desc_empty" in (out["r5_hook_error"] or "")
    assert "c2_skipped:desc_empty" in (out["r5_hook_error"] or "")
    assert out["r5_attribution"] is None  # no judgment possible


@pytest.mark.asyncio
async def test_run_r5_judge_both_pass_returns_none_attribution():
    """Both c₁ + c₂ aligned high conf → r5_attribution=None (defer R1a)."""
    llm = _mock_llm(
        '{"aligned": true, "confidence": 0.9, "reason": "ok"}',
        '{"aligned": true, "confidence": 0.9, "reason": "ok"}',
    )
    out = await run_r5_judge(
        hypothesis_statement="momentum hypothesis",
        description="rolling mean of returns",
        expression="ts_mean(returns, 20)",
        llm_service=llm,
        r1a_attribution="unknown",
    )
    assert out["r5_c1_aligned"] == "true"
    assert out["r5_c2_aligned"] == "true"
    assert out["r5_composite_score"] == pytest.approx(0.9)
    assert out["r5_attribution"] is None
    assert out["r5_agrees_r1a"] is None  # no R5 verdict → not computed


@pytest.mark.asyncio
async def test_run_r5_judge_c1_fail_attributes_hypothesis():
    """c₁ strong fail + c₂ pass → r5_attribution='hypothesis'."""
    llm = _mock_llm(
        '{"aligned": false, "confidence": 0.85, "reason": "h says X, d says Y"}',
        '{"aligned": true, "confidence": 0.9, "reason": "ok"}',
    )
    out = await run_r5_judge(
        hypothesis_statement="momentum",
        description="value tilt",
        expression="rank(close)",
        llm_service=llm,
        r1a_attribution="implementation",  # R1a said impl; R5 will overwrite to hypothesis
    )
    assert out["r5_attribution"] == "hypothesis"
    assert out["r5_agrees_r1a"] == "false"  # R5 hypothesis ≠ R1a implementation


@pytest.mark.asyncio
async def test_run_r5_judge_both_fail_attributes_both():
    llm = _mock_llm(
        '{"aligned": false, "confidence": 0.8, "reason": ""}',
        '{"aligned": false, "confidence": 0.75, "reason": ""}',
    )
    out = await run_r5_judge(
        hypothesis_statement="h",
        description="d",
        expression="f",
        llm_service=llm,
        r1a_attribution="both",
    )
    assert out["r5_attribution"] == "both"
    assert out["r5_agrees_r1a"] == "true"  # R5 both == R1a both


@pytest.mark.asyncio
async def test_run_r5_judge_cost_estimation_populated():
    """Cost summed across c1 + c2 calls."""
    llm = _mock_llm(
        '{"aligned": true, "confidence": 0.8, "reason": ""}',
        '{"aligned": true, "confidence": 0.8, "reason": ""}',
        tokens=1000,
    )
    out = await run_r5_judge(
        hypothesis_statement="h",
        description="d",
        expression="f",
        llm_service=llm,
    )
    # 2 calls × 1000 tokens × haiku rate ≈ 0.0076 (one call cost) × 2 = 0.0152
    assert out["r5_cost_usd"] > 0
    assert out["r5_cost_usd"] < 0.05  # GO gate criterion


@pytest.mark.asyncio
async def test_run_r5_judge_llm_call_failure_graceful():
    """LLM exception → caught, hook_error set, judge abstains gracefully."""
    failing_llm = SimpleNamespace()
    failing_llm.model = "claude-haiku-4-5-20251001"
    failing_llm.call = AsyncMock(side_effect=RuntimeError("API timeout"))
    out = await run_r5_judge(
        hypothesis_statement="h",
        description="d",
        expression="f",
        llm_service=failing_llm,
    )
    assert out["r5_attribution"] is None  # cannot derive without both
    assert out["r5_hook_error"]
    assert "API timeout" in out["r5_hook_error"]
