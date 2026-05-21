"""Tests for the dataset_categories backfill (2026-05-21).

Covers the script's pure helpers and the resolver against the REAL datafields
catalog (PG) — the load-bearing step (field_id → category). The full --apply
path is exercised by the script's own --dry-run on live data (it must never
write in a test).
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import socket
import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

# Import the standalone script module by path (scripts/ is not a package).
_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "backfill_kb_dataset_categories.py"
_spec = importlib.util.spec_from_file_location("backfill_kb_dataset_categories", _SCRIPT)
bk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bk)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_is_skeleton_detects_placeholders():
    assert bk._is_skeleton("ts_rank(FIELD, NUM)") is True
    assert bk._is_skeleton("subtract(FIELD, FIELD)") is True
    assert bk._is_skeleton("") is True
    assert bk._is_skeleton("rank(close)") is False
    assert bk._is_skeleton("ts_rank(returns, 20)") is False


def test_row_region_resolution():
    assert bk._row_region({"region": "chn"}) == "CHN"
    assert bk._row_region({"regions": ["EUR", "USA"]}) == "EUR"
    assert bk._row_region({}) == "USA"  # default
    assert bk._row_region({"region": None, "regions": []}) == "USA"


# ---------------------------------------------------------------------------
# resolver against the REAL datafields catalog (PG)
# ---------------------------------------------------------------------------

def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pg_only = pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on localhost:5433")


@pytest_asyncio.fixture
async def pg_session() -> AsyncGenerator[AsyncSession, None]:
    from backend.config import settings
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
    finally:
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_resolve_real_field_to_its_category(pg_session):
    """A field_id that exists in the USA datafields catalog must resolve to its
    canonical category (self-validating against whatever the catalog holds)."""
    from backend.models.metadata import DataField
    from backend.agents.services.rag_service import resolve_field_categories, _canonical_category

    row = (await pg_session.execute(
        select(DataField.field_id, DataField.category)
        .where(DataField.region == "USA", DataField.category.isnot(None))
        .limit(1)
    )).first()
    if not row:
        pytest.skip("no USA datafields rows to validate against")
    field_id, category = row
    cats = await resolve_field_categories([field_id], "USA", pg_session)
    assert _canonical_category(category) in cats


@pg_only
@pytest.mark.asyncio
async def test_resolve_non_usa_region_returns_empty(pg_session):
    """datafields is USA-only — an unknown region resolves to [] (graceful gap)."""
    from backend.agents.services.rag_service import resolve_field_categories
    cats = await resolve_field_categories("rank(close)", f"ZZ_{uuid.uuid4().hex[:6]}", pg_session)
    assert cats == []


@pg_only
@pytest.mark.asyncio
async def test_noisy_fields_used_tokens_self_filter(pg_session):
    """P0.5 safety: the new own-`fields_used` tier feeds legacy failure-row token
    lists straight to the resolver. Many such lists are description WORDS, not real
    fields (observed live: ['based','of','ranks','transformations', ...]). The
    resolver must keep only tokens matching a real datafields field_id, so pure-noise
    lists resolve to [] — no false categories stamped."""
    from backend.agents.services.rag_service import resolve_field_categories
    noise = ["based", "of", "on", "ranks", "transformations", "identical", "parameters"]
    cats = await resolve_field_categories(noise, "USA", pg_session)
    assert cats == []
