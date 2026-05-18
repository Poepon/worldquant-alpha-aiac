"""Phase 3 R1b.3b: R8 RAG L2 elevation for failure_tree pitfalls (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §7.3.

Verifies the consumer-side wire — Jaccard-based bump for FAILURE_PITFALL
entries whose failure_tree.root_statement is semantically close to the
current hypothesis. Per [V1.0-A2-5] this is data-shape coupling, NOT
new API.

Tests directly exercise _r1b_tokens / _r1b_jaccard_distance /
_elevate_failure_tree_pitfalls without DB.
"""
from __future__ import annotations

import pytest

from backend.agents.hierarchical_rag import (
    RAGEntry,
    _elevate_failure_tree_pitfalls,
    _r1b_jaccard_distance,
    _r1b_tokens,
)


def _mk_pitfall(pattern, *, failure_tree_statement=None, score=0.65, meta=None):
    md = dict(meta or {})
    if failure_tree_statement is not None:
        md["failure_tree"] = {"statement": failure_tree_statement, "children": []}
    return RAGEntry(
        pattern_hash="h" + pattern[:4],
        pattern=pattern,
        entry_type="FAILURE_PITFALL",
        description="",
        meta_data=md,
        source_layer="L2_family",
        relevance_score=score,
    )


# ---------------------------------------------------------------------------
# Tokens + Jaccard
# ---------------------------------------------------------------------------

def test_tokens_drops_stopwords_and_ohlcv_fields():
    out = _r1b_tokens("the momentum thesis is rank(close)")
    # 'the', 'is', 'rank', 'close' all dropped (stopwords + OHLCV); 'momentum' + 'thesis' kept
    assert "momentum" in out
    assert "thesis" in out
    assert "the" not in out
    assert "is" not in out
    assert "rank" not in out
    assert "close" not in out


def test_tokens_drops_short_tokens():
    out = _r1b_tokens("a be do or in on")
    # All ≤2 chars filtered (plus stopword filter)
    assert out == set()


def test_tokens_empty_input():
    assert _r1b_tokens("") == set()
    assert _r1b_tokens(None) == set()  # type: ignore


def test_jaccard_distance_identical_zero():
    assert _r1b_jaccard_distance({"a", "b"}, {"a", "b"}) == 0.0


def test_jaccard_distance_disjoint_one():
    assert _r1b_jaccard_distance({"a", "b"}, {"c", "d"}) == 1.0


def test_jaccard_distance_empty_returns_one():
    assert _r1b_jaccard_distance(set(), {"a"}) == 1.0
    assert _r1b_jaccard_distance({"a"}, set()) == 1.0
    assert _r1b_jaccard_distance(set(), set()) == 1.0


def test_jaccard_distance_partial_overlap():
    # {a,b,c} vs {a,b,d} → intersection 2, union 4 → distance 0.5
    assert abs(_r1b_jaccard_distance({"a", "b", "c"}, {"a", "b", "d"}) - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# _elevate_failure_tree_pitfalls
# ---------------------------------------------------------------------------

def test_elevate_no_hypothesis_returns_unchanged():
    pitfalls = [_mk_pitfall("p1", failure_tree_statement="momentum thesis")]
    out = _elevate_failure_tree_pitfalls(pitfalls, "")
    assert out[0].relevance_score == 0.65


def test_elevate_no_failure_tree_skips():
    pitfalls = [_mk_pitfall("p1")]  # no failure_tree in meta
    out = _elevate_failure_tree_pitfalls(pitfalls, "momentum thesis")
    assert out[0].relevance_score == 0.65
    assert "_r1b_failure_tree_match_jaccard" not in out[0].meta_data


def test_elevate_close_match_bumps_score():
    """High token overlap → bump applied."""
    pitfalls = [
        _mk_pitfall("p1", failure_tree_statement="momentum thesis works"),
    ]
    out = _elevate_failure_tree_pitfalls(
        pitfalls, "momentum thesis tested", jaccard_max=0.5, bonus=0.20,
    )
    # original 0.65 + 0.20 = 0.85
    assert out[0].relevance_score == pytest.approx(0.85, abs=1e-6)
    assert "_r1b_failure_tree_match_jaccard" in out[0].meta_data
    assert "_r1b_failure_tree_bonus_applied" in out[0].meta_data


def test_elevate_far_match_no_bump():
    """Low token overlap (distance > jaccard_max) → no bump."""
    pitfalls = [
        _mk_pitfall("p1", failure_tree_statement="completely unrelated quality factor"),
    ]
    out = _elevate_failure_tree_pitfalls(
        pitfalls, "momentum signal thesis", jaccard_max=0.4,
    )
    assert out[0].relevance_score == 0.65  # unchanged


def test_elevate_re_sorts_by_relevance_desc():
    """After bumps, fail list is re-sorted highest-relevance first."""
    pitfalls = [
        _mk_pitfall("low", score=0.65, failure_tree_statement="quality factor exists"),  # no bump
        _mk_pitfall("high", score=0.65, failure_tree_statement="momentum is real"),  # bumped
    ]
    out = _elevate_failure_tree_pitfalls(
        pitfalls, "momentum signal", jaccard_max=0.7, bonus=0.20,
    )
    # 'high' should be first after re-sort
    assert out[0].pattern == "high"
    assert out[0].relevance_score == pytest.approx(0.85, abs=1e-6)


def test_elevate_caps_relevance_at_one():
    """0.95 + 0.20 = 1.15 → capped at 1.0."""
    pitfalls = [
        _mk_pitfall("p1", score=0.95, failure_tree_statement="momentum thesis"),
    ]
    out = _elevate_failure_tree_pitfalls(
        pitfalls, "momentum thesis", jaccard_max=0.5, bonus=0.20,
    )
    assert out[0].relevance_score == 1.0


def test_elevate_empty_root_statement_skips():
    pitfalls = [_mk_pitfall("p1", failure_tree_statement="")]
    out = _elevate_failure_tree_pitfalls(pitfalls, "momentum thesis")
    assert out[0].relevance_score == 0.65  # unchanged


def test_elevate_handles_mixed_entries():
    """Mix of entries with/without failure_tree — only matching ones bumped."""
    pitfalls = [
        _mk_pitfall("no_tree"),
        _mk_pitfall("match", failure_tree_statement="momentum value combined"),
        _mk_pitfall("no_match", failure_tree_statement="completely different topic"),
        _mk_pitfall("empty_tree", failure_tree_statement=""),
    ]
    out = _elevate_failure_tree_pitfalls(
        pitfalls, "momentum value strategy", jaccard_max=0.5, bonus=0.20,
    )
    # Only the 'match' entry gets bumped → moves to front of sorted list
    bumped = [e for e in out if "_r1b_failure_tree_match_jaccard" in e.meta_data]
    assert len(bumped) == 1
    assert bumped[0].pattern == "match"


# ---------------------------------------------------------------------------
# layer2_family signature back-compat
# ---------------------------------------------------------------------------

def test_layer2_family_accepts_current_hypothesis_kwarg():
    """Smoke: signature accepts the new current_hypothesis kwarg (defaults None)."""
    import inspect
    from backend.agents.hierarchical_rag import layer2_family
    sig = inspect.signature(layer2_family)
    assert "current_hypothesis" in sig.parameters
    assert sig.parameters["current_hypothesis"].default is None


def test_query_hierarchical_accepts_current_hypothesis_kwarg():
    import inspect
    from backend.agents.hierarchical_rag import query_hierarchical
    sig = inspect.signature(query_hierarchical)
    assert "current_hypothesis" in sig.parameters
    assert sig.parameters["current_hypothesis"].default is None
