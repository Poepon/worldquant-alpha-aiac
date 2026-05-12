"""V-22.8 (2026-05-13) — post-LLM hallucinated-field guard.

Spike on task 534/535 found LLM occasionally selects field IDs not
present in available_fields (cross-dataset hallucination), causing
VALIDATE failures + wasted SELF_CORRECT cycles. V-22.8 hard-filters
LLM output to the strict subset of available_fields IDs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.factor_generation import (
    T1Strategy,
    select_t1_strategy_via_llm,
)


def _mock_llm_response(promising_fields: list[str]):
    """Build a mocked T1Strategy LLM return."""
    parsed = T1Strategy(
        economic_hypothesis="test hypothesis",
        signal_velocity="MEDIUM",
        window_scale="MEDIUM",
        promising_fields=promising_fields,
        preferred_ts_ops=["ts_rank", "ts_zscore"],
        rationale="test",
    )

    class _Raw:
        success = True
        error = None

    return parsed, _Raw()


@pytest.mark.asyncio
async def test_v22_8_drops_hallucinated_fields():
    """LLM-returned fields not in available_fields must be dropped."""
    available_fields = [
        {"id": "close", "type": "MATRIX", "coverage": 1.0},
        {"id": "cap", "type": "MATRIX", "coverage": 1.0},
        {"id": "fnd6_eps_q", "type": "MATRIX", "coverage": 0.9},
        {"id": "fnd6_ebit_q", "type": "MATRIX", "coverage": 0.9},
        {"id": "fnd6_revenue_q", "type": "MATRIX", "coverage": 0.95},
        {"id": "fnd6_acodo", "type": "MATRIX", "coverage": 0.8},
        {"id": "fnd6_teq", "type": "MATRIX", "coverage": 0.85},
    ]
    # LLM mixes valid IDs with 3 hallucinated ones
    llm_picked = [
        "close", "cap", "fnd6_eps_q", "fnd6_ebit_q", "fnd6_revenue_q",
        "opt8_put_call_ratio_30d",     # hallucinated (option dataset)
        "anl4_afv4_cfps_mean",          # hallucinated (analyst dataset)
        "fnd6_debt_to_equity_ratio",    # hallucinated (not in this list)
    ]

    mock_llm = AsyncMock()
    mock_llm.call_with_schema.return_value = _mock_llm_response(llm_picked)

    result = await select_t1_strategy_via_llm(
        dataset_id="fundamental6",
        region="USA",
        available_fields=available_fields,
        success_patterns=[],
        llm_service=mock_llm,
    )

    valid_ids = {f["id"] for f in available_fields}
    for fid in result.promising_fields:
        assert fid in valid_ids, (
            f"hallucinated field '{fid}' was not filtered out"
        )
    # Should preserve the 5 valid ones LLM picked
    for fid in ("close", "cap", "fnd6_eps_q", "fnd6_ebit_q", "fnd6_revenue_q"):
        assert fid in result.promising_fields


@pytest.mark.asyncio
async def test_v22_8_preserves_when_no_hallucination():
    """LLM output without hallucination must pass through unchanged."""
    available_fields = [
        {"id": "close", "type": "MATRIX"},
        {"id": "open", "type": "MATRIX"},
        {"id": "cap", "type": "MATRIX"},
    ]
    llm_picked = ["close", "open", "cap"]

    mock_llm = AsyncMock()
    mock_llm.call_with_schema.return_value = _mock_llm_response(llm_picked)

    result = await select_t1_strategy_via_llm(
        dataset_id="pv1", region="USA",
        available_fields=available_fields,
        success_patterns=[], llm_service=mock_llm,
    )
    assert set(result.promising_fields) == {"close", "open", "cap"}


@pytest.mark.asyncio
async def test_v22_8_backfills_when_too_aggressive():
    """When filter strips too many fields (< 5 remain), backfill with
    top-coverage from available_fields."""
    available_fields = [
        {"id": f"fnd6_field_{i:03d}", "type": "MATRIX", "coverage": 1.0 - i * 0.01}
        for i in range(20)
    ]
    # LLM picks 8 fields, only 2 valid
    llm_picked = [
        "fnd6_field_000", "fnd6_field_001",  # valid
        "halluc1", "halluc2", "halluc3",
        "halluc4", "halluc5", "halluc6",
    ]

    mock_llm = AsyncMock()
    mock_llm.call_with_schema.return_value = _mock_llm_response(llm_picked)

    result = await select_t1_strategy_via_llm(
        dataset_id="fundamental6", region="USA",
        available_fields=available_fields,
        success_patterns=[], llm_service=mock_llm,
    )
    # Backfill should bring it to ≥ 5 valid fields
    assert len(result.promising_fields) >= 5
    valid_ids = {f["id"] for f in available_fields}
    for fid in result.promising_fields:
        assert fid in valid_ids
    # The 2 originally-valid LLM picks should be preserved
    assert "fnd6_field_000" in result.promising_fields
    assert "fnd6_field_001" in result.promising_fields


@pytest.mark.asyncio
async def test_v22_8_case_insensitive_match():
    """Field ID matching should be case-insensitive (BRAIN convention varies)."""
    available_fields = [
        {"id": "close", "type": "MATRIX"},
        {"id": "FND6_EPS_Q", "type": "MATRIX"},
    ]
    llm_picked = ["CLOSE", "fnd6_eps_q"]  # case-mismatched

    mock_llm = AsyncMock()
    mock_llm.call_with_schema.return_value = _mock_llm_response(llm_picked)

    result = await select_t1_strategy_via_llm(
        dataset_id="fundamental6", region="USA",
        available_fields=available_fields,
        success_patterns=[], llm_service=mock_llm,
    )
    # Both should pass case-insensitive check
    assert len(result.promising_fields) == 2
