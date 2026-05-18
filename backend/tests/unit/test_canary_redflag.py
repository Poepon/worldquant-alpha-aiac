"""Unit tests for backend.tasks.canary_redflag — covers RED_FLAGS shape,
eval_predicate behavior, and summarize. The async check_redflags relies
on a live DB so it's exercised by the script smoke + celery beat task;
here we lock down the pure-function pieces."""
from __future__ import annotations

import pytest

from backend.tasks.canary_redflag import (
    RED_FLAGS,
    eval_predicate,
    summarize,
)


def test_red_flags_shape():
    """5 declared red-flag checks, each a 4-tuple (label, sql, pred, rollback)."""
    assert len(RED_FLAGS) == 5
    for entry in RED_FLAGS:
        assert isinstance(entry, tuple) and len(entry) == 4
        label, sql, pred, rollback = entry
        assert label and isinstance(label, str)
        assert sql.upper().startswith("SELECT"), f"sql for {label!r} must SELECT"
        assert ":t0" in sql, f"sql for {label!r} must scope to :t0"
        assert "value" in pred, f"predicate for {label!r} must reference value"
        assert rollback and isinstance(rollback, str)


def test_red_flag_labels_unique():
    """No duplicate labels (summarize.first_rollback wouldn't be deterministic)."""
    labels = [e[0] for e in RED_FLAGS]
    assert len(labels) == len(set(labels))


@pytest.mark.parametrize("pred,value,expected", [
    ("value > 0.10", 0.05, False),
    ("value > 0.10", 0.15, True),
    ("value > 0.10", 0.10, False),  # strict gt
    ("value >= 1", 0, False),
    ("value >= 1", 1, True),
    ("value > 5.0", 5.0, False),
    ("value > 5.0", 5.001, True),
])
def test_eval_predicate_truth_table(pred, value, expected):
    assert eval_predicate(pred, value) is expected


def test_eval_predicate_returns_false_on_garbage():
    """Author-controlled constants only — but the safety net should catch parse errors."""
    assert eval_predicate("import os; os.system('rm -rf')", 1) is False
    assert eval_predicate("undefined_name > 0", 1) is False
    assert eval_predicate("", 1) is False


def test_eval_predicate_no_builtin_access():
    """Sandbox: __builtins__ stripped so eval can't reach open/exec/etc."""
    assert eval_predicate("__import__('os').system('ls')", 1) is False


def test_summarize_all_green():
    results = [
        {"label": "a", "value": 0.0, "triggered": False, "rollback": "FLAG_A"},
        {"label": "b", "value": 0,   "triggered": False, "rollback": "FLAG_B"},
    ]
    red, first = summarize(results)
    assert red == 0
    assert first is None


def test_summarize_one_red():
    results = [
        {"label": "a", "value": 0.0, "triggered": False, "rollback": "FLAG_A"},
        {"label": "b", "value": 0.5, "triggered": True,  "rollback": "FLAG_B"},
        {"label": "c", "value": 0,   "triggered": False, "rollback": "FLAG_C"},
    ]
    red, first = summarize(results)
    assert red == 1
    assert first == "FLAG_B"


def test_summarize_multiple_red_returns_first_in_declaration_order():
    """When multiple red, summarize returns the FIRST one — that's the
    operator's priority signal per SOP §6 escalation tree."""
    results = [
        {"label": "a", "triggered": True,  "rollback": "FLAG_FIRST"},
        {"label": "b", "triggered": True,  "rollback": "FLAG_SECOND"},
        {"label": "c", "triggered": False, "rollback": "FLAG_C"},
    ]
    red, first = summarize(results)
    assert red == 2
    assert first == "FLAG_FIRST"


def test_summarize_skips_db_error_rows():
    """check_redflags marks DB-error rows with error key + triggered=False.
    Summarize should treat them as non-red (already logged in helper)."""
    results = [
        {"label": "a", "value": None, "triggered": False, "rollback": "FLAG_A",
         "error": "table missing"},
        {"label": "b", "value": 0.05, "triggered": False, "rollback": "FLAG_B"},
    ]
    red, first = summarize(results)
    assert red == 0
    assert first is None
