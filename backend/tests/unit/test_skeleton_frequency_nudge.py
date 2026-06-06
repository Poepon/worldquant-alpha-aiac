"""Pool Phase 2 (R1a-v1) — skeleton-frequency soft de-prioritization nudge.

Builder mines recent crowded SUCCESS_PATTERN skeletons (by usage_count) for a
region and renders a SOFT prompt nudge (prefer-novel, NOT a forbidden list).
Default OFF → byte-for-byte legacy. Sample-size-gated + [:5] cap + field-aware.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.agents.prompts.base import PromptContext
from backend.agents.prompts.hypothesis import build_hypothesis_prompt
from backend.agents.prompts.skeleton_frequency import (
    _fields_hint,
    _row_regions,
    skeleton_frequency_nudge_block,
)
from backend.config import settings
from backend.database import SQLAlchemyBase
from backend.models import KnowledgeEntry


async def _setup_db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    return eng, async_sessionmaker(eng, expire_on_commit=False)


def _kb(pattern, usage_count, region="USA", days_ago=0, meta_extra=None):
    meta = {"region": region, "regions": [region]}
    if meta_extra:
        meta.update(meta_extra)
    return KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=pattern, usage_count=usage_count,
        is_active=True, meta_data=meta,
        updated_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


@pytest.fixture
def flag_on():
    orig = settings.ENABLE_R1A_KB_SKELETON_FREQUENCY
    settings.ENABLE_R1A_KB_SKELETON_FREQUENCY = True
    try:
        yield
    finally:
        settings.ENABLE_R1A_KB_SKELETON_FREQUENCY = orig


class TestHelpers:
    def test_row_regions(self):
        assert _row_regions({"region": "USA"}) == {"USA"}
        assert _row_regions({"regions": ["USA", "EUR"]}) == {"USA", "EUR"}
        assert _row_regions(None) == set()

    def test_fields_hint(self):
        assert "price_volume" in _fields_hint({"dataset_categories_used": ["price_volume", "x"]})
        assert _fields_hint({}) == ""


@pytest.mark.asyncio
async def test_flag_off_returns_empty():
    """Default OFF → no query, "" (byte-for-byte legacy)."""
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            s.add(_kb("ts_rank(x)", 5))
            await s.commit()
            assert await skeleton_frequency_nudge_block(s, region="USA") == ""
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_too_few_samples(flag_on):
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            s.add(_kb("ts_rank(x)", 5))
            s.add(_kb("ts_mean(y)", 3))  # 2 rows < MIN_SAMPLES (3)
            await s.commit()
            assert await skeleton_frequency_nudge_block(s, region="USA") == ""
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_no_crowding_all_singletons(flag_on):
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            for i in range(4):
                s.add(_kb(f"sk{i}", 1))  # all usage_count==1 → diverse, not crowded
            await s.commit()
            assert await skeleton_frequency_nudge_block(s, region="USA") == ""
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_renders_crowded(flag_on):
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            s.add(_kb("ts_rank(close,5)", 12, meta_extra={"dataset_categories_used": ["price_volume"]}))
            s.add(_kb("ts_mean(returns,20)", 4))
            s.add(_kb("rank(x)", 1))  # singleton → excluded from crowded list
            await s.commit()
            out = await skeleton_frequency_nudge_block(s, region="USA")
            assert "Crowded Structures" in out
            assert "ts_rank(close,5)" in out and "12" in out
            assert "price_volume" in out          # field-aware hint
            assert "rank(x)" not in out            # singleton not listed
            assert "NOT forbidden" in out          # soft, not a forbidden list
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_window_excludes_old(flag_on):
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            s.add(_kb("old_sk", 99, days_ago=999))  # outside the 30d window
            for p in ("a", "b", "c"):
                s.add(_kb(p, 3))                      # 3 in-window
            await s.commit()
            out = await skeleton_frequency_nudge_block(s, region="USA")
            assert "old_sk" not in out
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_region_filter(flag_on):
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            for i in range(4):
                s.add(_kb(f"eur{i}", 5, region="EUR"))
            await s.commit()
            assert await skeleton_frequency_nudge_block(s, region="USA") == ""
    finally:
        await eng.dispose()


class TestPromptByteForByteLegacy:
    def _ctx(self, block):
        return PromptContext(dataset_id="pv1", region="USA", universe="TOP3000",
                             crowded_skeletons_block=block)

    def test_none_omits_block(self):
        assert "Crowded Structures" not in build_hypothesis_prompt(self._ctx(None), [])

    def test_value_includes_block(self):
        p = build_hypothesis_prompt(self._ctx("## Crowded Structures\n\n- `x` (~5×)"), [])
        assert "Crowded Structures" in p

    def test_none_and_empty_identical(self):
        # "" and None both → no block → identical prompt (byte-for-byte legacy).
        assert build_hypothesis_prompt(self._ctx(None), []) == build_hypothesis_prompt(self._ctx(""), [])
