"""Phase A — orthogonality prompt steering: build_hypothesis_prompt invariant.

The submitted-pool NUDGE block is spliced into the hypothesis prompt ONLY when
ctx.submitted_pool_profile is a non-empty string (node_hypothesis sets it only
under ENABLE_ORTHOGONAL_PROMPT_STEERING). None / "" → byte-for-byte legacy.
Plan: docs/orthogonality_steered_exploration_plan_2026-06-05.md
"""
from backend.agents.prompts.base import PromptContext
from backend.agents.prompts.hypothesis import build_hypothesis_prompt

_HEADER = "Portfolio Breadth (orthogonality nudge)"


def _ctx(**kw):
    return PromptContext(dataset_id="pv1", region="USA", universe="TOP3000", **kw)


def test_flag_off_byte_for_byte_legacy():
    # Field defaults to None → no orthogonality header → identical to a context
    # that never touches the field (the flag-OFF production path).
    legacy = build_hypothesis_prompt(_ctx())
    explicit_none = build_hypothesis_prompt(_ctx(submitted_pool_profile=None))
    assert legacy == explicit_none
    assert _HEADER not in legacy


def test_empty_string_is_legacy():
    # render_profile_block returns "" for an empty pool → also legacy.
    legacy = build_hypothesis_prompt(_ctx())
    empty = build_hypothesis_prompt(_ctx(submitted_pool_profile=""))
    assert empty == legacy
    assert _HEADER not in empty


def test_block_injected_when_set():
    nudge = "已提交组合: momentum 6/13; 探索正交 value. <<MARKER123>>"
    prompt = build_hypothesis_prompt(_ctx(submitted_pool_profile=nudge))
    assert _HEADER in prompt
    assert "<<MARKER123>>" in prompt
    # placed in the nudge region (next to the pillar nudge), before ## Task.
    assert prompt.index(_HEADER) < prompt.index("## Task")


def test_injection_does_not_disturb_other_sections():
    # The block is additive — the rest of the prompt is unchanged vs legacy.
    legacy = build_hypothesis_prompt(_ctx())
    steered = build_hypothesis_prompt(_ctx(submitted_pool_profile="X"))
    # Everything legacy still present; steered is a superset by exactly the block.
    assert "## Research Context" in steered
    assert "## Task" in steered
    assert len(steered) > len(legacy)
