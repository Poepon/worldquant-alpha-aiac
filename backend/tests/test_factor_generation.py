"""Tests for backend.factor_generation — T1 LLM-guided strategy + expansion."""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.factor_generation import (
    DEFAULT_T1_STRATEGY,
    T1Strategy,
    WINDOW_SCALE_MAP,
    _fill_default_with_top_fields,
    expand_t1_strategy,
    select_t1_strategy_via_llm,
    stratified_sample,
)


# Prime field cache so the classifier's tier=1 roundtrip check passes
from backend.factor_tier_classifier import populate_known_fields

populate_known_fields({"close", "volume", "returns", "vwap", "fnd6_test_field", "return_equity"})


# ---------------------------------------------------------------------------
# T1Strategy schema sanity
# ---------------------------------------------------------------------------

class TestT1StrategySchema:
    def test_schema_round_trip(self):
        s = T1Strategy(
            economic_hypothesis="test",
            signal_velocity="MEDIUM",
            window_scale="MEDIUM",
            promising_fields=["close"],
            preferred_ts_ops=["ts_rank"],
        )
        d = s.model_dump()
        s2 = T1Strategy(**d)
        assert s2.economic_hypothesis == "test"
        assert s2.signal_velocity == "MEDIUM"
        assert s2.preferred_ts_ops == ["ts_rank"]

    def test_window_scale_map(self):
        assert WINDOW_SCALE_MAP["SHORT"] == [5, 10]
        assert WINDOW_SCALE_MAP["MEDIUM"] == [20, 60]
        assert WINDOW_SCALE_MAP["LONG"] == [120, 240]


# ---------------------------------------------------------------------------
# expand_t1_strategy
# ---------------------------------------------------------------------------

class TestExpandT1Strategy:
    def test_basic_expansion(self):
        strat = T1Strategy(
            economic_hypothesis="x",
            signal_velocity="MEDIUM",
            window_scale="MEDIUM",
            promising_fields=["close", "volume"],
            preferred_ts_ops=["ts_rank", "ts_zscore"],
        )
        result = expand_t1_strategy(strat, daily_goal=4, region="USA")
        # 2 fields × 2 ops × 2 windows = 8 candidates max; sample target = ceil(4*1.5) = 6
        assert 1 <= len(result) <= 6
        # All must be classified as T1
        from backend.factor_tier_classifier import classify_tier
        for r in result:
            assert classify_tier(r["expression"]) == 1

    def test_empty_strategy_returns_empty(self):
        strat = T1Strategy(
            economic_hypothesis="x",
            signal_velocity="MEDIUM",
            window_scale="MEDIUM",
            promising_fields=[],
            preferred_ts_ops=["ts_rank"],
        )
        assert expand_t1_strategy(strat, daily_goal=4, region="USA") == []

    def test_empty_ops_returns_empty(self):
        strat = T1Strategy(
            economic_hypothesis="x",
            signal_velocity="MEDIUM",
            window_scale="MEDIUM",
            promising_fields=["close"],
            preferred_ts_ops=[],
        )
        assert expand_t1_strategy(strat, daily_goal=4, region="USA") == []

    def test_short_window_scale(self):
        strat = T1Strategy(
            economic_hypothesis="fast", signal_velocity="FAST",
            window_scale="SHORT", promising_fields=["close"],
            preferred_ts_ops=["ts_delta"],
        )
        result = expand_t1_strategy(strat, daily_goal=2, region="USA")
        # ts_delta(close, 5) and ts_delta(close, 10) — both windows from SHORT map
        assert len(result) >= 1
        windows_used = {r.get("window") for r in result}
        assert windows_used.issubset({5, 10})


# ---------------------------------------------------------------------------
# stratified_sample
# ---------------------------------------------------------------------------

class TestStratifiedSample:
    def test_each_group_represented(self):
        items = [
            {"op": "ts_rank", "x": i} for i in range(5)
        ] + [
            {"op": "ts_zscore", "x": i} for i in range(5)
        ] + [
            {"op": "ts_mean", "x": i} for i in range(5)
        ]
        out = stratified_sample(items, by="op", n=6)
        ops_in_out = {it["op"] for it in out}
        # All 3 groups should have at least one representative
        assert ops_in_out == {"ts_rank", "ts_zscore", "ts_mean"}
        assert len(out) == 6

    def test_n_zero_returns_empty(self):
        items = [{"op": "x"}]
        assert stratified_sample(items, by="op", n=0) == []

    def test_empty_items(self):
        assert stratified_sample([], by="op", n=5) == []

    def test_n_larger_than_items_returns_all(self):
        items = [{"op": "a"}, {"op": "b"}]
        out = stratified_sample(items, by="op", n=10)
        assert len(out) == 2  # capped at len(items)


