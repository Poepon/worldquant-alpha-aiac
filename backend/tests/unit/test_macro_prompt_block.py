"""P2-A macro prompt block unit tests (2026-05-16).

4 unit cases covering:
  - P1 build_macro_context_block([]) → ""  (byte-for-byte legacy invariant)
  - P2 field-scope rendering contains mechanism + transmission_channel
  - P3 top-K cap: 100 narratives in → ≤5 entries rendered
  - P4 byte-for-byte invariant on build_hypothesis_prompt:
        macro_narratives=[] produces identical prompt output as legacy
        (PromptContext-level + textual comparison on macro block insertion
         site, M8 field-assertion approach).
"""
from __future__ import annotations

# Warm-up: importing backend.tasks first triggers the full agents/tasks
# graph in the right order, avoiding the known
# agents.mining_agent → graph.nodes.evaluation → tasks.session_watchdog
# → tasks.mining_tasks → agents (back) CIRCULAR.
import backend.tasks  # noqa: F401

from backend.agents.prompts.base import (
    PromptContext,
    build_macro_context_block,
)
from backend.agents.prompts.hypothesis import build_hypothesis_prompt


# ---------------------------------------------------------------------------
# P1: empty list → empty string
# ---------------------------------------------------------------------------
def test_build_macro_context_block_empty_returns_empty_string():
    assert build_macro_context_block([]) == ""
    # None is also tolerated (defensive — caller might pass getattr(...) None)
    # the type-hinted contract is List[Dict] but the empty/falsy short-circuit
    # is what gives byte-for-byte legacy.
    # We don't pass None directly (mypy would reject), but the empty list
    # case is the one the prompt-builder hits.


# ---------------------------------------------------------------------------
# P2: field-scope rendering contains mechanism + transmission
# ---------------------------------------------------------------------------
def test_build_macro_context_block_renders_field_scope():
    narr = [
        {
            "scope": "field",
            "field_id": "close",
            "dataset_category": "pv",
            "region": "*",
            "mechanism": "MECHANISM_TOKEN_X",
            "transmission_channel": "TRANSMISSION_TOKEN_Y",
            "expected_signal_hint": "momentum",
            "confidence": 0.9,
            "source": "seed",
        }
    ]
    out = build_macro_context_block(narr)
    assert "Macro Context" in out
    assert "field `close`" in out
    assert "MECHANISM_TOKEN_X" in out
    assert "TRANSMISSION_TOKEN_Y" in out
    assert "momentum" in out
    # conf rendered to 2 decimals
    assert "0.90" in out


# ---------------------------------------------------------------------------
# P3: top-K cap
# ---------------------------------------------------------------------------
def test_build_macro_context_block_caps_at_five():
    huge = []
    for i in range(100):
        huge.append({
            "scope": "field",
            "field_id": f"field_{i}",
            "mechanism": f"mech_{i}",
            "transmission_channel": f"trans_{i}",
            "expected_signal_hint": "momentum",
            "confidence": 0.5,
            "source": "seed",
        })
    out = build_macro_context_block(huge)
    # 5 entries → 5 list bullets
    assert out.count("- **field `field_") == 5
    # 6th entry must NOT leak in
    assert "field_5`" not in out  # 6th element index 5
    assert "field_99" not in out


# ---------------------------------------------------------------------------
# P4: byte-for-byte invariant — empty macro_narratives produces same
# prompt as legacy (no macro block leaks into the rendered text)
# ---------------------------------------------------------------------------
def test_build_hypothesis_prompt_byte_for_byte_when_macro_empty():
    """M8-style field-level assertion: when macro_narratives=[], the
    rendered prompt MUST NOT contain the Macro Context header. This is
    the P2-A flag-off invariant.
    """
    ctx_empty = PromptContext(
        dataset_id="fundamental6",
        dataset_description="test dataset",
        dataset_category="fundamental",
        region="USA",
        universe="TOP3000",
        fields=[
            {"id": "eps", "type": "MATRIX"},
            {"id": "pe_ratio", "type": "MATRIX"},
        ],
        success_patterns=[],
        failure_pitfalls=[],
        macro_narratives=[],  # empty → no block
    )
    prompt_empty = build_hypothesis_prompt(ctx_empty)
    # Invariant: NO Macro Context header rendered
    assert "Macro Context" not in prompt_empty, (
        "byte-for-byte invariant violated: empty macro_narratives still "
        "rendered a Macro Context header"
    )
    # Sanity: legacy sections still present
    assert "## Research Context" in prompt_empty
    assert "## Available Data Fields" in prompt_empty
    assert "## Historical Patterns" in prompt_empty

    # When populated, the header DOES show up — distinguishes the empty
    # short-circuit from "build helper never fires".
    ctx_with = PromptContext(
        dataset_id="fundamental6",
        dataset_description="test dataset",
        dataset_category="fundamental",
        region="USA",
        universe="TOP3000",
        fields=[{"id": "eps", "type": "MATRIX"}],
        success_patterns=[],
        failure_pitfalls=[],
        macro_narratives=[{
            "scope": "field",
            "field_id": "eps",
            "mechanism": "INJECTED_MECH",
            "transmission_channel": "INJECTED_TRANS",
            "expected_signal_hint": "value",
            "confidence": 0.85,
        }],
    )
    prompt_with = build_hypothesis_prompt(ctx_with)
    assert "Macro Context" in prompt_with
    assert "INJECTED_MECH" in prompt_with
    assert "INJECTED_TRANS" in prompt_with
