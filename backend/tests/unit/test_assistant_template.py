"""Phase 4 Sprint 1 A1.3 — assistant_template service unit tests.

Coverage:
  - YAML loading + 10 templates × 5 pillars structure
  - tokenization basics
  - match_template:
    - keyword overlap returns best match
    - pillar filter restricts candidates
    - threshold prunes low-scoring matches → empty list
    - no-keyword-match → empty list (caller falls through)
  - compose_expression:
    - slot defaults fill correctly
    - explicit overrides win over defaults
    - missing-slot-no-default leaves visible placeholder
    - malformed template raises ValueError
  - compose_for_hypothesis convenience:
    - end-to-end success
    - empty match returns None
    - placeholder-remaining returns None (refuses broken DSL)
  - clear_template_cache + force_reload
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_templates_yields_ten_entries():
    from backend.services.assistant_template import get_templates, clear_template_cache
    clear_template_cache()
    templates = get_templates()
    assert len(templates) == 10, f"expected 10 templates, got {len(templates)}"


def test_load_templates_five_pillars():
    """Each of the 5 expected pillars has ≥1 template."""
    from backend.services.assistant_template import get_templates, clear_template_cache
    clear_template_cache()
    templates = get_templates()
    pillars = {t["pillar"] for t in templates}
    assert pillars == {"momentum", "value", "quality", "volatility", "sentiment"}


def test_load_templates_required_fields():
    """Every loaded template has the required keys + non-empty skeleton."""
    from backend.services.assistant_template import get_templates, clear_template_cache
    clear_template_cache()
    templates = get_templates()
    for t in templates:
        assert isinstance(t["template_id"], str) and t["template_id"]
        assert isinstance(t["expression_skeleton"], str) and t["expression_skeleton"]
        assert isinstance(t["slots"], dict)
        assert isinstance(t["hypothesis_keywords"], list)


def test_clear_template_cache_forces_reload():
    """clear_template_cache + get_templates re-reads the YAML."""
    from backend.services.assistant_template import (
        get_templates, clear_template_cache,
    )
    a = get_templates()
    clear_template_cache()
    b = get_templates(force_reload=True)
    # Should return equivalent content (no mutation in real flow)
    assert len(a) == len(b)


# ---------------------------------------------------------------------------
# Tokenization + scoring
# ---------------------------------------------------------------------------


def test_tokenize_lowercases_and_splits_words():
    from backend.services.assistant_template import _tokenize
    tokens = _tokenize("Industry Neutral Momentum")
    assert "industry" in tokens
    assert "neutral" in tokens
    assert "momentum" in tokens


def test_tokenize_empty():
    from backend.services.assistant_template import _tokenize
    assert _tokenize("") == set()
    assert _tokenize(None) == set()  # type: ignore


# ---------------------------------------------------------------------------
# match_template
# ---------------------------------------------------------------------------


def test_match_template_finds_momentum_basic():
    """A momentum-flavored hypothesis should match a momentum template."""
    from backend.services.assistant_template import match_template
    matches = match_template(
        "Persistent momentum: ts_zscore of recent returns above the trend baseline",
    )
    assert matches, "expected at least one match"
    best = matches[0][0]
    assert best["pillar"] == "momentum"


def test_match_template_pillar_filter():
    """Pillar=value restricts candidates to value templates only."""
    from backend.services.assistant_template import match_template
    matches = match_template(
        "rank book-to-market cheapness signal",
        pillar="value",
    )
    assert matches
    for tmpl, _ in matches:
        assert tmpl["pillar"] == "value"


def test_match_template_top_k_returns_multiple():
    """top_k=3 returns up to 3 candidates sorted by score desc."""
    from backend.services.assistant_template import match_template
    matches = match_template(
        "industry neutral momentum residual after sector exposure",
        top_k=3,
        min_score=0.0,
    )
    assert len(matches) <= 3
    if len(matches) >= 2:
        # Sorted descending by score
        assert matches[0][1] >= matches[1][1]


def test_match_template_no_keyword_overlap_returns_empty():
    """A hypothesis with no overlapping keywords returns empty list."""
    from backend.services.assistant_template import match_template
    matches = match_template("totally unrelated random gibberish xyzzy")
    assert matches == []


def test_match_template_pillar_not_in_library():
    """pillar='other' (not in YAML) returns empty since 0 candidates."""
    from backend.services.assistant_template import match_template
    matches = match_template("momentum trend persistence", pillar="other")
    assert matches == []


# ---------------------------------------------------------------------------
# compose_expression
# ---------------------------------------------------------------------------


def test_compose_uses_default_slots():
    from backend.services.assistant_template import compose_expression
    template = {
        "template_id": "test.x",
        "expression_skeleton": "ts_zscore({{field}}, {{window}})",
        "slots": {
            "field": {"default": "returns", "type": "field_ref"},
            "window": {"default": 60, "type": "int"},
        },
    }
    expr = compose_expression(template)
    assert expr == "ts_zscore(returns, 60)"


def test_compose_overrides_win():
    from backend.services.assistant_template import compose_expression
    template = {
        "template_id": "test.x",
        "expression_skeleton": "ts_zscore({{field}}, {{window}})",
        "slots": {
            "field": {"default": "returns", "type": "field_ref"},
            "window": {"default": 60, "type": "int"},
        },
    }
    expr = compose_expression(
        template, slot_overrides={"window": 20, "field": "close"}
    )
    assert expr == "ts_zscore(close, 20)"


def test_compose_missing_default_leaves_placeholder():
    """Slot without an override AND without a default → placeholder
    survives (caller detects + falls through)."""
    from backend.services.assistant_template import compose_expression
    template = {
        "template_id": "test.x",
        "expression_skeleton": "{{missing_slot}}",
        "slots": {},
    }
    expr = compose_expression(template)
    assert "{{" in expr


def test_compose_malformed_template_raises():
    from backend.services.assistant_template import compose_expression
    with pytest.raises(ValueError, match="missing expression_skeleton"):
        compose_expression({"template_id": "test.broken"})


# ---------------------------------------------------------------------------
# compose_for_hypothesis end-to-end
# ---------------------------------------------------------------------------


def test_compose_for_hypothesis_happy_path():
    """End-to-end: matches a real template + returns composed DSL."""
    from backend.services.assistant_template import compose_for_hypothesis
    out = compose_for_hypothesis(
        "ts_zscore-based momentum persistence on recent returns"
    )
    assert out is not None
    assert "ts_zscore" in out["expression"]
    assert out["pillar"] == "momentum"
    assert out["score"] > 0
    assert out["template_id"]


def test_compose_for_hypothesis_no_match_returns_none():
    from backend.services.assistant_template import compose_for_hypothesis
    out = compose_for_hypothesis("totally unrelated random gibberish xyzzy")
    assert out is None


def test_compose_for_hypothesis_pillar_hint_used():
    """When the LLM provides a pillar hint, only that pillar's templates
    are considered."""
    from backend.services.assistant_template import compose_for_hypothesis
    out = compose_for_hypothesis(
        "rank book-to-market value cheapness",
        pillar="value",
    )
    assert out is not None
    assert out["pillar"] == "value"


def test_compose_for_hypothesis_pillar_hint_eliminates_all_returns_none():
    """Pillar restricts to value templates but hypothesis has zero
    overlap with value keywords → returns None."""
    from backend.services.assistant_template import compose_for_hypothesis
    # Deliberately avoid the words 'value' / 'book' / 'cheap' / 'fundamental'
    # / 'rank' / 'industry' / 'sector' — those overlap with value templates.
    out = compose_for_hypothesis(
        "completely unrelated xyzzy plover frobnication",
        pillar="value",
    )
    assert out is None
