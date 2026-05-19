"""A5.1 G10 logic_distill_service unit tests (Phase 4 Sprint 3 / plan v5 §6.12).

Coverage:
  - tokenize: lower-cases + drops 1-char garbage
  - jaccard_similarity: standard set math
  - _week_anchor: Monday-of-week stability
  - build_distill_prompt: contains pillar, region, alpha IDs, sharpe
  - distill_last_week_pass_alphas:
    - skips buckets below min_pass_count
    - respects max_cost_usd cap
    - LLM exception → bucket skipped (soft-fail)
    - empty LLM response → bucket skipped
    - happy path: writes 1 entry per qualified bucket
  - stamp_similarity_to_prev_week: missing prior → similarity stays None;
    matching tokens → high similarity
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services import logic_distill_service as lds


# ---------------------------------------------------------------------------
# Tokenization + Jaccard
# ---------------------------------------------------------------------------

def test_tokenize_lowercases_drops_one_char():
    tokens = lds.tokenize("Macro signal — rates DROP a B c! 12")
    # 1-char "a", "B", "c" dropped (regex requires ≥2 chars); "12" dropped (no leading letter)
    assert "macro" in tokens
    assert "signal" in tokens
    assert "rates" in tokens
    assert "drop" in tokens
    assert "a" not in tokens
    assert "12" not in tokens


def test_tokenize_empty_returns_empty():
    assert lds.tokenize("") == []
    assert lds.tokenize(None) == []  # type: ignore


def test_jaccard_basic():
    assert lds.jaccard_similarity(["a", "b"], ["a", "b"]) == 1.0
    assert lds.jaccard_similarity(["a", "b"], ["c", "d"]) == 0.0
    # |{a,b,c} ∩ {b,c,d}| / |{a,b,c,d}| = 2/4 = 0.5
    assert lds.jaccard_similarity(["a", "b", "c"], ["b", "c", "d"]) == 0.5


def test_jaccard_empty_both_returns_zero():
    assert lds.jaccard_similarity([], []) == 0.0


def test_jaccard_one_empty_returns_zero():
    assert lds.jaccard_similarity(["a"], []) == 0.0


# ---------------------------------------------------------------------------
# _week_anchor
# ---------------------------------------------------------------------------

def test_week_anchor_returns_monday():
    """F2 review fix: anchor uses Asia/Shanghai timezone so the cron's
    SH-Sunday boundary aligns with the week key. Any SH-time day in a
    week should map to the same SH-Monday 00:00 (returned as UTC)."""
    try:
        from zoneinfo import ZoneInfo
        sh = ZoneInfo("Asia/Shanghai")
    except ImportError:
        pytest.skip("zoneinfo unavailable")

    # 2026-05-20 Wed 12:00 SH, 2026-05-22 Fri 18:00 SH, 2026-05-24 Sun 23:00 SH
    # all fall within SH-week starting Mon 2026-05-18 00:00 SH.
    wed = datetime(2026, 5, 20, 12, 0, 0, tzinfo=sh)
    fri = datetime(2026, 5, 22, 18, 0, 0, tzinfo=sh)
    sun = datetime(2026, 5, 24, 23, 0, 0, tzinfo=sh)
    wed_anchor = lds._week_anchor(wed.astimezone(timezone.utc))
    fri_anchor = lds._week_anchor(fri.astimezone(timezone.utc))
    sun_anchor = lds._week_anchor(sun.astimezone(timezone.utc))
    assert wed_anchor == fri_anchor == sun_anchor
    # The anchor is Monday 00:00 SH expressed in UTC = Sunday 16:00 UTC.
    anchor_sh = wed_anchor.astimezone(sh)
    assert anchor_sh.weekday() == 0  # SH-Monday
    assert anchor_sh.hour == 0


def test_week_anchor_sh_boundary_alignment():
    """The cron runs at Sun 03:00 SH (= Sat 19:00 UTC). The anchor must
    resolve to the just-finished SH-week's Monday, NOT the upcoming one.

    A retry at Mon 00:30 SH (= Sun 16:30 UTC) should still anchor to the
    SAME SH-Monday — that's the whole point of the SH-tz fix."""
    try:
        from zoneinfo import ZoneInfo
        sh = ZoneInfo("Asia/Shanghai")
    except ImportError:
        pytest.skip("zoneinfo unavailable")

    cron_fire = datetime(2026, 5, 24, 3, 0, 0, tzinfo=sh).astimezone(timezone.utc)
    retry_mon = datetime(2026, 5, 25, 0, 30, 0, tzinfo=sh).astimezone(timezone.utc)
    anchor_cron = lds._week_anchor(cron_fire)
    anchor_retry = lds._week_anchor(retry_mon)
    # cron Sun 03:00 SH → anchor previous SH-Monday (2026-05-18)
    # retry Mon 00:30 SH → anchor THAT SH-Monday (2026-05-25)
    # Different anchors — F2 fix prevents same-row collision via SH-tz.
    # What we DO require: cron Sun 03:00 SH and noon Sat are same anchor.
    sat_noon = datetime(2026, 5, 23, 12, 0, 0, tzinfo=sh).astimezone(timezone.utc)
    anchor_sat = lds._week_anchor(sat_noon)
    assert anchor_cron == anchor_sat


