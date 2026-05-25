"""P2-A MacroNarrativeService integration tests (PG-only, 2026-05-16).

Mirrors backend/tests/integration/test_negative_knowledge_service.py: PG
required because the queries use JSONB operators (``->>``, ANY) that
aiosqlite can't evaluate.

M3 fix: ``_pg_reachable`` defined INLINE in this module (NOT in conftest)
to match the rest of the integration suite. ``pytestmark`` set at module
top so any new test in this file automatically picks up the skip.

6 cases:
  - S1 upsert_seed_narratives idempotent over 2 runs
  - S2 fetch_macro_narratives field-level matches (region wildcard '*')
  - S3 fetch_macro_narratives dataset-level matches against BRAIN string id
  - S4 fetch_macro_narratives category-only fallback (no field/dataset)
  - S5 fetch_macro_narratives global confidence DESC + field +0.1 bonus
  - S6 list_fields_missing_narrative uses DataField → DatasetMetadata JOIN
"""
from __future__ import annotations

import os
import socket
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")


# M3: inline _pg_reachable + pytestmark (NO conftest)
def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="P2-A macro-narrative tests require Postgres on localhost:5433",
)


# Warm-up: see test_negative_knowledge_service.py for the cycle note.
import backend.tasks  # noqa: E402, F401

from backend.macro_narratives import (  # noqa: E402
    MacroNarrative,
    compute_narrative_hash,
    narrative_to_kb_payload,
)
from backend.models import (  # noqa: E402
    DataField,
    DataFieldCellStats,
    DatasetMetadata,
    KnowledgeEntry,
)
from backend.services.macro_narrative_service import (  # noqa: E402
    MacroNarrativeService,
)


_TAG = f"mnT{uuid.uuid4().hex[:3]}_"


@pytest_asyncio.fixture
async def pg_session():
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                # Drop any rows tagged with our run-prefix
                await s.execute(
                    text(
                        "DELETE FROM knowledge_entries WHERE "
                        "entry_type='MACRO_NARRATIVE' "
                        "AND (created_by = 'P2A_MACRO' "
                        "     OR meta_data->>'field_id' ILIKE :p "
                        "     OR meta_data->>'dataset_id' ILIKE :p "
                        "     OR meta_data->>'dataset_category' ILIKE :p)"
                    ),
                    {"p": f"%{_TAG}%"},
                )
                # Cleanup tagged DataFields/Datasets seeded for S6 — delete the
                # cell_stats children first (FK to datafields/datasets).
                await s.execute(
                    delete(DataFieldCellStats).where(
                        DataFieldCellStats.datafield_ref.in_(
                            select(DataField.id).where(DataField.field_id.like(f"{_TAG}%"))
                        )
                    )
                )
                await s.execute(
                    delete(DataField).where(
                        DataField.field_id.like(f"{_TAG}%"),
                    )
                )
                await s.execute(
                    delete(DatasetMetadata).where(
                        DatasetMetadata.dataset_id.like(f"{_TAG}%"),
                    )
                )
                # Defensive: also clear ALL MACRO_NARRATIVE seed rows
                # (idempotent — seed upsert re-creates them next run).
                await s.execute(
                    text(
                        "DELETE FROM knowledge_entries "
                        "WHERE entry_type='MACRO_NARRATIVE' "
                        "AND created_by='P2A_MACRO'"
                    )
                )
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _insert_narrative(s, *, field_id=None, dataset_id=None,
                            dataset_category=None, region="*", source="seed",
                            confidence=0.7, mechanism="X", transmission="Y",
                            hint="momentum"):
    """Direct INSERT bypassing the service so we can stage rows for fetch
    tests without depending on the upsert path."""
    n = MacroNarrative(
        field_id=field_id, dataset_id=dataset_id,
        dataset_category=dataset_category, region=region,
        mechanism=mechanism, transmission_channel=transmission,
        expected_signal_hint=hint, confidence=confidence, source=source,
    )
    payload = narrative_to_kb_payload(n)
    row = KnowledgeEntry(
        entry_type=payload["entry_type"],
        pattern=payload["pattern"],
        pattern_hash=payload["pattern_hash"],
        description=payload["description"],
        meta_data=payload["meta_data"],
        is_active=True,
        created_by=payload["created_by"],
        usage_count=0,
    )
    s.add(row)
    await s.commit()
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestUpsertSeedNarratives:

    @pytest.mark.asyncio
    async def test_seed_upsert_idempotent(self, pg_session):
        """S1: two consecutive seed upserts → first inserts ~11 rows,
        second produces zero ``new`` (all updated/skipped). KB count
        stable across re-runs."""
        svc = MacroNarrativeService(pg_session)
        counters_1 = await svc.upsert_seed_narratives()
        assert counters_1["errors"] == 0, counters_1
        new1 = counters_1["new"]
        assert new1 >= 11, (
            f"first seed run should insert ≥11 rows, got {new1}: {counters_1}"
        )

        # Verify rows present
        n_rows = (await pg_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.entry_type == "MACRO_NARRATIVE",
                KnowledgeEntry.created_by == "P2A_MACRO",
            )
        )).scalars().all()
        assert len(n_rows) == new1

        # Second pass — must be idempotent (no ``new``)
        counters_2 = await svc.upsert_seed_narratives()
        assert counters_2["new"] == 0, (
            f"seed re-run should not insert new rows: {counters_2}"
        )
        assert counters_2["errors"] == 0


