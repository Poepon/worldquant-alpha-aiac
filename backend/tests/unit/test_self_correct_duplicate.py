"""Self-correct prompt must demand STRUCTURAL novelty for duplicate errors
(2026-05-23).

The structural dedup compares the SET of operators and fields (ignores window
sizes, constants, and a leading multiply(-1, ...)), so the generic "minimal
change" self-correct guidance kept producing 100% duplicates (changed only a
window / negation). The prompt now injects an explicit field/operator-change
directive when the error is a duplicate.
"""
from backend.agents.prompts.validation import build_self_correct_prompt


def test_duplicate_directive_present_and_actionable():
    p = build_self_correct_prompt(
        expression="multiply(-1, group_neutralize(ts_zscore(close, 30)))",
        available_fields=["close", "returns", "vwap"],
        error_message=(
            "Duplicate: Structurally similar (100.0%) to: "
            "multiply(-1, group_neutralize(ts_zscore(close, 60)))..."
        ),
        error_type="other",
    )
    assert "DUPLICATE" in p
    assert "structural novelty" in p.lower()
    assert "60%" in p                       # fields carry the higher weight
    assert "multiply(-1" in p               # explicitly calls out the no-op fix


def test_no_duplicate_directive_for_field_error():
    p = build_self_correct_prompt(
        expression="ts_zscore(missing_field, 20)",
        available_fields=["close"],
        error_message="Field 'missing_field' not found in dataset",
        error_type="field_error",
    )
    assert "structural novelty REQUIRED" not in p
