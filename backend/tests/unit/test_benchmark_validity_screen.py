"""Offline tests for the per-node benchmark's validity SCREEN (no DB / no network).

Pins:
  - build_arity_map parses operator `definition` strings into [min,max] windows
    (param_count is 0 for all live ops, so definition-parsing is the only signal).
  - the screen rejects every BAD_EXPR (hallucinated op / arity / group-as-value)
    and accepts well-formed expressions — the plan's pinned invariant.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import benchmark_llm_per_node as B  # noqa: E402
from backend.alpha_semantic_validator import AlphaSemanticValidator  # noqa: E402


# Controlled operator registry (name + definition) — mirrors the live-DB shape.
_OPERATORS = [
    {"name": "ts_rank", "definition": "ts_rank(x, d)"},
    {"name": "ts_delta", "definition": "ts_delta(x, d)"},
    {"name": "ts_mean", "definition": "ts_mean(x, d)"},
    {"name": "ts_zscore", "definition": "ts_zscore(x, d)"},
    {"name": "ts_decay_linear", "definition": "ts_decay_linear(x, d)"},
    {"name": "rank", "definition": "rank(x, rate=2)"},
    {"name": "divide", "definition": "divide(x, y)"},
    {"name": "group_neutralize", "definition": "group_neutralize(x, group)"},
    {"name": "group_rank", "definition": "group_rank(x, group)"},
    {"name": "group_zscore", "definition": "group_zscore(x, group)"},
    {"name": "ts_regression", "definition": "ts_regression(y, x, d, lag = 0, rettype = 0)"},
]
_OP_NAMES = [o["name"] for o in _OPERATORS]


@pytest.fixture
def screen():
    arity = B.build_arity_map(_OPERATORS)
    validator = AlphaSemanticValidator(
        fields=B.FIELDS_FIXTURE, operators=_OP_NAMES,
        strict_field_check=False, strict_type_check=False, reject_unknown_operators=True)
    return B.ScreenCtx(validator, arity)


def test_build_arity_map_parses_definitions():
    a = B.build_arity_map(_OPERATORS)
    assert a["ts_regression"] == (3, 5)   # 3 required + 2 defaulted
    assert a["ts_mean"] == (2, 2)
    assert a["rank"] == (1, 2)            # x required, rate defaulted
    assert a["divide"] == (2, 2)


def test_arity_violation_detection():
    a = B.build_arity_map(_OPERATORS)
    assert B._arity_violations("ts_mean(close)", a) is True          # 1 < 2
    assert B._arity_violations("ts_regression(close, volume)", a) is True  # 2 < 3
    assert B._arity_violations("ts_mean(close, 20)", a) is False
    assert B._arity_violations("ts_regression(close, volume, 20)", a) is False  # 3 in [3,5]
    # nested call arg-counting stays at the operator's own level
    assert B._arity_violations("group_neutralize(ts_rank(ts_delta(close, 5), 20), sector)", a) is False


def test_screen_rejects_all_bad_exprs(screen):
    for bad, etype, _ in B.BAD_EXPRS:
        assert not B.expr_usable(bad, screen.validator, screen.arity), f"{bad!r} ({etype}) should be rejected"


def test_screen_accepts_good_exprs(screen):
    for good in B.GOOD_EXPRS:
        assert B.expr_usable(good, screen.validator, screen.arity), f"{good!r} should be accepted"


def test_split_top_level():
    assert B._split_top_level("a, b, c") == ["a", " b", " c"]
    assert B._split_top_level("a, f(b, c), d") == ["a", " f(b, c)", " d"]
    assert B._split_top_level("") == []
