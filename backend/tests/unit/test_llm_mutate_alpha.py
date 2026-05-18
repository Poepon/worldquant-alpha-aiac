"""Phase 3 flat-F3 unit tests for llm_mutate_alpha module.

Coverage:
  - build_mutate_prompt: defaults, truncation, top_k clipping
  - _parse_variants: valid / malformed / non-dict / missing variants /
    item without <SEED> / item not a dict
  - _substitute_seed: placeholder replacement
  - llm_mutate_alpha: empty seed early return / LLM exception soft-fail /
    happy path mock / LLM returns malformed → empty
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.llm_mutate_alpha import (
    MUTATE_SYSTEM,
    _parse_variants,
    _substitute_seed,
    build_mutate_prompt,
    llm_mutate_alpha,
)


# ---------------------------------------------------------------------------
# build_mutate_prompt
# ---------------------------------------------------------------------------

def test_build_mutate_prompt_defaults():
    p = build_mutate_prompt("rank(close)", region="USA")
    assert "rank(close)" in p
    assert "USA" in p
    assert "(no recent failures recorded)" in p
    assert "(no decay-related concerns)" in p
    assert "Return at most 3 variants" in p


def test_build_mutate_prompt_top_k_clipped():
    """top_k bounded to [1, 5]."""
    p1 = build_mutate_prompt("x", region="USA", top_k=99)
    assert "Return at most 5 variants" in p1
    p2 = build_mutate_prompt("x", region="USA", top_k=0)
    assert "Return at most 1 variants" in p2


def test_build_mutate_prompt_truncates_seed():
    """Long seed truncated to 500 chars to bound prompt size."""
    long_seed = "x" * 1000
    p = build_mutate_prompt(long_seed, region="USA")
    # Seed line should not include all 1000 chars
    assert long_seed[:500] in p
    assert long_seed not in p


def test_build_mutate_prompt_passes_context():
    failure = "Recent T2 sweep group_zscore_industry: 12/12 FAIL (sharpe<0.8)"
    decay = "Q9 reference: rank(close) decayed -65% post-Fama-French"
    p = build_mutate_prompt("rank(close)", region="USA",
                             failure_context=failure, decay_context=decay)
    assert "12/12 FAIL" in p
    assert "Fama-French" in p


# ---------------------------------------------------------------------------
# _parse_variants
# ---------------------------------------------------------------------------

def test_parse_variants_valid():
    raw = json.dumps({
        "variants": [
            {"expression": "group_neutralize(<SEED>, industry)",
             "wrapper_kind": "group_neutralize_industry",
             "rationale": "reduces industry beta"},
            {"expression": "rank(<SEED>)",
             "wrapper_kind": "rank_xs", "rationale": "cs-normalize"},
        ]
    })
    out = _parse_variants(raw, max_variants=3)
    assert len(out) == 2
    assert out[0]["expression"] == "group_neutralize(<SEED>, industry)"
    assert out[0]["wrapper_kind"] == "group_neutralize_industry"


def test_parse_variants_max_variants_cap():
    raw = json.dumps({
        "variants": [{"expression": f"op{i}(<SEED>)", "wrapper_kind": f"k{i}",
                      "rationale": f"r{i}"} for i in range(5)]
    })
    out = _parse_variants(raw, max_variants=2)
    assert len(out) == 2


def test_parse_variants_drops_items_without_seed_placeholder():
    """Variant must reference <SEED> — else dropped (caller can't substitute)."""
    raw = json.dumps({
        "variants": [
            {"expression": "group_neutralize(<SEED>, industry)",
             "wrapper_kind": "k1", "rationale": "ok"},
            {"expression": "no_placeholder(close)",  # no <SEED> — drop
             "wrapper_kind": "k2", "rationale": "broken"},
        ]
    })
    out = _parse_variants(raw, max_variants=5)
    assert len(out) == 1
    assert out[0]["wrapper_kind"] == "k1"


def test_parse_variants_malformed_returns_empty():
    out = _parse_variants("not json", max_variants=3)
    assert out == []


def test_parse_variants_non_dict_returns_empty():
    raw = json.dumps([1, 2, 3])  # list, not dict
    assert _parse_variants(raw, max_variants=3) == []


def test_parse_variants_missing_variants_key_returns_empty():
    assert _parse_variants(json.dumps({}), max_variants=3) == []


def test_parse_variants_variants_not_list_returns_empty():
    assert _parse_variants(json.dumps({"variants": "garbage"}), max_variants=3) == []


def test_parse_variants_skips_non_dict_items():
    raw = json.dumps({"variants": [
        "string-not-dict",
        {"expression": "rank(<SEED>)", "wrapper_kind": "k", "rationale": "ok"},
        12345,
    ]})
    out = _parse_variants(raw, max_variants=5)
    assert len(out) == 1


def test_parse_variants_dedupes_identical_expressions():
    """M7 fix: LLM returning duplicate wrapper expressions collapses to 1
    candidate (low-temp models often emit identicals; dedupe honors the
    flat-F3 cost-saving claim by not wasting BRAIN sim cycles)."""
    raw = json.dumps({"variants": [
        {"expression": "group_neutralize(<SEED>, industry)",
         "wrapper_kind": "group_neutralize_industry",
         "rationale": "first occurrence"},
        {"expression": "group_neutralize(<SEED>, industry)",  # exact dup
         "wrapper_kind": "group_neutralize_industry_alt",
         "rationale": "duplicate — must be skipped"},
        {"expression": "rank(<SEED>)",
         "wrapper_kind": "rank_xs",
         "rationale": "distinct — must survive"},
    ]})
    out = _parse_variants(raw, max_variants=3)
    assert len(out) == 2
    expressions = [v["expression"] for v in out]
    assert expressions == [
        "group_neutralize(<SEED>, industry)",
        "rank(<SEED>)",
    ]
    # First-seen wins (the dup's wrapper_kind/rationale must NOT replace it)
    assert out[0]["wrapper_kind"] == "group_neutralize_industry"
    assert out[0]["rationale"] == "first occurrence"


def test_parse_variants_supplies_default_wrapper_kind():
    """Missing wrapper_kind → default 'llm_mutate_unspecified'."""
    raw = json.dumps({"variants": [
        {"expression": "rank(<SEED>)", "rationale": "ok"},
    ]})
    out = _parse_variants(raw, max_variants=3)
    assert out[0]["wrapper_kind"] == "llm_mutate_unspecified"


# ---------------------------------------------------------------------------
# _substitute_seed
# ---------------------------------------------------------------------------

def test_substitute_seed_basic():
    v = {"expression": "group_rank(<SEED>, industry)", "wrapper_kind": "k", "rationale": "r"}
    out = _substitute_seed(v, "rank(close)")
    assert out["expression"] == "group_rank(rank(close), industry)"
    assert out["wrapper_kind"] == "k"
    assert out["rationale"] == "r"


def test_substitute_seed_multiple_occurrences():
    """Both <SEED> placeholders replaced."""
    v = {"expression": "subtract(<SEED>, group_mean(<SEED>, cap, industry))",
         "wrapper_kind": "k", "rationale": "r"}
    out = _substitute_seed(v, "ts_mean(close, 20)")
    assert out["expression"].count("<SEED>") == 0
    assert out["expression"].count("ts_mean(close, 20)") == 2


# ---------------------------------------------------------------------------
# llm_mutate_alpha (async)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_mutate_empty_seed_returns_empty():
    """Empty seed → early return without LLM call."""
    llm = SimpleNamespace()
    llm.call = AsyncMock(side_effect=AssertionError("should not call LLM"))
    out = await llm_mutate_alpha("", region="USA", llm_service=llm)
    assert out == []


@pytest.mark.asyncio
async def test_llm_mutate_exception_returns_empty():
    """LLM exception → soft-fail empty (caller falls back to legacy)."""
    llm = SimpleNamespace()
    llm.call = AsyncMock(side_effect=RuntimeError("API timeout"))
    out = await llm_mutate_alpha("rank(close)", region="USA", llm_service=llm)
    assert out == []


@pytest.mark.asyncio
async def test_llm_mutate_happy_path():
    """Mock LLM returns valid JSON → variants returned with <SEED> substituted."""
    raw = json.dumps({"variants": [
        {"expression": "group_neutralize(<SEED>, industry)",
         "wrapper_kind": "group_neutralize_industry",
         "rationale": "neutralize industry beta"},
        {"expression": "winsorize(<SEED>, std=4)",
         "wrapper_kind": "winsorize_std4",
         "rationale": "clip outliers"},
    ]})
    llm = SimpleNamespace()
    llm.call = AsyncMock(return_value=SimpleNamespace(content=raw))
    out = await llm_mutate_alpha("rank(close)", region="USA",
                                  llm_service=llm, top_k=3)
    assert len(out) == 2
    # <SEED> already substituted
    assert "<SEED>" not in out[0]["expression"]
    assert "rank(close)" in out[0]["expression"]
    assert out[0]["wrapper_kind"] == "group_neutralize_industry"


@pytest.mark.asyncio
async def test_llm_mutate_returns_empty_when_llm_returns_empty():
    """LLM returns valid JSON but variants=[] → empty result (caller fallback)."""
    llm = SimpleNamespace()
    llm.call = AsyncMock(return_value=SimpleNamespace(content='{"variants":[]}'))
    out = await llm_mutate_alpha("rank(close)", region="USA", llm_service=llm)
    assert out == []


@pytest.mark.asyncio
async def test_llm_mutate_returns_empty_when_llm_returns_garbage():
    """LLM returns non-JSON → empty (graceful fallback)."""
    llm = SimpleNamespace()
    llm.call = AsyncMock(return_value=SimpleNamespace(content="this is not JSON"))
    out = await llm_mutate_alpha("rank(close)", region="USA", llm_service=llm)
    assert out == []
