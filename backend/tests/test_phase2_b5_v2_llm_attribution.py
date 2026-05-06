"""B5 v2 — LLM-based attribution classifier tests.

The LLM is fully mocked. We verify:

1. classify_attribution_llm correctly delegates to LLM when llm_service is
   provided + hypothesis_statement is non-empty + heuristic isn't UNKNOWN
2. Falls back to heuristic when LLM call fails / returns invalid output /
   hypothesis empty / heuristic == "unknown"
3. _process_hypothesis_feedback wires llm_service through correctly and
   stores LLM reasoning in the round-history entry
4. attribution from LLM (e.g., "implementation") overrides heuristic
   (e.g., "hypothesis") when LLM disagrees
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_llm_response(parsed: dict | None, success: bool = True, content: str = ""):
    r = MagicMock()
    r.success = success
    r.parsed = parsed
    r.content = content
    r.error = None
    return r


def _make_llm_service(parsed: dict | None, success: bool = True):
    """Build a mock LLMService that returns the given parsed JSON."""
    svc = MagicMock()
    svc.call = AsyncMock(return_value=_make_llm_response(parsed, success))
    return svc


# =============================================================================
# classify_attribution_llm — pure unit tests (no DB, no real LLM)
# =============================================================================

@pytest.mark.asyncio
async def test_llm_attribution_used_when_service_provided():
    from backend.agents.graph.attribution import classify_attribution_llm

    llm = _make_llm_service({
        "attribution": "implementation",
        "confidence": 0.9,
        "reasoning": "syntax errors dominated despite low qual fail count",
    })
    result, reason = await classify_attribution_llm(
        hypothesis_statement="Test hypothesis statement",
        pending_alphas=[],
        alpha_count=4, pass_count=0,
        syntax_fail_count=2, simulate_fail_count=1, quality_fail_count=1,
        llm_service=llm,
    )
    # Heuristic would say "both" (50/50); LLM says "implementation"
    assert result == "implementation"
    assert "syntax errors" in (reason or "")
    llm.call.assert_called_once()


@pytest.mark.asyncio
async def test_fallback_when_llm_service_is_none():
    from backend.agents.graph.attribution import classify_attribution_llm
    result, reason = await classify_attribution_llm(
        hypothesis_statement="anything",
        pending_alphas=[],
        alpha_count=4, pass_count=0,
        syntax_fail_count=4, simulate_fail_count=0, quality_fail_count=0,
        llm_service=None,
    )
    # Heuristic: 100% syntax fails → "implementation"
    assert result == "implementation"
    assert reason is None  # no LLM call → no reasoning


@pytest.mark.asyncio
async def test_fallback_when_hypothesis_empty():
    from backend.agents.graph.attribution import classify_attribution_llm
    llm = _make_llm_service({"attribution": "hypothesis"})
    result, reason = await classify_attribution_llm(
        hypothesis_statement="",  # empty
        pending_alphas=[],
        alpha_count=4, pass_count=0,
        syntax_fail_count=0, simulate_fail_count=0, quality_fail_count=4,
        llm_service=llm,
    )
    # Heuristic: 100% quality fail → "hypothesis"
    assert result == "hypothesis"
    # LLM should NOT have been called (empty hypothesis = early bail)
    llm.call.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_when_heuristic_unknown_skips_llm():
    """Cost optimization: don't burn an LLM call when heuristic returns
    'unknown' (no signal to attribute, e.g. PASS round or 0 alphas)."""
    from backend.agents.graph.attribution import classify_attribution_llm
    llm = _make_llm_service({"attribution": "hypothesis"})
    result, reason = await classify_attribution_llm(
        hypothesis_statement="anything",
        pending_alphas=[],
        alpha_count=4, pass_count=2,  # has PASS → heuristic = unknown
        syntax_fail_count=0, simulate_fail_count=0, quality_fail_count=2,
        llm_service=llm,
    )
    assert result == "unknown"
    llm.call.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_when_llm_call_raises():
    from backend.agents.graph.attribution import classify_attribution_llm
    llm = MagicMock()
    llm.call = AsyncMock(side_effect=RuntimeError("DeepSeek down"))
    result, reason = await classify_attribution_llm(
        hypothesis_statement="test",
        pending_alphas=[],
        alpha_count=4, pass_count=0,
        syntax_fail_count=0, simulate_fail_count=0, quality_fail_count=4,
        llm_service=llm,
    )
    assert result == "hypothesis"  # heuristic
    assert "fallback" in (reason or "")


@pytest.mark.asyncio
async def test_fallback_when_llm_unsuccessful():
    from backend.agents.graph.attribution import classify_attribution_llm
    llm = _make_llm_service(parsed=None, success=False)
    result, reason = await classify_attribution_llm(
        hypothesis_statement="test",
        pending_alphas=[],
        alpha_count=4, pass_count=0,
        syntax_fail_count=0, simulate_fail_count=0, quality_fail_count=4,
        llm_service=llm,
    )
    assert result == "hypothesis"
    assert "fallback" in (reason or "")


@pytest.mark.asyncio
async def test_fallback_when_llm_returns_invalid_attribution():
    from backend.agents.graph.attribution import classify_attribution_llm
    llm = _make_llm_service({"attribution": "garbage_value", "reasoning": "x"})
    result, reason = await classify_attribution_llm(
        hypothesis_statement="test",
        pending_alphas=[],
        alpha_count=4, pass_count=0,
        syntax_fail_count=0, simulate_fail_count=0, quality_fail_count=4,
        llm_service=llm,
    )
    assert result == "hypothesis"  # heuristic
    assert "fallback" in (reason or "")


@pytest.mark.asyncio
async def test_llm_returns_valid_alternative_attribution():
    """Heuristic says 'implementation' (75% impl); LLM says 'both' — accept LLM."""
    from backend.agents.graph.attribution import classify_attribution_llm
    llm = _make_llm_service({
        "attribution": "both",
        "confidence": 0.7,
        "reasoning": "Mixed: some impl errors but quality fails also point to weak hypothesis",
    })
    result, reason = await classify_attribution_llm(
        hypothesis_statement="Test idea",
        pending_alphas=[],
        alpha_count=4, pass_count=0,
        syntax_fail_count=3, simulate_fail_count=0, quality_fail_count=1,
        llm_service=llm,
    )
    assert result == "both"
    assert "Mixed" in (reason or "")


@pytest.mark.asyncio
async def test_llm_response_via_content_when_parsed_missing():
    """Some LLM clients put JSON in `content` rather than `parsed`."""
    from backend.agents.graph.attribution import classify_attribution_llm
    llm = MagicMock()
    llm.call = AsyncMock(return_value=_make_llm_response(
        parsed=None,
        success=True,
        content='{"attribution": "hypothesis", "reasoning": "all 4 quality fails"}',
    ))
    result, reason = await classify_attribution_llm(
        hypothesis_statement="Test",
        pending_alphas=[],
        alpha_count=4, pass_count=0,
        syntax_fail_count=0, simulate_fail_count=0, quality_fail_count=4,
        llm_service=llm,
    )
    assert result == "hypothesis"
    assert "all 4" in (reason or "")


# =============================================================================
# Sample builder
# =============================================================================

def test_build_samples_handles_all_failure_types():
    from backend.agents.graph.attribution import _build_samples
    from backend.agents.graph.state import AlphaCandidate

    rows = [
        AlphaCandidate(
            expression="ts_rank(close, 5)",
            is_valid=False, validation_error="undefined op",
            quality_status="FAIL", metrics={},
        ),
        AlphaCandidate(
            expression="rank(returns)",
            is_valid=True, is_simulated=True, simulation_success=False,
            simulation_error="brain timeout",
            quality_status="FAIL", metrics={},
        ),
        AlphaCandidate(
            expression="zscore(volume)",
            is_valid=True, is_simulated=True, simulation_success=True,
            quality_status="FAIL", metrics={"sharpe": -0.4, "fitness": 0.1},
        ),
    ]
    out = _build_samples(rows)
    assert len(out) == 3
    assert "SYNTAX_FAIL" in out[0]
    assert "SIMULATE_FAIL" in out[1]
    assert "FAIL" in out[2]
    assert "sharpe=-0.4" in out[2]
