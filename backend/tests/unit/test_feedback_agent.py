"""Unit tests for feedback_agent module-level helpers.

Covers _classify_pitfall_error_type — the source-side noise filter added
2026-05-19 to stop evolution_loop from sedimenting dedup/generic/infra
error_types into knowledge_entries (entry_type='FAILURE_PITFALL').

The historical KB audit on the same day surfaced ~824 NULL-category rows
written from this exact path; the helper closes the source while the
existing data was soft-deleted in parallel.
"""
from __future__ import annotations

import pytest

from backend.agents.feedback_agent import _classify_pitfall_error_type


class TestClassifyPitfallErrorType:
    """Mirrors backend.negative_knowledge generic-bucket logic; this side
    handles the LLM's free-form error_type strings."""

    @pytest.mark.parametrize("noise_et", [
        # dedup-race noise
        "DB duplicate",
        "DB duplicate: already simulated",
        "duplicate_simulation",
        "Database duplicate",
        # generic / fallback
        "Unknown failure",
        "unknown_failure",
        "Simulation Error",
        "simulation_error",
        # infra / auth / timeout
        "pre-simulate filter skip",
        "pre-simulate filter skip (low P(PASS))",
        "Database constraint violation",
        "Database constraint",
        "API auth error",
        "Infrastructure/Auth",
        "Infrastructure",
        "No Alpha ID returned",
        "System failure",
        "Cascading failure",
    ])
    def test_noise_buckets_return_none(self, noise_et):
        assert _classify_pitfall_error_type(noise_et) is None

    @pytest.mark.parametrize("et", [
        "Metrics below threshold",
        "metrics_below_threshold",
        "Performance below threshold",
        "performance_threshold",
        "Performance degradation",
        "Performance failure",
        "Low Sharpe ratio",
        "Low signal-to-noise ratio",
        "Signal degradation",
        "Low novelty",
        "LOW_FITNESS",
        "LOW_SHARPE",
        "HIGH_TURNOVER",
        "QUALITY_CHECK_FAILED",
        "LOW_SUB_UNIVERSE_SHARPE",
    ])
    def test_threshold_signals(self, et):
        assert _classify_pitfall_error_type(et) == "threshold"

    @pytest.mark.parametrize("et", [
        "CONCENTRATED_WEIGHT",
        "concentrated weights",
        "High correlation",
    ])
    def test_robustness_signals(self, et):
        assert _classify_pitfall_error_type(et) == "robustness"

    @pytest.mark.parametrize("et", [
        "Syntax error",
        "syntax",
        "Type mismatch",
        "Type error",
        "Semantic error",
    ])
    def test_static_finding_signals(self, et):
        assert _classify_pitfall_error_type(et) == "static_finding"

    @pytest.mark.parametrize("et", [None, "", "   "])
    def test_empty_returns_none(self, et):
        assert _classify_pitfall_error_type(et) is None

    def test_unclassifiable_returns_none(self):
        """error_type the LLM made up that doesn't match any known signal
        keyword falls through — caller MUST treat None as 'do not write',
        so unclassifiable strings are silently dropped (NOT written with
        NULL category as the legacy code did)."""
        assert _classify_pitfall_error_type("Quantum tunneling") is None
        assert _classify_pitfall_error_type("xyz123") is None

    def test_noise_takes_precedence_over_signal_substring(self):
        """If both noise and signal keywords match, noise wins (skip).
        Example: an LLM string mixing both — better to drop than mis-stamp."""
        # "Metrics below threshold / Unknown failure" has both 'threshold'
        # (signal) and 'unknown' (noise) — noise check runs first.
        assert _classify_pitfall_error_type(
            "Metrics below threshold / Unknown failure"
        ) is None

    def test_database_word_alone_does_not_mark_as_noise(self):
        """Regression guard: an early version had bare 'database' in the
        noise list, which incorrectly killed legitimate threshold findings
        whose message mentioned the word. Noise list now only matches
        compound infra phrases ('database constraint' / 'database error')
        while real signal keywords like 'low sharpe' still classify even
        when the LLM phrase mentions database context."""
        assert _classify_pitfall_error_type("low Sharpe on database X") == "threshold"
        # Compound infra phrases still get dropped:
        assert _classify_pitfall_error_type("Database constraint") is None
        assert _classify_pitfall_error_type("Database error") is None
        # And dedup race is caught by 'duplicate', not 'database':
        assert _classify_pitfall_error_type("Database duplicate") is None
