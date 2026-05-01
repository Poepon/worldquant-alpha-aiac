"""Tests for backend.factor_wrapping — T2 / T3 LLM-guided wrapping + expansion."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.factor_tier_classifier import classify_tier, populate_known_fields
from backend.factor_wrapping import (
    DEFAULT_T2_STRATEGY,
    DEFAULT_T3_STRATEGY,
    TRADE_WHEN_TEMPLATES,
    T2Strategy,
    T3Strategy,
    _allowed_groups,
    expand_t2_strategy,
    expand_t3_strategy,
    select_t2_strategy_via_llm,
    select_t3_strategy_via_llm,
    template_available,
)


populate_known_fields({"close", "volume", "returns", "vwap"})


# ---------------------------------------------------------------------------
# T2 expansion
# ---------------------------------------------------------------------------

class TestExpandT2Strategy:
    def test_default_strategy_produces_t2_variants(self):
        seed = "ts_rank(close, 20)"
        out = expand_t2_strategy(seed, DEFAULT_T2_STRATEGY, region="USA")
        # default has: 1 group_neutralize + 1 group_rank + 2 pure_xs + 1 smoothing = 5
        assert len(out) == 5
        for v in out:
            assert classify_tier(v["expression"]) == 2

    def test_chn_drops_sector_groups(self):
        """CHN region has no sector — sector group choices should be filtered out."""
        strat = T2Strategy(
            signal_velocity="MEDIUM",
            use_group_neutralize=["industry", "sector"],  # sector should be dropped for CHN
        )
        out = expand_t2_strategy("ts_rank(close, 20)", strat, region="CHN")
        wrapper_kinds = {v["wrapper_kind"] for v in out}
        assert "group_neutralize_industry" in wrapper_kinds
        assert "group_neutralize_sector" not in wrapper_kinds

    def test_pure_xs_winsorize_uses_std_param(self):
        strat = T2Strategy(signal_velocity="MEDIUM", use_pure_xs=["winsorize"])
        out = expand_t2_strategy("ts_rank(close, 20)", strat, region="USA")
        assert any("winsorize(ts_rank(close, 20), std=4)" == v["expression"] for v in out)

    def test_signed_power_uses_exponent(self):
        strat = T2Strategy(signal_velocity="MEDIUM", use_pure_xs=["signed_power"])
        out = expand_t2_strategy("ts_rank(close, 20)", strat, region="USA")
        assert any("signed_power(ts_rank(close, 20), 0.5)" == v["expression"] for v in out)

    def test_smoothing_window_parsing(self):
        strat = T2Strategy(
            signal_velocity="MEDIUM",
            use_smoothing=["ts_decay_linear@10", "ts_mean@20"],
        )
        out = expand_t2_strategy("ts_rank(close, 20)", strat, region="USA")
        exprs = {v["expression"] for v in out}
        assert "ts_decay_linear(ts_rank(close, 20), 10)" in exprs
        assert "ts_mean(ts_rank(close, 20), 20)" in exprs

    def test_dedup_drops_duplicates(self):
        # group_neutralize_industry written twice = should dedup to one
        strat = T2Strategy(
            signal_velocity="MEDIUM",
            use_group_neutralize=["industry", "industry"],  # duplicate
        )
        out = expand_t2_strategy("ts_rank(close, 20)", strat, region="USA")
        assert len(out) == 1


class TestRegionGroups:
    def test_usa_has_sector(self):
        assert "sector" in _allowed_groups("USA")

    def test_chn_no_sector(self):
        assert "sector" not in _allowed_groups("CHN")
        assert "industry" in _allowed_groups("CHN")


# ---------------------------------------------------------------------------
# T3 expansion
# ---------------------------------------------------------------------------

class TestExpandT3Strategy:
    def test_default_strategy_produces_t3_variants(self):
        seed_t2 = "group_neutralize(ts_rank(close, 20), industry)"
        out = expand_t3_strategy(seed_t2, DEFAULT_T3_STRATEGY, region="USA")
        assert len(out) == 2  # default has [high_volume_entry, vol_spike_entry]
        for v in out:
            assert classify_tier(v["expression"]) == 3
            assert v["wrapper_kind"].startswith("trade_when_")

    def test_chn_drops_earnings_template(self):
        """earnings_entry needs days_to_announcement which CHN lacks — should be filtered."""
        strat = T3Strategy(
            signal_velocity="MEDIUM",
            use_templates=["high_volume_entry", "earnings_entry"],
        )
        out = expand_t3_strategy(
            "rank(ts_rank(close, 20))", strat, region="CHN"
        )
        wrapper_kinds = {v["wrapper_kind"] for v in out}
        assert "trade_when_high_volume_entry" in wrapper_kinds
        assert "trade_when_earnings_entry" not in wrapper_kinds

    def test_template_available(self):
        assert template_available("USA", "earnings_entry") is True
        assert template_available("CHN", "earnings_entry") is False
        assert template_available("USA", "unknown_template") is False
        assert template_available("USA", "high_volume_entry") is True


# ---------------------------------------------------------------------------
# select_t2_strategy_via_llm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_t2_strategy_uses_llm_when_succeeds():
    fake_strategy = T2Strategy(
        signal_velocity="FAST",
        signal_source="pv",
        is_normalized=False,
        use_group_neutralize=["industry", "subindustry"],
        use_pure_xs=["rank"],
        rationale="LLM picked these",
    )
    fake_response = MagicMock(success=True, error=None)
    llm = MagicMock()
    llm.call_with_schema = AsyncMock(return_value=(fake_strategy, fake_response))

    out = await select_t2_strategy_via_llm(
        seed_expression="ts_rank(close, 20)",
        seed_metrics={"sharpe": 1.2, "fitness": 0.9, "turnover": 0.3},
        region="USA", dataset_id="pv1", llm_service=llm,
    )
    assert out.signal_velocity == "FAST"
    assert out.use_group_neutralize == ["industry", "subindustry"]


@pytest.mark.asyncio
async def test_select_t2_strategy_falls_back_on_llm_failure():
    fake_response = MagicMock(success=False, error="parse error")
    llm = MagicMock()
    llm.call_with_schema = AsyncMock(return_value=(None, fake_response))

    out = await select_t2_strategy_via_llm(
        seed_expression="ts_rank(close, 20)", seed_metrics={},
        region="USA", dataset_id="pv1", llm_service=llm,
    )
    assert "default fallback" in out.rationale.lower()
    assert out == DEFAULT_T2_STRATEGY


@pytest.mark.asyncio
async def test_select_t2_strategy_handles_exception():
    llm = MagicMock()
    llm.call_with_schema = AsyncMock(side_effect=RuntimeError("connection refused"))

    out = await select_t2_strategy_via_llm(
        seed_expression="ts_rank(close, 20)", seed_metrics={},
        region="USA", dataset_id="pv1", llm_service=llm,
    )
    assert out == DEFAULT_T2_STRATEGY


# ---------------------------------------------------------------------------
# select_t3_strategy_via_llm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_select_t3_strategy_uses_llm_when_succeeds():
    fake_strategy = T3Strategy(
        signal_velocity="MEDIUM",
        use_templates=["trend_entry", "rebound_entry"],
        rationale="picked momentum templates",
    )
    fake_response = MagicMock(success=True, error=None)
    llm = MagicMock()
    llm.call_with_schema = AsyncMock(return_value=(fake_strategy, fake_response))

    out = await select_t3_strategy_via_llm(
        seed_t2_expression="group_neutralize(ts_rank(close, 20), industry)",
        seed_metrics={}, region="USA", dataset_id="pv1", llm_service=llm,
    )
    assert "trend_entry" in out.use_templates


@pytest.mark.asyncio
async def test_select_t3_strategy_falls_back_on_failure():
    fake_response = MagicMock(success=False, error="x")
    llm = MagicMock()
    llm.call_with_schema = AsyncMock(return_value=(None, fake_response))

    out = await select_t3_strategy_via_llm(
        seed_t2_expression="x", seed_metrics={}, region="USA", dataset_id="x", llm_service=llm,
    )
    assert out == DEFAULT_T3_STRATEGY


# ---------------------------------------------------------------------------
# Templates registry
# ---------------------------------------------------------------------------

class TestTradeWhenTemplates:
    def test_all_templates_have_expr_placeholder(self):
        for name, tpl in TRADE_WHEN_TEMPLATES.items():
            assert "{expr}" in tpl, f"template {name} missing {{expr}} placeholder"

    def test_template_substitution_yields_t3(self):
        seed_t2 = "group_neutralize(ts_rank(close, 20), industry)"
        for name, tpl in TRADE_WHEN_TEMPLATES.items():
            if name == "earnings_entry":
                continue  # USA-only field; skip in unit test
            expr = tpl.format(expr=seed_t2)
            assert classify_tier(expr) == 3, f"template {name} produced non-T3: {expr}"
