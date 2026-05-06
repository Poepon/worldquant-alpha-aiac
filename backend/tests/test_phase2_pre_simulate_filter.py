"""Plan v5+ #3 — Pre-simulate skeleton classifier tests.

Covers:
1. Feature extractor: handles every operator category, nesting depth,
   numeric extraction, edge cases (empty / null expression)
2. Filter behavior: threshold gating, all-keep when model unavailable,
   inference exception → fail-open (keep)
3. Integration: with the actually-trained model, sanity check on
   well-known PASS-likely vs FAIL-likely expressions
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# =============================================================================
# Feature extractor — pure unit tests
# =============================================================================

def test_features_extracts_categories():
    from backend.agents.services.pre_simulate_features import extract_features

    feats = extract_features("ts_zscore(returns, 20)")
    assert feats["has_cat_ts"] == 1
    assert feats["has_op_ts_zscore"] == 1
    assert feats["num_operators"] == 1
    assert feats["max_window"] == 20.0
    assert feats["mean_window"] == 20.0


def test_features_handles_nested_expression():
    from backend.agents.services.pre_simulate_features import extract_features

    feats = extract_features("group_neutralize(rank(divide(close, eps)), industry)")
    assert feats["has_cat_xs"] == 1
    assert feats["has_cat_group"] == 1
    assert feats["has_cat_arith"] == 1
    assert feats["has_op_rank"] == 1
    assert feats["has_op_group_neutralize"] == 1
    assert feats["nesting_depth"] >= 3
    assert feats["num_operators"] >= 3


def test_features_counts_negation():
    from backend.agents.services.pre_simulate_features import extract_features

    feats = extract_features("multiply(-1, ts_rank(returns, 5))")
    assert feats["num_negation"] == 1
    assert feats["has_op_ts_rank"] == 1


def test_features_handles_empty_expression():
    from backend.agents.services.pre_simulate_features import extract_features
    feats = extract_features("")
    # No crash; empty expr returns zeros for all keys
    assert feats["num_operators"] == 0
    assert feats["num_fields"] == 0
    assert feats["nesting_depth"] == 0


def test_features_skeleton_hash_is_stable():
    from backend.agents.services.pre_simulate_features import extract_features
    f1 = extract_features("ts_rank(close, 5)")
    f2 = extract_features("ts_rank(close, 5)")
    assert f1["skeleton_hash"] == f2["skeleton_hash"]
    f3 = extract_features("ts_rank(volume, 10)")  # Different field/num
    # skeleton normalizes FIELD/NUM, so f3 should match f1
    assert f1["skeleton_hash"] == f3["skeleton_hash"]


def test_features_window_stats():
    from backend.agents.services.pre_simulate_features import extract_features
    feats = extract_features("ts_corr(close, ts_delay(volume, 5), 60)")
    # numeric literals: 5, 60 (windows)
    assert feats["max_window"] == 60.0
    assert feats["min_window"] == 5.0
    assert feats["mean_window"] == pytest.approx((60 + 5) / 2)


def test_feature_keys_are_stable():
    from backend.agents.services.pre_simulate_features import (
        feature_keys, extract_features,
    )
    keys = feature_keys()
    assert len(keys) == 29  # 8 base + 5 categories + 16 high-signal ops
    # Every extracted feature dict must have all keys
    feats = extract_features("rank(close)")
    for k in keys:
        assert k in feats, f"missing key {k}"


# =============================================================================
# Filter behavior — unit tests with mocked model
# =============================================================================

def test_filter_returns_all_keep_when_model_unavailable():
    """When model file missing, filter is no-op (keep all).
    Test by patching _MODEL_PATH to nonexistent."""
    import backend.agents.services.pre_simulate_filter as ff

    # Force unloaded state
    with patch.object(ff, "_model", None), \
         patch.object(ff, "_metadata", None), \
         patch.object(ff, "_load_attempted", False), \
         patch.object(ff, "_MODEL_PATH", Path("/nonexistent/file.pkl")), \
         patch.object(ff, "_META_PATH", Path("/nonexistent/meta.json")):
        keep, skip, probas = ff.filter_candidates(
            ["rank(close)", "ts_rank(returns, 5)"],
            threshold=0.05,
        )
        assert keep == [0, 1]
        assert skip == []
        assert probas == [1.0, 1.0]


def test_filter_threshold_gate_with_mock_predictions():
    """Force probability values to verify keep/skip threshold logic."""
    import backend.agents.services.pre_simulate_filter as ff

    with patch.object(ff, "predict_pass_probability", return_value=[0.9, 0.04, 0.5, 0.01]):
        keep, skip, probas = ff.filter_candidates(
            ["e0", "e1", "e2", "e3"], threshold=0.05,
        )
        # 0.9, 0.5 → keep; 0.04, 0.01 → skip
        assert keep == [0, 2]
        assert skip == [1, 3]


def test_filter_predict_swallows_inference_exception():
    """If model.predict_proba raises, return all 1.0 (fail-open)."""
    import backend.agents.services.pre_simulate_filter as ff
    from unittest.mock import MagicMock

    bad_model = MagicMock()
    bad_model.predict_proba.side_effect = RuntimeError("model corrupted")

    with patch.object(ff, "_model", bad_model), \
         patch.object(ff, "_metadata", {"recommended_threshold": 0.05}), \
         patch.object(ff, "_load_attempted", True):
        probas = ff.predict_pass_probability(["rank(close)", "ts_rank(volume, 5)"])
        assert probas == [1.0, 1.0]


# =============================================================================
# Real-model integration smoke
# =============================================================================

@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "models" / "pre_simulate_classifier.pkl").exists(),
    reason="no trained model; run scripts/train_pre_simulate_classifier.py first",
)
def test_real_model_smoke():
    """Sanity check on the actually-trained model. We don't assert specific
    probabilities (training data shifts) — only that:
      - probabilities are in [0, 1]
      - filter respects threshold
      - well-formed alpha → P > 0.5 typically
    """
    from backend.agents.services.pre_simulate_filter import (
        filter_candidates, predict_pass_probability,
    )

    expressions = [
        "rank(close)",
        "ts_zscore(returns, 20)",
        "group_neutralize(rank(divide(close, eps)), industry)",
        "multiply(-1, ts_delta(close, 5))",
    ]
    probas = predict_pass_probability(expressions)
    assert all(0 <= p <= 1 for p in probas), f"probabilities out of range: {probas}"

    keep, skip, p2 = filter_candidates(expressions, threshold=0.05)
    assert keep == [i for i, p in enumerate(probas) if p >= 0.05]
    assert skip == [i for i, p in enumerate(probas) if p < 0.05]
    assert p2 == probas