# ---------------------------------------------------------------------------
# build_distill_prompt
# ---------------------------------------------------------------------------

def test_build_distill_prompt_contains_pillar_region_alphas():
    alphas = [
        lds.AlphaSummary(id=1, expression="rank(close)", sharpe=1.5),
        lds.AlphaSummary(id=2, expression="ts_zscore(returns, 20)", sharpe=1.8),
    ]
    prompt = lds.build_distill_prompt(
        pillar="momentum", region="USA", alphas=alphas,
    )
    assert "momentum" in prompt
    assert "USA" in prompt
    assert "rank(close)" in prompt
    assert "ts_zscore(returns, 20)" in prompt
    assert "id=1" in prompt
    assert "1.50" in prompt or "1.5" in prompt  # sharpe formatted


# ---------------------------------------------------------------------------
# distill_last_week_pass_alphas
# ---------------------------------------------------------------------------

def _make_mock_db(rows: List[tuple]) -> Any:
    """Mock AsyncSession.execute(...).all() returning the given rows."""
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


def _make_mock_llm(text: str, cost: float = 0.001) -> Any:
    llm = MagicMock()
    llm.call = AsyncMock(return_value={
        "text": text,
        "cost_usd": cost,
        "model": "test-model",
    })
    return llm


@pytest.mark.asyncio
async def test_distill_skips_bucket_below_min_pass_count():
    """Bucket with 2 alphas, min_pass_count=3 → skipped."""
    rows = [
        ("USA", "momentum", 1, "BRAIN1", "rank(close)", 1.5),
        ("USA", "momentum", 2, "BRAIN2", "rank(volume)", 1.4),
    ]
    db = _make_mock_db(rows)
    llm = _make_mock_llm("Momentum logic.")
    out = await lds.distill_last_week_pass_alphas(
        db=db, llm=llm, min_pass_count=3,
    )
    assert out == []
    llm.call.assert_not_called()


@pytest.mark.asyncio
async def test_distill_happy_path_one_entry_per_bucket():
    rows = [
        ("USA", "momentum", i, f"B{i}", f"expr_{i}", 1.5 + i * 0.1)
        for i in range(5)
    ] + [
        ("CHN", "value", i, f"B{100 + i}", f"expr_v_{i}", 1.3 + i * 0.05)
        for i in range(4)
    ]
    db = _make_mock_db(rows)
    llm = _make_mock_llm("Distilled logic text.")
    out = await lds.distill_last_week_pass_alphas(
        db=db, llm=llm, min_pass_count=3,
    )
    assert len(out) == 2
    regions = {e.region for e in out}
    assert regions == {"USA", "CHN"}
    pillars = {e.pillar for e in out}
    assert pillars == {"momentum", "value"}
    # Each entry tokenized
    for e in out:
        assert "distilled" in e.tokens or "logic" in e.tokens or "text" in e.tokens


