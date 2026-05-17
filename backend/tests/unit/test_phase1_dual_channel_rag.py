"""Phase 1 R4' (2026-05-17) dual-channel RAG unit tests.

ENABLE_DUAL_CHANNEL_RAG=False (default) MUST render the legacy single-section
"## Historical Patterns (For Reference Only)" block byte-for-byte identical
to pre-R4' behavior. ON splits into ✓ Channel A (success) + ⛔ Channel B
(failure) visual-separated blocks so LLM treats positive vs negative evidence
as orthogonal signals.

Tests:
1. Helper build_dual_channel_patterns_block — OFF returns legacy form
2. Helper — ON returns dual-channel form with ✓ + ⛔ markers
3. Empty patterns + empty pitfalls handled both modes
4. Integration: build_hypothesis_prompt flag-off byte-for-byte invariant
5. Integration: build_hypothesis_prompt flag-on contains "Dual Channel"
"""
from __future__ import annotations

import pytest

from backend.agents.prompts.base import (
    PromptContext,
    build_dual_channel_patterns_block,
    build_patterns_context,
)
from backend.agents.prompts.hypothesis import build_hypothesis_prompt


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------

class TestDualChannelHelper:
    @pytest.fixture
    def success_patterns(self):
        return [
            {"pattern": "ts_rank(returns, 20)", "description": "20d momentum z-score"},
            {"pattern": "ts_zscore(volume, 30)", "description": "30d volume zscore"},
        ]

    @pytest.fixture
    def failure_pitfalls(self):
        return [
            {"pattern": "rank(close)", "description": "raw cross-sectional rank — too noisy"},
        ]

    def test_flag_off_renders_legacy_section_header(self, success_patterns, failure_pitfalls):
        block = build_dual_channel_patterns_block(
            success_patterns, failure_pitfalls, dual_channel=False
        )
        assert "## Historical Patterns (For Reference Only)" in block
        # Legacy form does NOT mention Dual Channel
        assert "Dual Channel" not in block
        assert "Channel A" not in block
        assert "Channel B" not in block

    def test_flag_on_renders_dual_channel_markers(self, success_patterns, failure_pitfalls):
        block = build_dual_channel_patterns_block(
            success_patterns, failure_pitfalls, dual_channel=True
        )
        assert "## Historical Patterns — Dual Channel" in block
        assert "✓ Channel A" in block
        assert "⛔ Channel B" in block
        assert "positive evidence" in block.lower()
        assert "negative evidence" in block.lower()

    def test_flag_off_contains_success_and_failure_lines(
        self, success_patterns, failure_pitfalls
    ):
        block = build_dual_channel_patterns_block(
            success_patterns, failure_pitfalls, dual_channel=False
        )
        # Both patterns rendered inline
        assert "ts_rank(returns, 20)" in block
        assert "rank(close)" in block
        # Legacy explanatory note
        assert "What failed before may work in different contexts" in block

    def test_flag_on_contains_success_and_failure_lines(
        self, success_patterns, failure_pitfalls
    ):
        block = build_dual_channel_patterns_block(
            success_patterns, failure_pitfalls, dual_channel=True
        )
        assert "ts_rank(returns, 20)" in block
        assert "rank(close)" in block
        # New explanatory framing — orthogonal evidence streams
        assert "orthogonal" in block

    def test_empty_inputs_both_modes_safe(self):
        off_block = build_dual_channel_patterns_block(
            [], [], dual_channel=False
        )
        on_block = build_dual_channel_patterns_block(
            [], [], dual_channel=True
        )
        # No crash + still contains section header
        assert "Historical Patterns" in off_block
        assert "Historical Patterns" in on_block
        # Empty rendering via build_patterns_context placeholder
        assert "No patterns recorded yet" in off_block
        assert "No pitfalls recorded yet" in off_block
        assert "No patterns recorded yet" in on_block
        assert "No pitfalls recorded yet" in on_block

    def test_flag_off_byte_for_byte_legacy_form(self, success_patterns, failure_pitfalls):
        """The OFF form must equal the exact pre-R4' template substring used
        in hypothesis.py:250-258 — byte-for-byte. If this breaks, the legacy
        flag-off invariant is violated and ENABLE_DUAL_CHANNEL_RAG=False
        users see a different prompt.
        """
        block = build_dual_channel_patterns_block(
            success_patterns, failure_pitfalls, dual_channel=False
        )
        expected = (
            "## Historical Patterns (For Reference Only)\n"
            "\n"
            "**Approaches that have worked in similar contexts**:\n"
            f"{build_patterns_context(success_patterns, 'patterns')}\n"
            "\n"
            "**Approaches that have not worked**:\n"
            f"{build_patterns_context(failure_pitfalls, 'pitfalls')}\n"
            "\n"
            "Note: These are observations, not rules. What failed before may work in different contexts."
        )
        assert block == expected


# ---------------------------------------------------------------------------
# Integration: build_hypothesis_prompt + flag toggling
# ---------------------------------------------------------------------------

class TestDualChannelInPrompt:
    @pytest.fixture
    def ctx(self):
        return PromptContext(
            dataset_id="fundamental6",
            dataset_category="fundamentals",
            dataset_description="Quarterly fundamentals",
            region="USA",
            universe="TOP3000",
            fields=[
                {"id": "roe", "name": "ROE", "category": "MATRIX"},
                {"id": "roa", "name": "ROA", "category": "MATRIX"},
            ],
            success_patterns=[
                {"pattern": "ts_rank(roe, 60)", "description": "60d ROE rank"},
            ],
            failure_pitfalls=[
                {"pattern": "rank(roa)", "description": "raw cross-sectional rank"},
            ],
        )

    def test_flag_off_uses_legacy_section_header(self, ctx, monkeypatch):
        from backend.agents.prompts import hypothesis as hyp
        monkeypatch.setattr(hyp.settings, "ENABLE_DUAL_CHANNEL_RAG", False)

        prompt = build_hypothesis_prompt(ctx)
        assert "## Historical Patterns (For Reference Only)" in prompt
        assert "Dual Channel" not in prompt
        assert "✓ Channel A" not in prompt
        assert "⛔ Channel B" not in prompt

    def test_flag_on_renders_dual_channel(self, ctx, monkeypatch):
        from backend.agents.prompts import hypothesis as hyp
        monkeypatch.setattr(hyp.settings, "ENABLE_DUAL_CHANNEL_RAG", True)

        prompt = build_hypothesis_prompt(ctx)
        assert "## Historical Patterns — Dual Channel" in prompt
        assert "✓ Channel A" in prompt
        assert "⛔ Channel B" in prompt
        # Patterns still present in the dual-channel rendering
        assert "ts_rank(roe, 60)" in prompt
        assert "rank(roa)" in prompt

    def test_flag_off_default_state(self, ctx):
        """Without monkeypatching, default settings.ENABLE_DUAL_CHANNEL_RAG=False
        — the prompt must be in legacy form.
        """
        prompt = build_hypothesis_prompt(ctx)
        # Settings default is False (config.py:200 + feature_flag_service.py
        # SUPPORTED_FLAGS register). DB FeatureFlagOverride may flip it in
        # production but not in test env.
        assert "## Historical Patterns (For Reference Only)" in prompt
