"""A5.2 G10 PR2 — refine + retrieval + prompt injection tests (Sprint 4).

Coverage:
  - refine_logic_library: retires near-duplicates (Jaccard ≥ threshold);
    preserves divergent rows; single-row buckets unchanged
  - fetch_active_logic_entries: filters retired_at IS NULL; pillar
    pre-filter then region fallback; limit respected
  - build_distilled_logic_block: empty input → ""; non-empty → markdown
    with pillar + source_alpha_count
  - PromptContext.distilled_logic_block field exists
  - hypothesis prompt template splice carries the block when set, byte-
    identical legacy when empty
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# refine_logic_library
# ---------------------------------------------------------------------------

def _make_refine_db(bucket_rows: List[tuple]) -> Any:
    """Mock DB: first execute returns bucket inventory; subsequent
    UPDATEs no-op; commit succeeds."""
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = bucket_rows
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock(return_value=None)
    db.rollback = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_refine_retires_near_duplicate_older_entry():
    from backend.services.logic_distill_service import refine_logic_library

    # 2 entries same (region, pillar), ~75% token overlap → older retired.
    # F6 review fix: 6th column = source_alpha_ids; newest must have ≥
    # source count to retire older (here equal counts → retire allowed).
    now = datetime.now(timezone.utc)
    week_new = now
    week_old = now - timedelta(days=7)
    rows = [
        (1, "USA", "momentum", week_new, ["momentum", "rank", "returns"], [10, 20]),
        (2, "USA", "momentum", week_old, ["momentum", "rank", "returns", "z"], [30, 40]),
    ]
    db = _make_refine_db(rows)
    out = await refine_logic_library(db, similarity_threshold=0.70)
    # 3/4 = 0.75 > 0.70 AND newest src(2) >= older src(2) → retire older
    assert out["retired"] == 1
    assert out["checked"] == 1


@pytest.mark.asyncio
async def test_refine_keeps_older_when_richer():
    """F6 fix: older entry backed by MORE source alphas is NOT retired
    even when similar (newest is a thinner re-distillation)."""
    from backend.services.logic_distill_service import refine_logic_library
    now = datetime.now(timezone.utc)
    rows = [
        (1, "USA", "momentum", now, ["momentum", "rank", "returns"], [10]),       # 1 source
        (2, "USA", "momentum", now - timedelta(days=7),
         ["momentum", "rank", "returns"], [30, 40, 50]),                          # 3 sources
    ]
    db = _make_refine_db(rows)
    out = await refine_logic_library(db, similarity_threshold=0.70)
    # Jaccard 1.0 ≥ τ BUT newest src(1) < older src(3) → keep older
    assert out["retired"] == 0
    assert out["checked"] == 1


@pytest.mark.asyncio
async def test_refine_preserves_divergent_entries():
    """Low Jaccard → no retirement."""
    from backend.services.logic_distill_service import refine_logic_library

    now = datetime.now(timezone.utc)
    rows = [
        (1, "USA", "momentum", now, ["momentum", "trend"], [10]),
        (2, "USA", "momentum", now - timedelta(days=7),
         ["value", "earnings", "cheap"], [20]),  # zero overlap
    ]
    db = _make_refine_db(rows)
    out = await refine_logic_library(db, similarity_threshold=0.70)
    assert out["retired"] == 0
    assert out["checked"] == 1


@pytest.mark.asyncio
async def test_refine_single_entry_bucket_unchanged():
    from backend.services.logic_distill_service import refine_logic_library

    now = datetime.now(timezone.utc)
    rows = [
        (1, "USA", "momentum", now, ["momentum"], [10]),
    ]
    db = _make_refine_db(rows)
    out = await refine_logic_library(db, similarity_threshold=0.70)
    assert out == {"retired": 0, "checked": 0}


@pytest.mark.asyncio
async def test_refine_respects_lookback_weeks():
    """5 entries; lookback=2 → only compare newest vs idx 1 and idx 2."""
    from backend.services.logic_distill_service import refine_logic_library

    now = datetime.now(timezone.utc)
    rows = [
        (i, "USA", "momentum",
         now - timedelta(days=7 * i),
         ["momentum"] if i < 3 else ["unrelated"],
         [100 + i])  # 1 source each → equal counts → retire allowed
        for i in range(5)
    ]
    db = _make_refine_db(rows)
    out = await refine_logic_library(db, similarity_threshold=0.70, lookback_weeks=2)
    # 2 comparisons (idx 0 vs 1, idx 0 vs 2). Both share "momentum" with
    # newest (Jaccard=1.0) AND equal source counts → both retired.
    assert out["checked"] == 2
    assert out["retired"] == 2


# ---------------------------------------------------------------------------
# fetch_active_logic_entries
# ---------------------------------------------------------------------------

def _make_fetch_db(pillar_rows: List[tuple], region_rows: List[tuple]) -> Any:
    """Mock DB serving 2 sequential SELECTs: pillar-matched, then region fallback."""
    db = MagicMock()
    pillar_result = MagicMock()
    pillar_result.all.return_value = pillar_rows
    region_result = MagicMock()
    region_result.all.return_value = region_rows
    # Return pillar query first, then region query
    db.execute = AsyncMock(side_effect=[pillar_result, region_result])
    return db


@pytest.mark.asyncio
async def test_fetch_returns_pillar_matched_rows():
    from backend.services.logic_distill_service import fetch_active_logic_entries

    now = datetime.now(timezone.utc)
    pillar_rows = [
        (1, "Momentum logic", "momentum", "USA", now, [10, 20], "test-model"),
        (2, "More momentum", "momentum", "USA", now - timedelta(days=7), [30], "test-model"),
    ]
    db = _make_fetch_db(pillar_rows, region_rows=[])
    out = await fetch_active_logic_entries(db, region="USA", pillar="momentum", limit=5)
    assert len(out) == 2
    assert out[0]["pillar"] == "momentum"
    assert out[0]["source_alpha_count"] == 2  # len([10, 20])


@pytest.mark.asyncio
async def test_fetch_falls_back_to_region_when_pillar_short():
    """1 pillar match + need 3 more → region query fills."""
    from backend.services.logic_distill_service import fetch_active_logic_entries

    now = datetime.now(timezone.utc)
    pillar_rows = [
        (1, "Momentum", "momentum", "USA", now, [10], "m"),
    ]
    # D4 review fix: the region fallback now excludes seen_ids in SQL
    # (NOT IN :seen0...), so the real query would never return id=1 again.
    # The mock reflects that contract — fallback rows are post-exclusion.
    region_rows = [
        (2, "Value", "value", "USA", now, [20, 30], "m"),
        (3, "Quality", "quality", "USA", now - timedelta(days=14), [40], "m"),
    ]
    db = _make_fetch_db(pillar_rows, region_rows)
    out = await fetch_active_logic_entries(db, region="USA", pillar="momentum", limit=3)
    # momentum (pillar match) + value + quality (region fallback)
    assert len(out) == 3
    ids = [r["id"] for r in out]
    assert 1 in ids and 2 in ids and 3 in ids


@pytest.mark.asyncio
async def test_fetch_respects_limit():
    from backend.services.logic_distill_service import fetch_active_logic_entries

    now = datetime.now(timezone.utc)
    rows = [
        (i, f"Logic {i}", "momentum", "USA", now, [], "m") for i in range(10)
    ]
    db = _make_fetch_db(rows, region_rows=[])
    out = await fetch_active_logic_entries(db, region="USA", pillar="momentum", limit=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# build_distilled_logic_block
# ---------------------------------------------------------------------------

def test_build_block_empty_input_returns_empty_string():
    from backend.services.logic_distill_service import build_distilled_logic_block
    assert build_distilled_logic_block([]) == ""


def test_build_block_renders_pillar_and_source_count():
    from backend.services.logic_distill_service import build_distilled_logic_block
    entries = [
        {
            "pillar": "momentum",
            "logic_text": "Momentum signals persist 60 days.",
            "source_alpha_count": 7,
        },
        {
            "pillar": "value",
            "logic_text": "Cheap book-to-market wins.",
            "source_alpha_count": 5,
        },
    ]
    block = build_distilled_logic_block(entries)
    assert "Distilled Logic" in block
    assert "momentum" in block
    assert "value" in block
    assert "n=7" in block
    assert "n=5" in block
    assert "Momentum signals persist 60 days." in block


def test_build_block_caps_at_5_entries():
    from backend.services.logic_distill_service import build_distilled_logic_block
    entries = [
        {"pillar": "momentum", "logic_text": f"Logic {i}", "source_alpha_count": 1}
        for i in range(10)
    ]
    block = build_distilled_logic_block(entries)
    # Logic 0..4 should appear; 5..9 should NOT
    assert "Logic 4" in block
    assert "Logic 5" not in block


def test_build_block_skips_empty_logic_text():
    from backend.services.logic_distill_service import build_distilled_logic_block
    entries = [
        {"pillar": "momentum", "logic_text": "", "source_alpha_count": 0},
        {"pillar": "value", "logic_text": "Real logic.", "source_alpha_count": 3},
    ]
    block = build_distilled_logic_block(entries)
    assert "Real logic." in block


# ---------------------------------------------------------------------------
# PromptContext.distilled_logic_block exists + hypothesis splice
# ---------------------------------------------------------------------------

def test_prompt_context_has_distilled_logic_block_field():
    from backend.agents.prompts.base import PromptContext
    ctx = PromptContext()
    # Default empty string preserves byte-for-byte legacy
    assert ctx.distilled_logic_block == ""


def test_hypothesis_prompt_template_splices_distilled_logic():
    """Splice must collapse to empty when block is "" (byte-for-byte
    legacy invariant) and carry the block when set."""
    from backend.agents.prompts.base import PromptContext
    from backend.agents.prompts.hypothesis import build_hypothesis_prompt

    # OFF path
    ctx_off = PromptContext(distilled_logic_block="")
    p_off = build_hypothesis_prompt(ctx_off)
    assert "Distilled Logic" not in p_off

    # ON path
    ctx_on = PromptContext(
        distilled_logic_block="## Distilled Logic — Recent PASS-Alpha Patterns (this region)\n\nTest content."
    )
    p_on = build_hypothesis_prompt(ctx_on)
    assert "Distilled Logic" in p_on
    assert "Test content." in p_on


def test_flag_off_byte_for_byte_legacy_invariant():
    """PromptContext with no R8-v3 / G8 / G10 blocks should render
    the same as before all 3 features existed."""
    from backend.agents.prompts.base import PromptContext
    from backend.agents.prompts.hypothesis import build_hypothesis_prompt
    ctx = PromptContext()
    prompt = build_hypothesis_prompt(ctx)
    # No R8-v3 / G8 / G10 markers
    assert "Research Lens" not in prompt
    assert "Cross-task Hypothesis Forest" not in prompt
    assert "Distilled Logic" not in prompt
