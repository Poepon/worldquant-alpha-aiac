"""
Unit tests for P1-A: compute_graded_score + GradedScore (backend/alpha_scoring.py)

来源: docs/alphagbm_skills_research_2026-05-15.md 原则① — 评分改百分位归一化 +
非均匀权重 + confidence 维度
"""

import pytest
from statistics import NormalDist

from backend.alpha_scoring import (
    GradedScore,
    _GRADE_BANDS,
    calculate_alpha_score,
    compute_graded_score,
)
from backend.baseline_screener import BaselineStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _usable_stats(mean: float = 1.0, std: float = 0.5) -> BaselineStats:
    """Construct a usable BaselineStats with known parameters."""
    return BaselineStats(mean=mean, std=std, count=100, cell_key="test|ds|USA", granularity="fine")


def _sim(sharpe: float = 1.5, fitness: float = 1.2, turnover: float = 0.3) -> dict:
    """Minimal sim_result dict accepted by calculate_alpha_score."""
    return {"train": {"sharpe": sharpe, "fitness": fitness, "turnover": turnover},
            "test":  {"sharpe": sharpe}}


# ---------------------------------------------------------------------------
# TestGradedScorePercentile
# ---------------------------------------------------------------------------

class TestGradedScorePercentile:
    """Percentile computation via normal-CDF of residual sigma."""

    def test_no_baseline_returns_05(self):
        gs = compute_graded_score(_sim(), baseline_stats=None)
        assert gs.percentile == pytest.approx(0.5, abs=0.01)

    def test_unusable_baseline_returns_05(self):
        stats = BaselineStats(0.0, 0.0, 5, "x", "insufficient")  # not usable
        gs = compute_graded_score(_sim(), baseline_stats=stats)
        assert gs.percentile == pytest.approx(0.5, abs=0.01)

    def test_sharpe_at_mean_gives_pct_half(self):
        stats = _usable_stats(mean=1.5, std=0.5)
        # sharpe == mean → z == 0 → CDF(0) == 0.5
        gs = compute_graded_score(_sim(sharpe=1.5), baseline_stats=stats)
        assert gs.percentile == pytest.approx(0.5, abs=0.01)

    def test_sharpe_above_mean_gives_pct_above_half(self):
        stats = _usable_stats(mean=1.0, std=0.5)
        gs = compute_graded_score(_sim(sharpe=1.5), baseline_stats=stats)  # z=+1
        assert gs.percentile > 0.5

    def test_sharpe_below_mean_gives_pct_below_half(self):
        stats = _usable_stats(mean=1.0, std=0.5)
        gs = compute_graded_score(_sim(sharpe=0.5), baseline_stats=stats)  # z=-1
        assert gs.percentile < 0.5

    def test_z2_gives_pct_approx_0977(self):
        """z=+2 should yield ~97.7% percentile → grade A."""
        stats = _usable_stats(mean=1.0, std=0.5)
        gs = compute_graded_score(_sim(sharpe=2.0), baseline_stats=stats)  # z=+2
        assert gs.percentile == pytest.approx(NormalDist().cdf(2.0), abs=0.001)
        assert gs.grade == "A"

    def test_z_minus2_gives_grade_e(self):
        """z=-2 → percentile ~0.023 → grade E."""
        stats = _usable_stats(mean=1.0, std=0.5)
        gs = compute_graded_score(_sim(sharpe=0.0), baseline_stats=stats)  # z=-2
        assert gs.percentile < 0.05
        assert gs.grade == "E"

    def test_percentile_clamped_to_unit_interval(self):
        stats = _usable_stats(mean=1.0, std=0.5)
        for sharpe in [-100.0, 0.0, 1.0, 2.0, 100.0]:
            gs = compute_graded_score(_sim(sharpe=sharpe), baseline_stats=stats)
            assert 0.0 <= gs.percentile <= 1.0


# ---------------------------------------------------------------------------
# TestGradedScoreGrade
# ---------------------------------------------------------------------------

