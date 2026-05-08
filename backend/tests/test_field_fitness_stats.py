"""#2 field-fitness aware prompt injection (2026-05-08).

LLM was picking "interesting" fields without knowing which historically
deliver fit ≥ 1.0 (BRAIN's submission gate). Inject empirical median-fit
ranking into T1 strategy prompt so high-fit families are preferred.

Tests cover: format_block thresholding, CW-prone exclusion, cache load,
prompt integration via build_t1_strategy_user_prompt.
"""
import json
from datetime import datetime, timezone

from backend.agents.seed_pool.field_fitness_stats import (
    _is_cw_prone,
    _cache_path,
    format_block,
    load_high_fit_fields,
)
from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt


class TestCWProneFilter:
    def test_derivative_blocked(self):
        assert _is_cw_prone("multi_factor_acceleration_score_derivative")
        assert _is_cw_prone("growth_potential_rank_derivative")

    def test_iv_blocked(self):
        assert _is_cw_prone("implied_volatility_call_30")

    def test_bbg_pyth_blocked(self):
        assert _is_cw_prone("pv96_bbg_dvd_cash_cg_amt")
        assert _is_cw_prone("some_pyth_field")

    def test_clean_field_passes(self):
        assert not _is_cw_prone("anl4_adjusted_netincome_ft")
        assert not _is_cw_prone("close")
        assert not _is_cw_prone("fnd6_newa2v1300_ni")


class TestFormatBlock:
    def test_empty_fields_returns_empty(self):
        assert format_block([]) == ""

    def test_below_threshold_returns_empty(self):
        # All fields have median < 0.7 → empty block
        fields = [{
            "field_id": "low", "median_fit": 0.5, "avg_fit": 0.5,
            "max_fit": 0.5, "n_alpha": 3, "n_fit_ge_1": 0,
        }]
        assert format_block(fields, min_median=0.7) == ""

    def test_above_threshold_renders(self):
        fields = [{
            "field_id": "anl4_test", "median_fit": 2.22, "avg_fit": 2.0,
            "max_fit": 2.5, "n_alpha": 3, "n_fit_ge_1": 2,
        }]
        out = format_block(fields, min_median=0.7)
        assert "HIGH-FITNESS" in out
        assert "anl4_test" in out
        assert "2.22" in out
        assert "GUIDANCE" in out

    def test_sorted_descending(self):
        fields = [
            {"field_id": "low", "median_fit": 0.8, "avg_fit": 0.8, "max_fit": 1.0, "n_alpha": 5, "n_fit_ge_1": 1},
            {"field_id": "high", "median_fit": 2.0, "avg_fit": 1.8, "max_fit": 2.5, "n_alpha": 3, "n_fit_ge_1": 2},
            {"field_id": "mid", "median_fit": 1.2, "avg_fit": 1.1, "max_fit": 1.5, "n_alpha": 5, "n_fit_ge_1": 3},
        ]
        out = format_block(fields, min_median=0.7)
        # Higher median should appear before lower
        assert out.index("high") < out.index("mid") < out.index("low")


class TestPromptIntegration:
    def _setup_cache(self, region: str, fields: list):
        from backend.agents.seed_pool.field_fitness_stats import CACHE_DIR
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(region)
        payload = {
            "region": region,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "min_alpha_count": 3,
            "fields": fields,
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _teardown_cache(self, region: str):
        path = _cache_path(region)
        if path.exists():
            path.unlink()

    def test_prompt_includes_high_fit_section(self):
        region = "TEST_FIT_REGION"
        fields = [{
            "field_id": "anl4_test", "median_fit": 2.0, "avg_fit": 1.8,
            "max_fit": 2.5, "n_alpha": 3, "n_fit_ge_1": 2,
        }]
        self._setup_cache(region, fields)
        try:
            prompt = build_t1_strategy_user_prompt(
                dataset_id="pv1", region=region,
                available_fields=[{"id": "close", "type": "MATRIX", "coverage": 0.99}],
            )
            assert "HIGH-FITNESS" in prompt
            assert "anl4_test" in prompt
        finally:
            self._teardown_cache(region)

    def test_prompt_no_section_when_empty_cache(self):
        region = "NONEXISTENT_FIT_REGION_XYZ"
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region=region,
            available_fields=[{"id": "close", "type": "MATRIX", "coverage": 0.99}],
        )
        assert "HIGH-FITNESS" not in prompt

    def test_real_usa_cache_renders(self):
        """When real USA cache exists (live state), prompt carries section."""
        fields = load_high_fit_fields("USA")
        if not fields:
            return  # graceful skip
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA",
            available_fields=[{"id": "close", "type": "MATRIX", "coverage": 0.99}],
        )
        assert "HIGH-FITNESS" in prompt
