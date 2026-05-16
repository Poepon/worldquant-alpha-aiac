"""Unit tests for P1-C part 2 hypothesis trigger pure helpers.

来源: docs/alphagbm_skills_research_2026-05-15.md skill `investment-thesis`.

Tests the pure-function evaluators in
``backend.services.hypothesis_health_service`` — no DB, no Celery, no FS.
Mirrors the alpha-health-check unit-test layout.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from pydantic import ValidationError

from backend.config import settings
from backend.services.hypothesis_health_service import (
    HypothesisAggregates,
    LLMThesisScore,
    TriggerConfig,
    evaluate_attribution_hypothesis_dominant,
    evaluate_dropped_sharpe,
    evaluate_no_pass_in_n_rounds,
    evaluate_pass_rate_drop,
    evaluate_stale_alphas,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@dataclass
class MockRoundStats:
    """Minimal HypothesisRoundStats stand-in for trigger evaluators.

    The evaluators only read these attrs via getattr — a SimpleNamespace
    would also work but a dataclass makes the test data crisper.
    """
    alpha_count: int = 0
    pass_count: int = 0
    flip_alpha_count: int = 0
    flip_pass_count: int = 0
    attribution: Optional[str] = None
    best_sharpe: Optional[float] = None
    round_index: int = 0


def _baseline(sharpe_avg=2.0, n=5, fitness=1.0, turnover=0.3):
    return {
        "stamped_at": "2026-05-01T00:00:00+00:00",
        "n_alphas": n,
        "alpha_pks_seed": [1, 2, 3],
        "sharpe_avg": sharpe_avg,
        "fitness_avg": fitness,
        "turnover_avg": turnover,
    }


def _aggs(**kw) -> HypothesisAggregates:
    defaults = dict(
        hypothesis_id=1,
        related_alpha_count=0,
        current_sharpe_avg=None,
        current_pass_rate=None,
        stale_share=None,
        recent_rounds=[],
        baseline_metrics=None,
    )
    defaults.update(kw)
    return HypothesisAggregates(**defaults)


@pytest.fixture
def cfg() -> TriggerConfig:
    """Fresh config built from current settings module — matches what
    the production service does."""
    return TriggerConfig.from_settings(settings)


# ===========================================================================
# T1 — evaluate_dropped_sharpe
# ===========================================================================


class TestEvalDroppedSharpe:
    def test_no_baseline_returns_none(self, cfg):
        assert (
            evaluate_dropped_sharpe(_aggs(current_sharpe_avg=0.5), cfg)
            is None
        )

    def test_small_sample_baseline_skipped(self, cfg):
        # n_alphas<3 → skip (MFX-1 small-sample guard)
        a = _aggs(
            current_sharpe_avg=0.5,
            baseline_metrics=_baseline(sharpe_avg=2.0, n=2),
        )
        assert evaluate_dropped_sharpe(a, cfg) is None

    def test_current_none_returns_none(self, cfg):
        a = _aggs(
            current_sharpe_avg=None,
            baseline_metrics=_baseline(sharpe_avg=2.0, n=5),
        )
        assert evaluate_dropped_sharpe(a, cfg) is None

    def test_zero_baseline_avoids_divzero(self, cfg):
        a = _aggs(
            current_sharpe_avg=-1.0,
            baseline_metrics=_baseline(sharpe_avg=0.0, n=5),
        )
        assert evaluate_dropped_sharpe(a, cfg) is None

    def test_orange_threshold_hit(self, cfg):
        # baseline=2.0 current=1.34 → delta=-33% (worse than -30 orange)
        a = _aggs(
            current_sharpe_avg=1.34,
            baseline_metrics=_baseline(sharpe_avg=2.0, n=5),
        )
        hit = evaluate_dropped_sharpe(a, cfg)
        assert hit is not None
        assert hit.severity == "orange"
        assert hit.type == "dropped_sharpe_pct"
        assert "sharpe_down_" in hit.reason

    def test_red_threshold_hit(self, cfg):
        # baseline=2.0 current=0.66 → delta=-67% (worse than -50 red)
        a = _aggs(
            current_sharpe_avg=0.66,
            baseline_metrics=_baseline(sharpe_avg=2.0, n=5),
        )
        hit = evaluate_dropped_sharpe(a, cfg)
        assert hit is not None
        assert hit.severity == "red"

    def test_nan_current_returns_none(self, cfg):
        a = _aggs(
            current_sharpe_avg=float("nan"),
            baseline_metrics=_baseline(sharpe_avg=2.0, n=5),
        )
        assert evaluate_dropped_sharpe(a, cfg) is None

    def test_positive_delta_returns_none(self, cfg):
        # current = 3.0 vs baseline 2.0 → +50% improvement
        a = _aggs(
            current_sharpe_avg=3.0,
            baseline_metrics=_baseline(sharpe_avg=2.0, n=5),
        )
        assert evaluate_dropped_sharpe(a, cfg) is None


# ===========================================================================
# T2 — evaluate_no_pass_in_n_rounds
# ===========================================================================


class TestEvalNoPassInNRounds:
    def test_too_few_rounds_returns_none(self, cfg):
        rounds = [
            MockRoundStats(alpha_count=2, pass_count=0)
            for _ in range(cfg.nopass_n_rounds - 1)
        ]
        assert (
            evaluate_no_pass_in_n_rounds(_aggs(recent_rounds=rounds), cfg)
            is None
        )

    def test_any_pass_returns_none(self, cfg):
        rounds = [
            MockRoundStats(alpha_count=2, pass_count=0)
            for _ in range(cfg.nopass_n_rounds - 1)
        ]
        rounds.append(MockRoundStats(alpha_count=2, pass_count=1))
        assert (
            evaluate_no_pass_in_n_rounds(_aggs(recent_rounds=rounds), cfg)
            is None
        )

    def test_empty_round_guard(self, cfg):
        # Round with alpha_count=0 AND flip_alpha_count=0 → not tested,
        # don't fire even if pass_count is 0.
        rounds = [
            MockRoundStats(alpha_count=0, pass_count=0, flip_alpha_count=0)
            for _ in range(cfg.nopass_n_rounds)
        ]
        assert (
            evaluate_no_pass_in_n_rounds(_aggs(recent_rounds=rounds), cfg)
            is None
        )

    def test_flip_only_round_counts_as_tested(self, cfg):
        # MFX-2: flip-alphas count as testing the hypothesis.
        rounds = [
            MockRoundStats(alpha_count=0, pass_count=0, flip_alpha_count=2)
            for _ in range(cfg.nopass_n_rounds)
        ]
        hit = evaluate_no_pass_in_n_rounds(_aggs(recent_rounds=rounds), cfg)
        assert hit is not None
        assert hit.severity == "orange"

    def test_all_tested_zero_pass_fires(self, cfg):
        rounds = [
            MockRoundStats(alpha_count=3, pass_count=0)
            for _ in range(cfg.nopass_n_rounds)
        ]
        hit = evaluate_no_pass_in_n_rounds(_aggs(recent_rounds=rounds), cfg)
        assert hit is not None
        assert hit.window_rounds == cfg.nopass_n_rounds


# ===========================================================================
# T3 — evaluate_pass_rate_drop
# ===========================================================================


class TestEvalPassRateDrop:
    def test_too_few_rounds(self, cfg):
        rounds = [
            MockRoundStats(alpha_count=4, pass_count=2)
            for _ in range(cfg.passrate_window)
        ]
        # need 2W
        assert (
            evaluate_pass_rate_drop(_aggs(recent_rounds=rounds), cfg) is None
        )

    def test_early_zero_returns_none(self, cfg):
        W = cfg.passrate_window
        rounds = (
            [MockRoundStats(alpha_count=0, pass_count=0, flip_alpha_count=0)] * W
            + [MockRoundStats(alpha_count=4, pass_count=0)] * W
        )
        assert (
            evaluate_pass_rate_drop(_aggs(recent_rounds=rounds), cfg) is None
        )

    def test_stable_rate_returns_none(self, cfg):
        W = cfg.passrate_window
        rounds = [MockRoundStats(alpha_count=4, pass_count=2)] * (2 * W)
        assert (
            evaluate_pass_rate_drop(_aggs(recent_rounds=rounds), cfg) is None
        )

    def test_steep_drop_fires(self, cfg):
        W = cfg.passrate_window
        # Early rate = 0.5, recent rate = 0.0 → -100% drop (well past -50)
        rounds = (
            [MockRoundStats(alpha_count=4, pass_count=2)] * W
            + [MockRoundStats(alpha_count=4, pass_count=0)] * W
        )
        hit = evaluate_pass_rate_drop(_aggs(recent_rounds=rounds), cfg)
        assert hit is not None
        assert hit.severity == "orange"
        assert "pass_rate_dropped" in hit.reason


# ===========================================================================
# T4 — evaluate_attribution_hypothesis_dominant
# ===========================================================================


class TestEvalAttributionHypothesisDominant:
    def test_too_few_rounds(self, cfg):
        rounds = [
            MockRoundStats(attribution="hypothesis")
            for _ in range(cfg.attr_window - 1)
        ]
        assert (
            evaluate_attribution_hypothesis_dominant(
                _aggs(recent_rounds=rounds), cfg
            )
            is None
        )

    def test_all_none_attribution_returns_none(self, cfg):
        rounds = [MockRoundStats(attribution=None) for _ in range(cfg.attr_window)]
        assert (
            evaluate_attribution_hypothesis_dominant(
                _aggs(recent_rounds=rounds), cfg
            )
            is None
        )

    def test_half_share_returns_none(self, cfg):
        # share=0.5 < default 0.6 threshold
        W = cfg.attr_window
        rounds = (
            [MockRoundStats(attribution="hypothesis")] * (W // 2)
            + [MockRoundStats(attribution="implementation")] * (W - W // 2)
        )
        assert (
            evaluate_attribution_hypothesis_dominant(
                _aggs(recent_rounds=rounds), cfg
            )
            is None
        )

    def test_majority_share_fires(self, cfg):
        W = cfg.attr_window
        # share = 4/5 = 0.8 > 0.6
        rounds = (
            [MockRoundStats(attribution="hypothesis")] * (W - 1)
            + [MockRoundStats(attribution="implementation")]
        )
        hit = evaluate_attribution_hypothesis_dominant(
            _aggs(recent_rounds=rounds), cfg
        )
        assert hit is not None
        assert hit.severity == "orange"

    def test_full_share_fires(self, cfg):
        W = cfg.attr_window
        rounds = [MockRoundStats(attribution="hypothesis")] * W
        hit = evaluate_attribution_hypothesis_dominant(
            _aggs(recent_rounds=rounds), cfg
        )
        assert hit is not None
        assert hit.observed == 1.0


# ===========================================================================
# T5 — evaluate_stale_alphas
# ===========================================================================


class TestEvalStaleAlphas:
    def test_zero_alphas(self, cfg):
        assert (
            evaluate_stale_alphas(
                _aggs(related_alpha_count=0, stale_share=0.9), cfg,
            )
            is None
        )

    def test_share_below_threshold(self, cfg):
        assert (
            evaluate_stale_alphas(
                _aggs(related_alpha_count=10, stale_share=0.4), cfg,
            )
            is None
        )

    def test_share_none(self, cfg):
        assert (
            evaluate_stale_alphas(
                _aggs(related_alpha_count=10, stale_share=None), cfg,
            )
            is None
        )

    def test_share_at_threshold_fires(self, cfg):
        hit = evaluate_stale_alphas(
            _aggs(related_alpha_count=10, stale_share=0.6), cfg,
        )
        assert hit is not None
        assert hit.severity == "yellow"
        assert "stale_share_60pct" in hit.reason


# ===========================================================================
# LLMThesisScore validator (MFX-6)
# ===========================================================================


class TestLLMThesisScoreValidator:
    def _ok_kwargs(self, **overrides):
        base = dict(
            thesis_score=72,
            ai_feedback="Some feedback.",
            recommended_action="continue",
            reasons=["r1"],
        )
        base.update(overrides)
        return base

    def test_continue_lower(self):
        score = LLMThesisScore(**self._ok_kwargs(recommended_action="Continue"))
        assert score.recommended_action == "continue"

    def test_action_with_trailing_period(self):
        score = LLMThesisScore(**self._ok_kwargs(recommended_action="continue."))
        assert score.recommended_action == "continue"

    def test_action_with_padding(self):
        score = LLMThesisScore(**self._ok_kwargs(recommended_action=" ABANDON "))
        assert score.recommended_action == "abandon"

    def test_action_unknown_raises(self):
        with pytest.raises(ValidationError):
            LLMThesisScore(**self._ok_kwargs(recommended_action="quit"))

    def test_action_non_string_raises(self):
        with pytest.raises(ValidationError):
            LLMThesisScore(**self._ok_kwargs(recommended_action=42))

    def test_feedback_capped_at_600(self):
        long = "x" * 700
        score = LLMThesisScore(**self._ok_kwargs(ai_feedback=long))
        assert len(score.ai_feedback) == 600

    def test_reasons_capped_at_5(self):
        score = LLMThesisScore(
            **self._ok_kwargs(reasons=[f"r{i}" for i in range(10)])
        )
        assert len(score.reasons) == 5

    def test_reasons_empty_defaults(self):
        score = LLMThesisScore(**self._ok_kwargs(reasons=[]))
        assert score.reasons == ["(no reasons)"]

    def test_thesis_score_out_of_range(self):
        with pytest.raises(ValidationError):
            LLMThesisScore(**self._ok_kwargs(thesis_score=150))
        with pytest.raises(ValidationError):
            LLMThesisScore(**self._ok_kwargs(thesis_score=-5))
