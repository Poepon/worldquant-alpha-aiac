"""P2 portfolio-aware prompt injection (2026-05-08).

Goal: stop LLM from re-generating shapes already in user's submitted
portfolio. Soft guidance — not a hard ban — via prompt section listing
skeletons of submitted alpha + diversification guidance.

Tests:
1. format_block: empty portfolio → empty string (no-op)
2. format_block: single entry → renders skeleton + expr + sharpe
3. format_block: multiple entries with same skeleton → grouped
4. build_t1_strategy_user_prompt picks up cache when present
5. cache miss → prompt unchanged (graceful)
"""
from pathlib import Path
import json

from backend.agents.seed_pool.portfolio_skeletons import (
    format_block, load_portfolio, _cache_path,
)
from backend.agents.prompts.strategy_prompts import build_t1_strategy_user_prompt


class TestFormatBlock:
    def test_empty_portfolio(self):
        assert format_block([]) == ""

    def test_single_entry(self):
        portfolio = [{
            "alpha_id": "X1", "skeleton": "rank(returns)",
            "expression": "rank(returns)", "sharpe": 1.5, "fitness": 1.1,
        }]
        out = format_block(portfolio)
        assert "ALREADY HAS SUBMITTED" in out
        assert "rank(returns)" in out
        assert "sh=1.50" in out

    def test_grouped_skeletons(self):
        portfolio = [
            {"alpha_id": "X1", "skeleton": "ts_rank(F, N)", "expression": "ts_rank(close, 20)",  "sharpe": 1.3},
            {"alpha_id": "X2", "skeleton": "ts_rank(F, N)", "expression": "ts_rank(close, 60)",  "sharpe": 1.5},
            {"alpha_id": "X3", "skeleton": "rank(F)",       "expression": "rank(returns)",       "sharpe": 1.2},
        ]
        out = format_block(portfolio)
        # Expect grouped: ts_rank(F, N) shows 2× with sharpe range
        assert "ts_rank(F, N)" in out
        assert "(2×" in out
        assert "rank(F)" in out
        # Example shown for highest-sharpe entry within group
        assert "ts_rank(close, 60)" in out

    def test_diversification_guidance_present(self):
        portfolio = [{
            "alpha_id": "X1", "skeleton": "X", "expression": "X", "sharpe": 1.0,
        }]
        out = format_block(portfolio)
        assert "GUIDANCE" in out
        assert "NOT in the list" in out


class TestPromptIntegration:
    def _setup_cache(self, region: str, portfolio: list):
        from backend.agents.seed_pool.portfolio_skeletons import CACHE_DIR
        from datetime import datetime, timezone
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(region)
        payload = {
            "region": region,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "portfolio": portfolio,
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)

    def _teardown_cache(self, region: str):
        path = _cache_path(region)
        if path.exists():
            path.unlink()

    def test_prompt_picks_up_portfolio_block(self):
        region = "TEST_REGION"
        portfolio = [{
            "alpha_id": "TEST_AID",
            "skeleton": "ts_zscore(F, N)",
            "expression": "ts_zscore(close, 60)",
            "sharpe": 1.4,
            "fitness": 1.0,
        }]
        self._setup_cache(region, portfolio)
        try:
            prompt = build_t1_strategy_user_prompt(
                dataset_id="pv1", region=region,
                available_fields=[{"id": "close", "type": "MATRIX", "coverage": 0.99}],
            )
            assert "ALREADY HAS SUBMITTED" in prompt
            assert "ts_zscore(F, N)" in prompt
            assert "ts_zscore(close, 60)" in prompt
        finally:
            self._teardown_cache(region)

    def test_prompt_unchanged_when_no_cache(self):
        # Use a region that doesn't have a cache file
        region = "NONEXISTENT_REGION_XYZ"
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region=region,
            available_fields=[{"id": "close", "type": "MATRIX", "coverage": 0.99}],
        )
        # Portfolio block should be absent (load_portfolio returns [] → format_block returns "")
        assert "ALREADY HAS SUBMITTED" not in prompt

    def test_prompt_with_real_usa_cache(self):
        """Sanity: when USA cache is populated (live state), prompt
        carries the section. This test passes when a real USA cache
        exists; otherwise self-skips."""
        portfolio = load_portfolio("USA")
        if not portfolio:
            return  # graceful skip — cache not initialized
        prompt = build_t1_strategy_user_prompt(
            dataset_id="pv1", region="USA",
            available_fields=[{"id": "close", "type": "MATRIX", "coverage": 0.99}],
        )
        assert "ALREADY HAS SUBMITTED" in prompt