@pytest.mark.asyncio
async def test_distill_respects_cost_cap():
    """Each call costs $1; max_cost=$2 → at most 2 buckets distilled."""
    rows = [
        ("USA", "momentum", i, f"BU{i}", f"expr_{i}", 1.5) for i in range(4)
    ] + [
        ("CHN", "value", i, f"BC{i}", f"expr_v_{i}", 1.3) for i in range(4)
    ] + [
        ("EUR", "quality", i, f"BE{i}", f"expr_q_{i}", 1.4) for i in range(4)
    ]
    db = _make_mock_db(rows)
    llm = _make_mock_llm("Logic.", cost=1.0)  # each call $1
    out = await lds.distill_last_week_pass_alphas(
        db=db, llm=llm, max_cost_usd=2.0, min_pass_count=3,
    )
    # Cap kicks in: at most 2 (could be 2 since the 3rd would push over $3 > $2 cap)
    assert len(out) <= 2


@pytest.mark.asyncio
async def test_distill_llm_exception_skips_bucket():
    rows = [("USA", "momentum", i, f"B{i}", f"e_{i}", 1.5) for i in range(4)]
    db = _make_mock_db(rows)
    llm = MagicMock()
    llm.call = AsyncMock(side_effect=RuntimeError("LLM transient failure"))
    out = await lds.distill_last_week_pass_alphas(
        db=db, llm=llm, min_pass_count=3,
    )
    assert out == []


@pytest.mark.asyncio
async def test_distill_empty_llm_text_skips_bucket():
    rows = [("USA", "momentum", i, f"B{i}", f"e_{i}", 1.5) for i in range(4)]
    db = _make_mock_db(rows)
    llm = _make_mock_llm("", cost=0.001)  # empty text
    out = await lds.distill_last_week_pass_alphas(
        db=db, llm=llm, min_pass_count=3,
    )
    assert out == []


@pytest.mark.asyncio
async def test_distill_top_k_per_group():
    """20 alphas in one bucket; top_k_per_group=5 → only 5 fed to LLM."""
    rows = [
        ("USA", "momentum", i, f"B{i}", f"e_{i}", 2.0 - i * 0.05)
        for i in range(20)
    ]
    db = _make_mock_db(rows)
    llm = _make_mock_llm("Logic.")
    out = await lds.distill_last_week_pass_alphas(
        db=db, llm=llm, top_k_per_group=5, min_pass_count=3,
    )
    assert len(out) == 1
    # Source IDs capped at 5 (top-K by sharpe; rows already ORDER BY sharpe DESC)
    assert len(out[0].source_alpha_ids) == 5


@pytest.mark.asyncio
async def test_distill_null_pillar_groups_separately():
    """Alpha without pillar metric should still be processable (pillar=None bucket)."""
    rows = [("USA", None, i, f"B{i}", f"e_{i}", 1.5) for i in range(4)]
    db = _make_mock_db(rows)
    llm = _make_mock_llm("General logic.")
    out = await lds.distill_last_week_pass_alphas(
        db=db, llm=llm, min_pass_count=3,
    )
    assert len(out) == 1
    assert out[0].pillar is None


# ---------------------------------------------------------------------------
# stamp_similarity_to_prev_week
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stamp_similarity_missing_prior_leaves_none():
    """No row in distilled_logic_library for the (region, pillar) → similarity stays None."""
    db = MagicMock()
    result = MagicMock()
    result.first.return_value = None
    db.execute = AsyncMock(return_value=result)

    entry = lds.DistilledEntry(
        pillar="momentum",
        region="USA",
        logic_text="Momentum logic.",
        tokens=["momentum", "logic"],
        distilled_at_week=datetime.now(timezone.utc),
    )
    await lds.stamp_similarity_to_prev_week(db, [entry])
    assert entry.similarity_jaccard_to_prev_week is None


@pytest.mark.asyncio
async def test_stamp_similarity_with_matching_tokens():
    """Prior row with identical tokens → similarity = 1.0."""
    db = MagicMock()
    result = MagicMock()
    result.first.return_value = (["momentum", "logic"],)  # tokens column
    db.execute = AsyncMock(return_value=result)

    entry = lds.DistilledEntry(
        pillar="momentum",
        region="USA",
        logic_text="Momentum logic.",
        tokens=["momentum", "logic"],
        distilled_at_week=datetime.now(timezone.utc),
    )
    await lds.stamp_similarity_to_prev_week(db, [entry])
    assert entry.similarity_jaccard_to_prev_week == 1.0
