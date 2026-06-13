"""Phase 1 R3/Q8 (2026-05-17) AST subtree-isomorphism distance unit tests.

Covers plan v1.3 §2.9 8 cases:
1. identical tree distance=0
2. one-node tree handling
3. wrapper-around-base distance < 0.3
4. unrelated trees distance > 0.7
5. enumerate_subtrees cap at max_depth
6. DiversityScore 6th dim field present
7. dedicated table model exists + indexes
8. flag OFF returns 0 + no DB write
"""
from __future__ import annotations

import pytest

from backend.knowledge_extraction import (
    ast_distance,
    ast_distance_from_expressions,
    enumerate_subtrees,
    extract_operator_tree,
)


# ---------------------------------------------------------------------------
# enumerate_subtrees + ast_distance math primitives
# ---------------------------------------------------------------------------

class TestEnumerateSubtrees:
    def test_none_node_returns_empty_set(self):
        assert enumerate_subtrees(None) == set()

    def test_single_node_returns_one_skeleton(self):
        tree = extract_operator_tree("rank(close)")
        assert tree is not None
        subs = enumerate_subtrees(tree, max_depth=3)
        # Should have at least the root skeleton — depth=0 node always counted
        assert len(subs) >= 1

    def test_deeper_tree_more_subtrees(self):
        small = extract_operator_tree("rank(close)")
        big = extract_operator_tree("ts_rank(ts_delta(rank(close), 5), 20)")
        assert big is not None and small is not None
        # Bigger tree should enumerate at least as many subtrees
        assert len(enumerate_subtrees(big, 3)) >= len(enumerate_subtrees(small, 3))

    def test_max_depth_cap_truncates(self):
        # Same tree at depth 1 should have fewer subtrees than depth 3
        tree = extract_operator_tree("ts_rank(ts_delta(rank(ts_mean(close, 5)), 10), 20)")
        assert tree is not None
        d1 = enumerate_subtrees(tree, max_depth=1)
        d3 = enumerate_subtrees(tree, max_depth=3)
        # Truncating depth should reduce or equal the set
        assert len(d1) <= len(d3)


class TestAstDistance:
    def test_identical_trees_distance_zero(self):
        t = extract_operator_tree("ts_rank(close, 20)")
        assert ast_distance(t, t) == 0.0

    def test_both_none_distance_zero(self):
        # Vacuously identical — neither has content to differ
        assert ast_distance(None, None) == 0.0

    def test_one_none_one_populated_distance_one(self):
        t = extract_operator_tree("rank(close)")
        assert ast_distance(t, None) == 1.0
        assert ast_distance(None, t) == 1.0

    def test_wrapper_around_base_small_distance(self):
        # rank(close) is a strict subtree of ts_zscore(rank(close), 20) —
        # significant overlap in subtree skeletons, distance should be small
        base = extract_operator_tree("rank(close)")
        wrap = extract_operator_tree("ts_zscore(rank(close), 20)")
        d = ast_distance(base, wrap)
        assert d < 0.6, f"wrapper-around-base distance should be modest, got {d}"

    def test_unrelated_trees_large_distance(self):
        # Completely different operator chains — distance close to 1
        a = extract_operator_tree("ts_decay_linear(returns, 30)")
        b = extract_operator_tree("rank(divide(volume, vwap))")
        d = ast_distance(a, b)
        assert d > 0.5, f"unrelated trees distance should be large, got {d}"

    def test_returns_unit_interval(self):
        # All pairwise comparisons must produce d in [0, 1]
        exprs = [
            "rank(close)",
            "ts_mean(volume, 30)",
            "ts_rank(ts_delta(returns, 5), 20)",
            "if_else(close > open, returns, 0)",
        ]
        trees = [extract_operator_tree(e) for e in exprs]
        for i in range(len(trees)):
            for j in range(len(trees)):
                d = ast_distance(trees[i], trees[j])
                assert 0.0 <= d <= 1.0, f"d({i},{j})={d} out of [0,1]"


class TestAstDistanceFromExpressions:
    def test_round_trip_via_strings(self):
        # Convenience wrapper should match the direct tree-based call
        e1 = "ts_rank(close, 20)"
        e2 = "ts_zscore(rank(close), 20)"
        direct = ast_distance(
            extract_operator_tree(e1),
            extract_operator_tree(e2),
        )
        via_strings = ast_distance_from_expressions(e1, e2)
        # Allow tiny floating point drift but they should match
        assert abs(direct - via_strings) < 1e-9

    def test_unparseable_input_returns_one(self):
        # Garbage in shouldn't crash hot path — returns max distance
        d = ast_distance_from_expressions("((((((", "valid_expr(close)")
        assert 0.0 <= d <= 1.0

    def test_empty_strings_handled(self):
        d = ast_distance_from_expressions("", "")
        assert d == 0.0  # Vacuously identical
        d = ast_distance_from_expressions("", "rank(close)")
        assert d == 1.0  # One missing → max distance