class TestGradedScoreGrade:
    """Grade boundary conditions (cutoffs: 0.90 / 0.70 / 0.50 / 0.30 / 0.00)."""

    def _grade_for_pct(self, pct: float) -> tuple:
        """Derive grade by injecting an exact percentile via a contrived baseline."""
        # Build stats so that CDF(z) == pct, i.e. z = NormalDist().inv_cdf(pct)
        # Then set sharpe = mean + z*std so residual_sigma returns exactly z.
        pct_clamped = max(0.001, min(0.999, pct))
        z = NormalDist().inv_cdf(pct_clamped)
        std = 0.5
        mean = 1.0
        target_sharpe = mean + z * std
        stats = _usable_stats(mean=mean, std=std)
        gs = compute_graded_score(_sim(sharpe=target_sharpe), baseline_stats=stats)
        return gs.grade, gs.grade_action

    def test_grade_a_at_090(self):
        grade, action = self._grade_for_pct(0.901)
        assert grade == "A"
        assert action == "submit_priority"

    def test_grade_b_just_below_090(self):
        grade, action = self._grade_for_pct(0.899)
        assert grade == "B"
        assert action == "pass_normal"

    def test_grade_b_at_070(self):
        grade, action = self._grade_for_pct(0.701)
        assert grade == "B"

    def test_grade_c_just_below_070(self):
        grade, action = self._grade_for_pct(0.699)
        assert grade == "C"
        assert action == "review"

    def test_grade_c_at_050(self):
        grade, action = self._grade_for_pct(0.501)
        assert grade == "C"

    def test_grade_d_just_below_050(self):
        grade, action = self._grade_for_pct(0.499)
        assert grade == "D"
        assert action == "optimize"

    def test_grade_d_at_030(self):
        grade, action = self._grade_for_pct(0.301)
        assert grade == "D"

    def test_grade_e_just_below_030(self):
        grade, action = self._grade_for_pct(0.299)
        assert grade == "E"
        assert action == "fail_lean"

    def test_no_baseline_grade_is_c_or_d(self):
        """pct=0.5 (neutral fallback) falls in C band."""
        gs = compute_graded_score(_sim())
        assert gs.grade == "C"

    def test_grade_tokens_cover_all_bands(self):
        all_grades = {g for _, g, _ in _GRADE_BANDS}
        assert all_grades == {"A", "B", "C", "D", "E"}

    def test_grade_actions_are_unique(self):
        all_actions = [a for _, _, a in _GRADE_BANDS]
        assert len(all_actions) == len(set(all_actions))


# ---------------------------------------------------------------------------
# TestGradedScoreConfidence
# ---------------------------------------------------------------------------

class TestGradedScoreConfidence:
    """Confidence computation from boolean input flags."""

    def test_all_real_gives_1(self):
        gs = compute_graded_score(
            _sim(),
            confidence_inputs={"a": True, "b": True, "c": True, "d": True},
        )
        assert gs.confidence == pytest.approx(1.0)

    def test_half_real_gives_half(self):
        gs = compute_graded_score(
            _sim(),
            confidence_inputs={"a": True, "b": False, "c": True, "d": False},
        )
        assert gs.confidence == pytest.approx(0.5)

    def test_none_real_gives_0(self):
        gs = compute_graded_score(
            _sim(),
            confidence_inputs={"a": False, "b": False, "c": False, "d": False},
        )
        assert gs.confidence == pytest.approx(0.0)

    def test_empty_dict_gives_neutral_05(self):
        gs = compute_graded_score(_sim(), confidence_inputs={})
        assert gs.confidence == pytest.approx(0.5)

    def test_none_confidence_inputs_gives_neutral_05(self):
        gs = compute_graded_score(_sim(), confidence_inputs=None)
        assert gs.confidence == pytest.approx(0.5)

    def test_fabricated_keys_appear_in_evidence(self):
        gs = compute_graded_score(
            _sim(),
            confidence_inputs={"prod_corr_real": False, "self_corr_real": True},
        )
        assert any("prod_corr_real" in e for e in gs.evidence)

    def test_all_real_no_fabricated_in_evidence(self):
        gs = compute_graded_score(
            _sim(),
            confidence_inputs={"a": True, "b": True},
        )
        assert any("all_inputs_real" in e for e in gs.evidence)


# ---------------------------------------------------------------------------
# TestGradedScoreRawScore
# ---------------------------------------------------------------------------