class TestFetchMacroNarratives:

    @pytest.mark.asyncio
    async def test_fetch_field_level_with_region_wildcard(self, pg_session):
        """S2: field-level row with region='*' matches a USA query."""
        fid = f"{_TAG}fld_a"
        await _insert_narrative(
            pg_session, field_id=fid, region="*", source="seed",
            confidence=0.88, mechanism=f"MECH_{_TAG}A",
        )
        svc = MacroNarrativeService(pg_session)
        out = await svc.fetch_macro_narratives(
            dataset_id=None, region="USA", key_fields=[fid],
        )
        assert len(out) >= 1
        assert any(r.get("field_id") == fid for r in out)
        match = [r for r in out if r.get("field_id") == fid][0]
        assert match.get("mechanism") == f"MECH_{_TAG}A"
        assert match.get("scope") == "field"

    @pytest.mark.asyncio
    async def test_fetch_dataset_level_brain_string_id(self, pg_session):
        """S3: dataset-level row stored with BRAIN string id matches
        on the same string passed at fetch time."""
        brain_id = f"{_TAG}ds_brain1"
        await _insert_narrative(
            pg_session, dataset_id=brain_id, region="USA", source="seed",
            confidence=0.92, mechanism=f"MECH_{_TAG}DS",
        )
        svc = MacroNarrativeService(pg_session)
        out = await svc.fetch_macro_narratives(
            dataset_id=brain_id, region="USA", key_fields=None,
        )
        assert any(r.get("dataset_id") == brain_id for r in out), (
            f"dataset_id {brain_id} not found in fetch result: {out}"
        )

    @pytest.mark.asyncio
    async def test_fetch_category_fallback(self, pg_session):
        """S4: when neither field nor dataset matches, infer the category
        from dataset_id and return the category-scope row."""
        # Use BRAIN-style dataset_id that infers to 'pv'
        brain_id = f"{_TAG}pv_only"
        # Seed a category-scope row for 'pv' (the keyword in brain_id triggers
        # infer_dataset_category → 'pv').
        await _insert_narrative(
            pg_session, dataset_category="pv", region="*", source="seed",
            confidence=0.7, mechanism=f"PV_CATSCOPE_{_TAG}",
        )
        svc = MacroNarrativeService(pg_session)
        out = await svc.fetch_macro_narratives(
            dataset_id=brain_id, region="USA", key_fields=None,
        )
        cat_hits = [r for r in out if r.get("scope") == "category"
                    and r.get("dataset_category") == "pv"]
        assert cat_hits, (
            f"category-scope fallback failed for dataset {brain_id}: {out}"
        )

    @pytest.mark.asyncio
    async def test_global_confidence_with_field_bonus(self, pg_session):
        """S5: a low-confidence field row (0.5) gets +0.1 bonus = 0.6 and
        beats a 0.55-confidence dataset row in the merged ranking.
        Verifies the S4-fix Python-side sort.
        """
        fid = f"{_TAG}lowfld"
        ds = f"{_TAG}pv_mediumds"
        # Field-scope: 0.5 base → 0.6 with bonus
        await _insert_narrative(
            pg_session, field_id=fid, region="*", source="llm",
            confidence=0.5, mechanism=f"LOWFLD_{_TAG}",
        )
        # Dataset-scope: 0.55 base
        await _insert_narrative(
            pg_session, dataset_id=ds, region="*", source="seed",
            confidence=0.55, mechanism=f"MEDIUMDS_{_TAG}",
        )
        svc = MacroNarrativeService(pg_session)
        out = await svc.fetch_macro_narratives(
            dataset_id=ds, region="USA", key_fields=[fid],
        )
        assert len(out) >= 2
        # Field-bonused row must appear before the dataset row
        idx_field = next(
            (i for i, r in enumerate(out) if r.get("field_id") == fid), None,
        )
        idx_ds = next(
            (i for i, r in enumerate(out) if r.get("dataset_id") == ds), None,
        )
        assert idx_field is not None and idx_ds is not None, out
        assert idx_field < idx_ds, (
            f"S4 ranking violated: field row (idx={idx_field}, conf=0.5+0.1) "
            f"did NOT outrank dataset row (idx={idx_ds}, conf=0.55)"
        )


