"""Unit tests for Phase 0 Q1/Q3 KB seed expansion (master plan v1.5 §2.6 + §7).

Verifies the module-level eager-merge of ACADEMIC_PATTERNS picks up the
Q1 alpha101_kakushadze.json (and once Q3 ships, alpha158_qlib.json), that
all rows pass the basic-syntax sniff test, that hashes are unique within
each JSON dataset, and that parse_pattern_operators populates a
non-empty list for every real alpha expression (forward-compat audit hook).

These run purely in-process — no DB / network / BRAIN dependencies.
"""
from __future__ import annotations

from typing import List

import pytest

from backend.external_knowledge import (
    ACADEMIC_PATTERNS,
    ExternalKnowledge,
    _BASE_ACADEMIC_PATTERNS,
    _load_external_patterns_json,
    is_likely_alpha_expression,
    parse_pattern_operators,
)
from backend.models.knowledge import compute_pattern_hash


# --------------------------------------------------------------------------- #
# 1. Merge length — Q1 raises ACADEMIC_PATTERNS from 5 → 106 (5 base + 101)
# --------------------------------------------------------------------------- #
def test_base_patterns_count():
    assert len(_BASE_ACADEMIC_PATTERNS) == 5


def test_merged_patterns_at_least_105():
    """After Q1: 5 base + 100 new Kakushadze (Alpha#2 skipped — already in _BASE) = 105.
    After Q3: + Alpha158 ≥ 263.
    """
    assert len(ACADEMIC_PATTERNS) >= 105, (
        f"expected ≥ 105 after Q1 ship; got {len(ACADEMIC_PATTERNS)}"
    )


def test_alpha101_json_loads_100_entries():
    """Alpha #2 is excluded from JSON because _BASE_ACADEMIC_PATTERNS already
    carries the identical text (pattern_hash collision otherwise). All other
    100 Kakushadze alphas (#1, #3-#101) are in the JSON file."""
    alpha101 = _load_external_patterns_json("alpha101_kakushadze.json")
    assert len(alpha101) == 100, f"alpha101 JSON should yield 100 rows, got {len(alpha101)}"


# --------------------------------------------------------------------------- #
# 2. Schema — every row is a non-empty ExternalKnowledge
# --------------------------------------------------------------------------- #
def test_all_rows_are_external_knowledge():
    for i, item in enumerate(ACADEMIC_PATTERNS):
        assert isinstance(item, ExternalKnowledge), \
            f"row {i}: expected ExternalKnowledge, got {type(item).__name__}"
        assert item.pattern, f"row {i}: empty pattern"
        assert item.description, f"row {i}: empty description"
        assert item.category, f"row {i}: empty category"
        assert item.source_title, f"row {i}: empty source_title"


# --------------------------------------------------------------------------- #
# 3. Pattern hash uniqueness within ACADEMIC_PATTERNS
# --------------------------------------------------------------------------- #
def test_no_duplicate_pattern_hashes():
    hashes: List[str] = [
        compute_pattern_hash(p.pattern, None, None) for p in ACADEMIC_PATTERNS
    ]
    dups = [h for h in hashes if hashes.count(h) > 1]
    assert not dups, f"{len(set(dups))} duplicate pattern hashes in ACADEMIC_PATTERNS"


# --------------------------------------------------------------------------- #
# 4. Basic syntax sniff — every pattern should look like alpha-DSL code
#    EXCEPT Q3 raw-feature rows which are pure-arithmetic OHLCV ratios
#    (KMID = (close-open)/open etc.) — those don't have function calls
#    on purpose, see ExternalKnowledge.raw_feature docstring.
# --------------------------------------------------------------------------- #
def test_all_non_raw_patterns_look_like_alpha_expressions():
    failed = []
    for item in ACADEMIC_PATTERNS:
        if item.raw_feature:
            continue  # raw OHLCV ratios skipped — see Q3 plan §3.11
        if not is_likely_alpha_expression(item.pattern):
            failed.append(item.pattern[:80])
    assert not failed, f"{len(failed)} non-raw patterns failed sniff: {failed[:5]}"


# --------------------------------------------------------------------------- #
# 5. parse_pattern_operators — every non-raw alpha pattern has ≥ 1 operator
#    (raw_feature=True rows like KMID = (close-open)/open have 0 operator
#    calls by definition.)
# --------------------------------------------------------------------------- #
def test_parse_pattern_operators_extracts_at_least_one_op():
    """Every NON-raw alpha in ACADEMIC_PATTERNS must use at least one operator."""
    no_ops = [
        item.pattern[:60] for item in ACADEMIC_PATTERNS
        if not item.raw_feature and not parse_pattern_operators(item.pattern)
    ]
    assert not no_ops, (
        f"{len(no_ops)} non-raw patterns have zero operators extracted: {no_ops[:5]}"
    )


def test_parse_pattern_operators_handles_known_shapes():
    assert parse_pattern_operators("rank(close)") == ["rank"]
    assert parse_pattern_operators("ts_corr(rank(x), rank(y), 6)") == [
        "rank", "ts_corr"
    ]
    assert parse_pattern_operators("") == []
    assert parse_pattern_operators("no_calls_just_text") == []
    # Numeric literals + datafields don't appear as ops
    assert parse_pattern_operators("close * volume * 2.5") == []
    # Operator with whitespace before paren
    assert parse_pattern_operators("rank (close)") == ["rank"]


# --------------------------------------------------------------------------- #
# 6. Kakushadze 101 source coverage — every row in alpha101 JSON references
#    the Kakushadze paper in source_title (used by R8 RAG provenance)
# --------------------------------------------------------------------------- #
def test_alpha101_rows_reference_kakushadze():
    alpha101 = _load_external_patterns_json("alpha101_kakushadze.json")
    missing = [
        item.pattern[:60] for item in alpha101
        if "Kakushadze" not in item.source_title
    ]
    assert not missing, f"{len(missing)} alpha101 rows missing Kakushadze attribution"


# --------------------------------------------------------------------------- #
# 7. Optional Q3 sanity (skipped until alpha158_qlib.json ships)
# --------------------------------------------------------------------------- #
def test_alpha158_json_loads_when_present():
    alpha158 = _load_external_patterns_json("alpha158_qlib.json")
    if not alpha158:
        pytest.skip("alpha158_qlib.json not yet committed (Q3 pending)")
    assert len(alpha158) == 158, f"Q3 should yield exactly 158 rows, got {len(alpha158)}"
    # Every Q3 row carries raw_feature bool + qlib_origin (Q3 plan §3.11 +
    # ExternalKnowledge docstring)
    for item in alpha158:
        assert item.raw_feature is not None, f"Q3 row {item.pattern[:40]} missing raw_feature"
        assert item.qlib_origin, f"Q3 row {item.pattern[:40]} missing qlib_origin"
    # CA-1 inspect: should be ~62% raw, ~38% wrapped per §3.11 verification
    raw_count = sum(1 for it in alpha158 if it.raw_feature)
    assert 80 <= raw_count <= 110, (
        f"raw_feature count {raw_count}/158 outside expected band 80-110 — "
        f"verify _BASE Alpha158 features match Qlib defaults"
    )
