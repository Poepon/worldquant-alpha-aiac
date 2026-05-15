"""P2-C `build_style_preset_block` unit tests (2026-05-16).

Pure-function tests for the prompt rendering helper that the
hypothesis prompt template splices in. No DB / Redis / LLM.

Covers P1..P3 from the P2-C plan:
    P1 None / {} → "" (byte-for-byte legacy splice invariant)
    P2 fully populated preset renders all fields
    P3 missing pillar_bias degrades to "no bias" without crash
"""
from __future__ import annotations

# Warm-up: load backend.tasks before backend.agents to break the legacy
# circular import (mining_tasks → backend.agents → graph.nodes.evaluation
# → backend.tasks.session_watchdog → backend.tasks.__init__). Same pattern
# used by test_node_hypothesis_macro.py.
import backend.tasks  # noqa: E402, F401

from backend.agents.prompts.base import build_style_preset_block  # noqa: E402


def test_block_none_returns_empty():
    """P1: None / {} → empty string (MF4 byte-for-byte legacy splice)."""
    assert build_style_preset_block(None) == ""
    assert build_style_preset_block({}) == ""


def test_block_render():
    """P2: a fully populated preset renders Investment Philosophy block
    containing regime / style_label / philosophy / pillars."""
    preset = {
        "regime": "crisis",
        "style_label": "Risk-Off Defensive",
        "style_philosophy": (
            "Capital preservation over alpha hunting. Favour low-beta, "
            "low-turnover, quality and defensive value signals."
        ),
        "pillar_bias": ["quality", "value", "volatility"],
    }
    out = build_style_preset_block(preset)
    assert "## Investment Philosophy — Current Regime: crisis" in out
    assert "Risk-Off Defensive" in out
    assert "Capital preservation" in out
    assert "quality" in out and "value" in out and "volatility" in out
    # The rationale guidance line must be present so the LLM knows what to do.
    assert "rationale" in out


def test_block_missing_pillar_bias():
    """P3: missing pillar_bias degrades to 'no bias' without crash."""
    preset = {
        "regime": "calm",
        "style_label": "Constructive",
        "style_philosophy": "Lean into positive carry.",
        # pillar_bias absent
    }
    out = build_style_preset_block(preset)
    assert "no bias" in out
    assert "Constructive" in out
    # No traceback / empty string leak
    assert out != ""


def test_block_missing_all_fields_safe():
    """Extreme defensive case: empty values for everything except a regime
    label — should still render without crashing."""
    preset = {"regime": "normal"}
    out = build_style_preset_block(preset)
    assert "normal" in out
    assert "no bias" in out


def test_block_tuple_pillar_bias():
    """RegimePreset uses tuple for pillar_bias internally — helper must
    accept either list or tuple."""
    preset = {
        "regime": "very_calm",
        "style_label": "Aggressive Growth",
        "style_philosophy": "Reach for differentiated structure.",
        "pillar_bias": ("sentiment", "momentum", "quality"),
    }
    out = build_style_preset_block(preset)
    assert "sentiment" in out
    assert "momentum" in out
    assert "quality" in out


def test_block_pillar_bias_truncated_to_5():
    """Defensive truncation cap: only the first 5 pillars are rendered."""
    preset = {
        "regime": "elevated",
        "style_label": "X",
        "style_philosophy": "Y",
        "pillar_bias": [f"p{i}" for i in range(10)],
    }
    out = build_style_preset_block(preset)
    assert "p0" in out and "p4" in out
    assert "p5" not in out