class TestListFieldsMissingNarrative:

    @pytest.mark.asyncio
    async def test_list_fields_missing_uses_join_to_datasetmeta(
        self, pg_session,
    ):
        """S6 / M1+M2: list_fields_missing_narrative MUST return rows
        whose ``dataset_id`` is the BRAIN string id from
        ``DatasetMetadata.dataset_id`` — NOT the Integer FK in
        ``DataField.dataset_id``. Pre-fix this would have crashed at
        infer_dataset_category(int)."""
        # Seed a DatasetMetadata row with a BRAIN-style string id
        ds_brain_id = f"{_TAG}pv_join"
        ds_row = DatasetMetadata(
            dataset_id=ds_brain_id,
            region="USA",
            name=f"{_TAG} test dataset",
            description="x",
            category="Price/Volume",
        )
        pg_session.add(ds_row)
        await pg_session.commit()
        await pg_session.refresh(ds_row)

        # Seed a DataField def whose FK points at the just-created Dataset, plus
        # its TOP3000/delay=1 cell (is_active lives on datafield_cell_stats now).
        fid = f"{_TAG}new_field"
        df_row = DataField(
            dataset_id=ds_row.id,  # Integer FK
            field_id=fid,
            field_name=f"{_TAG} new",
            field_type="MATRIX",
            description="a field with no narrative yet",
        )
        pg_session.add(df_row)
        await pg_session.flush()
        pg_session.add(DataFieldCellStats(
            datafield_ref=df_row.id, universe="TOP3000", delay=1, is_active=True,
        ))
        await pg_session.commit()

        svc = MacroNarrativeService(pg_session)
        # Use a generous limit — production DB has thousands of DataFields and
        # alphabetical sort can push our _TAG-prefixed test row past a small
        # limit. The point is to verify the JOIN works, not the LIMIT bound.
        rows = await svc.list_fields_missing_narrative(region="USA", limit=50000)
        match = [r for r in rows if r.get("field_id") == fid]
        assert match, (
            f"field {fid} (no MACRO_NARRATIVE seeded for it) not surfaced "
            f"by list_fields_missing_narrative — JOIN broken? rows[:5]={rows[:5]}"
        )
        row = match[0]
        # M1/M2 critical: dataset_id is the BRAIN STRING id, NOT the int FK
        assert row["dataset_id"] == ds_brain_id, (
            f"M1/M2 violated: expected BRAIN string id {ds_brain_id!r}, "
            f"got {row['dataset_id']!r}"
        )
        # infer_category should produce 'pv' for our id
        assert row["dataset_category_inferred"] == "pv", (
            f"inferred category wrong: {row['dataset_category_inferred']!r}"
        )
