"""P2-C regime classifier unit tests (2026-05-16).

Pure-function module — runs under any env (no DB / Redis / LLM).

Covers C1..C10 from the P2-C plan:
    C1  pass-rate bucket boundaries
    C2  boundary inclusion (strict < at 0.05)
    C3  EWMA cold start (history <2 → "normal")
    C4  EWMA single outlier (mostly normal + 1 crisis → not crisis)
    C5  EWMA persistent shift (7×crisis → crisis)
    C6  REGIME_PRESETS completeness + 5 keys + 8 fields + multiplier sanity
    C7  apply_multipliers regime="normal" → identity
    C8  apply_multipliers unknown regime → identity (no exception)
    C9  apply_multipliers provisional propagation
    C10 apply_multipliers score_optimize unchanged (MF6 verification)
"""
from __future__ import annotations

import pytest

from backend.regime_classifier import (
    REGIME_ORDER,
    REGIME_PRESETS,
    RegimePreset,
    apply_ewma_smoothing,
    apply_regime_multipliers,
    classify_pass_rate_to_regime,
)


class TestClassifyPassRate:

    def test_classify_pass_rate_buckets(self):
        """C1: boundary sweep across all 5 buckets."""
        # crisis: <0.05
        assert classify_pass_rate_to_regime(0.0) == "crisis"
        assert classify_pass_rate_to_regime(0.04) == "crisis"
        assert classify_pass_rate_to_regime(0.049) == "crisis"
        # elevated: [0.05, 0.10)
        assert classify_pass_rate_to_regime(0.05) == "elevated"
        assert classify_pass_rate_to_regime(0.099) == "elevated"
        # normal: [0.10, 0.20)
        assert classify_pass_rate_to_regime(0.10) == "normal"
        assert classify_pass_rate_to_regime(0.199) == "normal"
        # calm: [0.20, 0.30)
        assert classify_pass_rate_to_regime(0.20) == "calm"
        assert classify_pass_rate_to_regime(0.299) == "calm"
        # very_calm: >= 0.30
        assert classify_pass_rate_to_regime(0.30) == "very_calm"
        assert classify_pass_rate_to_regime(0.50) == "very_calm"
        assert classify_pass_rate_to_regime(1.0) == "very_calm"

    def test_classify_pass_rate_boundary_inclusion(self):
        """C2: 0.05 lands in `elevated` (strict <) — proves the cutoff side."""
        assert classify_pass_rate_to_regime(0.05) == "elevated"
        # And the predecessor side
        assert classify_pass_rate_to_regime(0.0499999) == "crisis"


class TestEWMA:

    def test_ewma_cold_start(self):
        """C3: <2 entries → 'normal'."""
        assert apply_ewma_smoothing([]) == "normal"
        assert apply_ewma_smoothing(["crisis"]) == "normal"

    def test_ewma_single_outlier(self):
        """C4: 6×normal + 1×crisis at the end should not flip to crisis.

        With α=0.3 the smoothed ordinal stays well above index 0; the
        round-to-nearest path either stays at 'normal' (idx 2) or drops
        one notch to 'elevated' (idx 1) — never 'crisis' (idx 0).
        """
        history = ["normal"] * 6 + ["crisis"]
        result = apply_ewma_smoothing(history, alpha=0.3)
        assert result in {"normal", "elevated"}, (
            f"expected normal/elevated, got {result}"
        )

    def test_ewma_persistent_shift(self):
        """C5: 7×crisis → crisis."""
        history = ["crisis"] * 7
        assert apply_ewma_smoothing(history, alpha=0.3) == "crisis"

    def test_ewma_unknown_label_treated_as_normal(self):
        """Edge: unknown labels degrade to 'normal' ordinal index."""
        # 7 entries to bypass the cold-start guard.
        out = apply_ewma_smoothing(["unknown"] * 7, alpha=0.3)
        assert out == "normal"


class TestRegimePresets:

    def test_regime_presets_completeness(self):
        """C6: 5 presets, all fields populated, multipliers in [0.5, 1.5]."""
        assert set(REGIME_PRESETS.keys()) == {
            "crisis", "elevated", "normal", "calm", "very_calm",
        }
        assert set(REGIME_ORDER) == set(REGIME_PRESETS.keys())

        for regime, preset in REGIME_PRESETS.items():
            assert isinstance(preset, RegimePreset)
            assert preset.regime == regime
            # multipliers must be sane and positive
            for mult in (
                preset.sharpe_multiplier,
                preset.fitness_multiplier,
                preset.turnover_multiplier,
                preset.score_pass_multiplier,
            ):
                assert 0.5 <= mult <= 1.5, (
                    f"{regime} multiplier {mult} out of [0.5, 1.5] safety range"
                )
            # string fields populated
            assert isinstance(preset.style_label, str) and preset.style_label
            assert isinstance(preset.style_philosophy, str) and preset.style_philosophy
            # pillar_bias non-empty tuple of strings
            assert isinstance(preset.pillar_bias, tuple)
            assert len(preset.pillar_bias) >= 1
            assert all(isinstance(p, str) and p for p in preset.pillar_bias)

        # Sanity: normal regime should be identity (1.0 × everything)
        n = REGIME_PRESETS["normal"]
        assert n.sharpe_multiplier == 1.0
        assert n.fitness_multiplier == 1.0
        assert n.turnover_multiplier == 1.0
        assert n.score_pass_multiplier == 1.0