class TestGradedScoreRawScore:
    """raw_score must match calculate_alpha_score exactly (single source of truth)."""

    def test_raw_score_matches_calculate_alpha_score_defaults(self):
        sim = _sim()
        expected = calculate_alpha_score(sim_result=sim, prod_corr=0.0, self_corr=0.0)
        gs = compute_graded_score(sim)
        assert gs.raw_score == pytest.approx(expected, rel=1e-5)

    def test_raw_score_with_custom_weights(self):
        sim = _sim(sharpe=2.0, fitness=2.0)
        w = {
            "test_sharpe": 0.8, "train_sharpe": 0.1, "fitness": 0.1,
            "prod_corr_penalty": 0.0, "turnover_penalty": 0.0, "investability_penalty": 0.0,
        }
        expected = calculate_alpha_score(sim_result=sim, prod_corr=0.0, self_corr=0.0, weights=w)
        gs = compute_graded_score(sim, weights=w)
        assert gs.raw_score == pytest.approx(expected, rel=1e-5)

    def test_increasing_sharpe_weight_increases_raw_score(self):
        sim = _sim(sharpe=2.5)
        w_low  = {"test_sharpe": 0.1, "train_sharpe": 0.1, "fitness": 0.1,
                  "prod_corr_penalty": 0.0, "turnover_penalty": 0.0, "investability_penalty": 0.0}
        w_high = {"test_sharpe": 0.9, "train_sharpe": 0.1, "fitness": 0.1,
                  "prod_corr_penalty": 0.0, "turnover_penalty": 0.0, "investability_penalty": 0.0}
        gs_low  = compute_graded_score(sim, weights=w_low)
        gs_high = compute_graded_score(sim, weights=w_high)
        assert gs_high.raw_score > gs_low.raw_score

    def test_prod_corr_penalty_reduces_raw_score(self):
        sim = _sim()
        gs_clean  = compute_graded_score(sim, prod_corr=0.0)
        gs_corred = compute_graded_score(sim, prod_corr=0.95)
        assert gs_corred.raw_score < gs_clean.raw_score


# ---------------------------------------------------------------------------
# TestGradedScoreReturnContract
# ---------------------------------------------------------------------------

class TestGradedScoreReturnContract:
    """Type and value contracts for GradedScore."""

    def test_returns_graded_score_instance(self):
        gs = compute_graded_score(_sim())
        assert isinstance(gs, GradedScore)

    def test_grade_in_valid_set(self):
        for sharpe in [-1.0, 0.5, 1.0, 1.5, 3.0]:
            gs = compute_graded_score(_sim(sharpe=sharpe))
            assert gs.grade in {"A", "B", "C", "D", "E"}, f"Unexpected grade {gs.grade!r}"

    def test_percentile_in_unit_interval(self):
        stats = _usable_stats()
        for sharpe in [-10.0, 0.0, 1.0, 5.0, 50.0]:
            gs = compute_graded_score(_sim(sharpe=sharpe), baseline_stats=stats)
            assert 0.0 <= gs.percentile <= 1.0

    def test_confidence_in_unit_interval(self):
        for flags in [
            {"a": True, "b": False},
            {},
            {"x": True},
        ]:
            gs = compute_graded_score(_sim(), confidence_inputs=flags)
            assert 0.0 <= gs.confidence <= 1.0

    def test_evidence_is_list_of_strings(self):
        gs = compute_graded_score(_sim())
        assert isinstance(gs.evidence, list)
        assert all(isinstance(e, str) for e in gs.evidence)

    def test_no_exception_on_missing_sharpe(self):
        """sim_result with no sharpe key must not raise."""
        sim = {"train": {"fitness": 1.0, "turnover": 0.3}, "test": {}}
        gs = compute_graded_score(sim)
        assert isinstance(gs, GradedScore)

    def test_no_exception_on_none_baseline_std(self):
        """Unusable baseline (std=0) must degrade gracefully."""
        stats = BaselineStats(1.0, 0.0, 50, "k", "fine")  # usable=False (std <= _MIN_STD)
        gs = compute_graded_score(_sim(), baseline_stats=stats)
        assert gs.percentile == pytest.approx(0.5, abs=0.01)