# ---------------------------------------------------------------------------
# _fill_default_with_top_fields
# ---------------------------------------------------------------------------

class TestFillDefaultFields:
    def test_filters_out_group_tokens(self):
        avail = [
            {"id": "industry", "coverage": 1.0},
            {"id": "sector", "coverage": 1.0},
            {"id": "close", "coverage": 0.95},
            {"id": "volume", "coverage": 0.8},
        ]
        s = _fill_default_with_top_fields(avail)
        # industry / sector are BUILTIN_GROUPS — should be excluded
        assert "industry" not in s.promising_fields
        assert "sector" not in s.promising_fields
        assert "close" in s.promising_fields

    def test_picks_by_coverage(self):
        avail = [
            {"id": "low_cov", "coverage": 0.1},
            {"id": "high_cov", "coverage": 0.99},
            {"id": "mid_cov", "coverage": 0.5},
        ]
        s = _fill_default_with_top_fields(avail)
        # Top is high_cov
        assert s.promising_fields[0] == "high_cov"


# ---------------------------------------------------------------------------
# select_t1_strategy_via_llm — LLM mock paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_t1_strategy_uses_llm_when_succeeds():
    """LLM returns a valid T1Strategy; method returns it directly."""
    fake_strategy = T1Strategy(
        economic_hypothesis="LLM-picked story",
        signal_velocity="FUNDAMENTAL_SLOW",
        window_scale="LONG",
        promising_fields=["fnd6_test_field"],
        preferred_ts_ops=["ts_rank", "ts_zscore"],
        rationale="LLM rationale",
    )
    fake_response = MagicMock(success=True, error=None)

    llm = MagicMock()
    llm.call_with_schema = AsyncMock(return_value=(fake_strategy, fake_response))

    out = await select_t1_strategy_via_llm(
        dataset_id="fundamental2", region="USA",
        available_fields=[{"id": "fnd6_test_field", "coverage": 0.9}],
        success_patterns=[], llm_service=llm,
    )

    assert out.economic_hypothesis == "LLM-picked story"
    assert out.window_scale == "LONG"


@pytest.mark.asyncio
async def test_select_t1_strategy_falls_back_on_llm_failure():
    """LLM returns success=False → DEFAULT_T1_STRATEGY (with field salvage)."""
    fake_response = MagicMock(success=False, error="rate limit")
    llm = MagicMock()
    llm.call_with_schema = AsyncMock(return_value=(None, fake_response))

    out = await select_t1_strategy_via_llm(
        dataset_id="x", region="USA",
        available_fields=[{"id": "close", "coverage": 0.99}],
        success_patterns=[], llm_service=llm,
    )

    # Default fallback rationale
    assert "default fallback" in out.rationale.lower()
    # Top-coverage field salvage
    assert "close" in out.promising_fields


@pytest.mark.asyncio
async def test_select_t1_strategy_fills_empty_promising_fields():
    """LLM returns valid strategy but empty promising_fields → salvage from top-coverage."""
    fake_strategy = T1Strategy(
        economic_hypothesis="x",
        signal_velocity="MEDIUM",
        window_scale="MEDIUM",
        promising_fields=[],  # empty!
        preferred_ts_ops=["ts_rank"],
    )
    fake_response = MagicMock(success=True, error=None)
    llm = MagicMock()
    llm.call_with_schema = AsyncMock(return_value=(fake_strategy, fake_response))

    out = await select_t1_strategy_via_llm(
        dataset_id="x", region="USA",
        available_fields=[{"id": "close", "coverage": 0.99}],
        success_patterns=[], llm_service=llm,
    )

    # Salvaged from top fields
    assert "close" in out.promising_fields


@pytest.mark.asyncio
async def test_select_t1_strategy_handles_exception():
    """LLM raises → DEFAULT_T1_STRATEGY (no crash)."""
    llm = MagicMock()
    llm.call_with_schema = AsyncMock(side_effect=RuntimeError("network down"))

    out = await select_t1_strategy_via_llm(
        dataset_id="x", region="USA",
        available_fields=[{"id": "close", "coverage": 0.99}],
        success_patterns=[], llm_service=llm,
    )
    assert "default fallback" in out.rationale.lower()