class TestApplyMultipliers:

    def _base(self) -> dict:
        return {
            "tier": 1,
            "sharpe_min": 1.25,
            "fitness_min": 0.95,
            "turnover_min": 0.01,
            "turnover_max": 0.70,
            "score_pass": 0.80,
            "score_optimize": 0.30,
            "provisional": {
                "sharpe_min": 0.80,
                "fitness_min": 0.60,
                "turnover_max": 0.85,
            },
        }

    def test_apply_multipliers_normal_noop(self):
        """C7: regime='normal' returns numerically identical thresholds."""
        base = self._base()
        out = apply_regime_multipliers(base, "normal")
        for k in ("sharpe_min", "fitness_min", "turnover_max",
                  "score_pass", "score_optimize"):
            assert out[k] == base[k], f"{k} should be unchanged for normal"
        assert out["provisional"]["sharpe_min"] == base["provisional"]["sharpe_min"]

    def test_apply_multipliers_none_passthrough(self):
        """regime=None → shallow-copy identity, no exception."""
        base = self._base()
        out = apply_regime_multipliers(base, None)
        assert out["sharpe_min"] == base["sharpe_min"]
        # output must NOT be the same object (caller may mutate)
        assert out is not base

    def test_apply_multipliers_unknown_passthrough(self):
        """C8: unknown regime → identity, no exception."""
        base = self._base()
        out = apply_regime_multipliers(base, "foobar")
        assert out["sharpe_min"] == base["sharpe_min"]
        assert out["turnover_max"] == base["turnover_max"]
        # no side-effect key (S8)
        assert "_regime_applied" not in out

    def test_apply_multipliers_crisis_scaling(self):
        """crisis scales sharpe/fitness/turnover/score_pass per preset."""
        base = self._base()
        out = apply_regime_multipliers(base, "crisis")
        p = REGIME_PRESETS["crisis"]
        assert out["sharpe_min"] == pytest.approx(1.25 * p.sharpe_multiplier)
        assert out["fitness_min"] == pytest.approx(0.95 * p.fitness_multiplier)
        assert out["turnover_max"] == pytest.approx(0.70 * p.turnover_multiplier)
        assert out["score_pass"] == pytest.approx(0.80 * p.score_pass_multiplier)

    def test_apply_multipliers_provisional_propagation(self):
        """C9: provisional sub-dict receives the same multipliers."""
        base = self._base()
        out = apply_regime_multipliers(base, "crisis")
        p = REGIME_PRESETS["crisis"]
        prov = out["provisional"]
        assert prov["sharpe_min"] == pytest.approx(0.80 * p.sharpe_multiplier)
        assert prov["fitness_min"] == pytest.approx(0.60 * p.fitness_multiplier)
        assert prov["turnover_max"] == pytest.approx(0.85 * p.turnover_multiplier)

    def test_apply_multipliers_score_optimize_unchanged(self):
        """C10 / MF6: score_optimize is NEVER multiplied — it stays exactly
        equal to the base value across every regime."""
        base = self._base()
        for regime in REGIME_ORDER:
            out = apply_regime_multipliers(base, regime)
            assert out["score_optimize"] == base["score_optimize"], (
                f"score_optimize moved under regime={regime}: "
                f"{out['score_optimize']} != {base['score_optimize']}"
            )

    def test_apply_multipliers_no_side_effect_key(self):
        """S8: apply_regime_multipliers must NOT write _regime_applied."""
        base = self._base()
        out = apply_regime_multipliers(base, "elevated")
        assert "_regime_applied" not in out
        # input dict also untouched
        assert "_regime_applied" not in base

    def test_apply_multipliers_missing_keys_safe(self):
        """Inputs missing some keys (legacy tier_cfg) should not crash."""
        minimal = {"sharpe_min": 1.0, "turnover_max": 0.5}
        out = apply_regime_multipliers(minimal, "calm")
        p = REGIME_PRESETS["calm"]
        assert out["sharpe_min"] == pytest.approx(1.0 * p.sharpe_multiplier)
        assert out["turnover_max"] == pytest.approx(0.5 * p.turnover_multiplier)
        # absent fields stay absent
        assert "fitness_min" not in out
        assert "score_pass" not in out