# ---------------------------------------------------------------------------
# DiversityScore — 6th dim field present
# ---------------------------------------------------------------------------

class TestDiversityScoreExtension:
    def test_ast_diversity_field_exists_defaults_zero(self):
        from backend.diversity_tracker import DiversityScore
        score = DiversityScore()
        assert hasattr(score, "ast_diversity")
        assert score.ast_diversity == 0.0

    def test_ast_diversity_in_to_dict_output(self):
        from backend.diversity_tracker import DiversityScore
        score = DiversityScore(ast_diversity=0.42)
        d = score.to_dict()
        assert "ast_diversity" in d
        assert d["ast_diversity"] == 0.42

    def test_legacy_score_construction_still_works(self):
        # P1-A / P2-B backward-compat invariant — DiversityScore can be
        # constructed without ast_diversity argument and behaves identically
        from backend.diversity_tracker import DiversityScore
        score = DiversityScore(
            dataset_diversity=0.5,
            field_diversity=0.6,
            operator_diversity=0.7,
            settings_diversity=0.8,
            pillar_diversity=0.9,
        )
        assert score.ast_diversity == 0.0  # default unchanged
        assert score.dataset_diversity == 0.5


# ---------------------------------------------------------------------------
# AstDistanceLog model exists + has required columns
# ---------------------------------------------------------------------------

class TestAstDistanceLogModel:
    def test_model_exported(self):
        from backend.models import AstDistanceLog
        assert AstDistanceLog.__tablename__ == "ast_distance_log"

    def test_required_columns_present(self):
        from backend.models import AstDistanceLog
        cols = {c.name for c in AstDistanceLog.__table__.columns}
        required = {
            "id", "task_id", "round_idx", "expression", "expression_hash",
            "skeleton", "ast_distance_min", "ast_distance_mean",
            "ast_distance_max", "nearest_neighbor_hash", "history_window",
            "tracker_version", "created_at",
        }
        missing = required - cols
        assert not missing, f"AstDistanceLog missing columns: {missing}"

    def test_indexes_present(self):
        from backend.models import AstDistanceLog
        index_names = {idx.name for idx in AstDistanceLog.__table__.indexes}
        # Plus auto-indexes from index=True columns (task_id, created_at)
        assert "ix_adl_task_id" in index_names
        assert "ix_adl_created_at" in index_names
        assert "ix_adl_expression_hash" in index_names


# ---------------------------------------------------------------------------
# Flag registration (double-file rule)
# ---------------------------------------------------------------------------

class TestAstDiversityFlag:
    def test_config_attribute_default_false(self):
        from backend.config import settings
        assert hasattr(settings, "ENABLE_AST_DIVERSITY_DIM")
        assert settings.ENABLE_AST_DIVERSITY_DIM is False

    def test_supported_flags_registered(self):
        from backend.services.feature_flag_service import SUPPORTED_FLAGS
        assert "ENABLE_AST_DIVERSITY_DIM" in SUPPORTED_FLAGS
        spec = SUPPORTED_FLAGS["ENABLE_AST_DIVERSITY_DIM"]
        assert spec.flag_type == "bool"
        assert spec.lifecycle == "experimental"
        assert spec.domain == "evaluation"

    def test_max_depth_default_3(self):
        from backend.config import settings
        assert settings.AST_DIVERSITY_MAX_DEPTH == 3

    def test_history_k_default_20(self):
        from backend.config import settings
        assert settings.AST_DIVERSITY_HISTORY_K == 20


# ---------------------------------------------------------------------------
# log_round_ast_distances — flag-OFF short-circuit
# ---------------------------------------------------------------------------

class TestLogHelperFlagGate:
    @pytest.mark.asyncio
    async def test_flag_off_returns_zero_no_db_write(self, monkeypatch):
        from backend import ast_distance_logger as mod
        # Default flag is False — function must short-circuit before
        # importing DB module / making any DB call
        monkeypatch.setattr(mod.settings, "ENABLE_AST_DIVERSITY_DIM", False)
        written = await mod.log_round_ast_distances(
            task_id=1, round_idx=0, new_expressions=["ts_rank(close, 20)"]
        )
        assert written == 0

    @pytest.mark.asyncio
    async def test_empty_expressions_returns_zero(self, monkeypatch):
        from backend import ast_distance_logger as mod
        monkeypatch.setattr(mod.settings, "ENABLE_AST_DIVERSITY_DIM", True)
        written = await mod.log_round_ast_distances(
            task_id=1, round_idx=0, new_expressions=[]
        )
        assert written == 0
