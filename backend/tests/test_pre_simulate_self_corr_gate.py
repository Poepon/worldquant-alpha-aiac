"""Pre-simulate self-correlation gate (2026-05-09).

User rule: "alpha 表达式在提交回测之前, 先本地检查自相关性, 如果不通过,
不能提交回测".

Implementation: skeleton match against submitted portfolio cache. Any
candidate whose expression skeleton (depth=3) matches a submitted
alpha's skeleton is dropped before BRAIN simulate (would fail server-
side self-correlation gate at submission anyway, so simulating wastes
BRAIN config quota).

Lookup is O(1) hashset against
backend/data/correlation_cache/submitted_portfolio_{region}.json.
"""
import json
from datetime import datetime, timezone

from backend.agents.seed_pool.portfolio_skeletons import (
    _cache_path,
    get_portfolio_skeleton_set,
    is_skeleton_in_portfolio,
)


class TestSkeletonSetLoad:
    def _setup_cache(self, region: str, portfolio: list):
        from backend.agents.seed_pool.portfolio_skeletons import CACHE_DIR
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _cache_path(region).open("w", encoding="utf-8") as f:
            json.dump({
                "region": region,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "portfolio": portfolio,
            }, f)

    def _teardown(self, region: str):
        path = _cache_path(region)
        if path.exists():
            path.unlink()

    def test_empty_cache_empty_set(self):
        region = "TEST_EMPTY_REGION"
        self._teardown(region)
        try:
            assert get_portfolio_skeleton_set(region) == set()
        finally:
            self._teardown(region)

    def test_loads_skeletons(self):
        region = "TEST_LOAD_REGION"
        portfolio = [
            {"alpha_id": "X1", "skeleton": "ts_rank(F, N)", "expression": "ts_rank(close, 20)"},
            {"alpha_id": "X2", "skeleton": "rank(F)",       "expression": "rank(returns)"},
            {"alpha_id": "X3", "skeleton": "",              "expression": "should_skip"},
        ]
        self._setup_cache(region, portfolio)
        try:
            skels = get_portfolio_skeleton_set(region)
            assert "ts_rank(F, N)" in skels
            assert "rank(F)" in skels
            assert "" not in skels  # empty skeleton filtered
        finally:
            self._teardown(region)


class TestIsSkeletonInPortfolio:
    def _setup_cache(self, region: str, skeletons: list[str]):
        from backend.agents.seed_pool.portfolio_skeletons import CACHE_DIR
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        portfolio = [{"alpha_id": f"X{i}", "skeleton": s, "expression": "x"}
                     for i, s in enumerate(skeletons)]
        with _cache_path(region).open("w", encoding="utf-8") as f:
            json.dump({"region": region, "saved_at": "x", "portfolio": portfolio}, f)

    def _teardown(self, region: str):
        path = _cache_path(region)
        if path.exists():
            path.unlink()

    def test_match_returns_true(self):
        """expression_to_skeleton('ts_rank(close, 20)') == 'ts_rank(FIELD, NUM)'.
        If portfolio has that skeleton, the function returns True."""
        from backend.knowledge_extraction import expression_to_skeleton
        region = "TEST_MATCH_REGION"
        sk_target = expression_to_skeleton("ts_rank(close, 20)", max_depth=3)
        self._setup_cache(region, [sk_target])
        try:
            assert is_skeleton_in_portfolio("ts_rank(close, 20)", region)
            # Different field but same skeleton — should still match
            assert is_skeleton_in_portfolio("ts_rank(returns, 60)", region)
        finally:
            self._teardown(region)

    def test_no_match_returns_false(self):
        from backend.knowledge_extraction import expression_to_skeleton
        region = "TEST_NOMATCH_REGION"
        sk = expression_to_skeleton("ts_zscore(returns, 60)", max_depth=3)
        self._setup_cache(region, [sk])
        try:
            assert not is_skeleton_in_portfolio("rank(close)", region)
        finally:
            self._teardown(region)

    def test_empty_expression(self):
        region = "TEST_EMPTY_EXPR_REGION"
        self._setup_cache(region, ["something"])
        try:
            assert not is_skeleton_in_portfolio("", region)
        finally:
            self._teardown(region)

    def test_empty_cache(self):
        region = "TEST_NO_CACHE_REGION"
        self._teardown(region)
        try:
            assert not is_skeleton_in_portfolio("ts_rank(close, 20)", region)
        finally:
            self._teardown(region)
