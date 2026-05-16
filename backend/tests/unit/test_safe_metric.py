"""
Unit tests for _safe_metric helper (P1-B).
来源: docs/alphagbm_skills_research_2026-05-15.md P1-B — fallback 降级
"""
import math
import pytest
from backend.agents.graph.nodes.evaluation import _safe_metric


class TestSafeMetric:
    """_safe_metric: NaN/inf/bool/str/missing → default + flag."""

    # ── numeric passthrough ───────────────────────────────────────────────────

    def test_float_positive(self):
        flags = []
        assert _safe_metric({"sharpe": 1.5}, "sharpe", 0.0, flags) == pytest.approx(1.5)
        assert flags == []

    def test_float_negative(self):
        flags = []
        assert _safe_metric({"sharpe": -1.5}, "sharpe", 0.0, flags) == pytest.approx(-1.5)
        assert flags == []

    def test_int_converts_to_float(self):
        flags = []
        result = _safe_metric({"sharpe": 1}, "sharpe", 0.0, flags)
        assert result == pytest.approx(1.0)
        assert isinstance(result, float)
        assert flags == []

    def test_zero_is_not_fallback(self):
        flags = []
        assert _safe_metric({"sharpe": 0}, "sharpe", 99.0, flags) == pytest.approx(0.0)
        assert flags == []

    def test_negative_zero(self):
        flags = []
        assert _safe_metric({"sharpe": -0.0}, "sharpe", 99.0, flags) == pytest.approx(0.0)
        assert flags == []

    # ── fallback triggers ─────────────────────────────────────────────────────

    def test_missing_key(self):
        flags = []
        result = _safe_metric({}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_explicit_none(self):
        flags = []
        result = _safe_metric({"sharpe": None}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_nan(self):
        flags = []
        result = _safe_metric({"sharpe": float("nan")}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_pos_inf(self):
        flags = []
        result = _safe_metric({"sharpe": float("inf")}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_neg_inf(self):
        flags = []
        result = _safe_metric({"sharpe": float("-inf")}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_bool_true_rejected(self):
        """True is a bool and bool ⊂ int — must NOT pass as numeric 1."""
        flags = []
        result = _safe_metric({"sharpe": True}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_bool_false_rejected(self):
        flags = []
        result = _safe_metric({"sharpe": False}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_string_rejected(self):
        flags = []
        result = _safe_metric({"sharpe": "1.5"}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_list_rejected(self):
        flags = []
        result = _safe_metric({"sharpe": [1.5]}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    def test_dict_rejected(self):
        flags = []
        result = _safe_metric({"sharpe": {"v": 1.5}}, "sharpe", 7.0, flags)
        assert result == pytest.approx(7.0)
        assert flags == ["sharpe"]

    # ── accumulation ─────────────────────────────────────────────────────────

    def test_flags_accumulate_across_calls(self):
        flags = []
        _safe_metric({}, "sharpe", 0.0, flags)
        _safe_metric({}, "fitness", 0.0, flags)
        _safe_metric({}, "turnover", 0.0, flags)
        assert flags == ["sharpe", "fitness", "turnover"]

    def test_flags_allow_duplicate_key(self):
        flags = []
        _safe_metric({}, "sharpe", 0.0, flags)
        _safe_metric({}, "sharpe", 0.0, flags)
        assert len(flags) == 2

    def test_no_mutation_of_input_dict(self):
        original = {"sharpe": float("nan")}
        flags = []
        _safe_metric(original, "sharpe", 0.0, flags)
        # dict not mutated; NaN equality is not == so use math.isnan
        assert math.isnan(original["sharpe"])

    # ── python semantics anchor ───────────────────────────────────────────────

    def test_bool_is_subclass_of_int(self):
        """Anchor: documents why bool check must precede (int, float) check."""
        assert isinstance(True, int) is True
        assert isinstance(True, bool) is True
