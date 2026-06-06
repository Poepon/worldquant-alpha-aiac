"""Real-ORM tests for the datasets/datafields cell-stats normalization
(2026-05-26, plan golden-forging-taco §Phase 1).

Per [[feedback_orm_constructor_real_test]] the join-bearing read paths must be
exercised against a real (aiosqlite) session, not mocked — the def↔cell join +
per-cell is_active + the any-universe attribution fix are the bug-prone parts.

The PG migration round-trip is covered separately by
``scripts/_verify_cell_stats_migration.py`` (scratch-DB upgrade→downgrade).
"""
import pytest

from backend.dataset_attribution import _clear_cache, build_field_dataset_map


async def _mk_dataset_with_fields(db, *, dataset_id, region, cells):
    """Create a dataset def + field defs + per-cell stats.

    ``cells`` = {universe: [(field_id, is_active), ...]}. Each field def is
    created once (universe-invariant); a datafield_cell_stats row per universe.
    Returns the dataset def id.
    """
    from backend.models import (
        DatasetMetadata, DatasetCellStats, DataField, DataFieldCellStats,
    )
    ds = DatasetMetadata(dataset_id=dataset_id, region=region, name=dataset_id)
    db.add(ds)
    await db.flush()
    # one dataset cell per universe
    for universe in cells:
        db.add(DatasetCellStats(
            dataset_ref=ds.id, universe=universe, delay=1, field_count=len(cells[universe]),
            mining_weight=1.0, is_active=True,
        ))
    # field defs (dedup by field_id across universes) + per-cell stats
    defs = {}
    for universe, fields in cells.items():
        for fid, active in fields:
            if fid not in defs:
                df = DataField(dataset_id=ds.id, field_id=fid, field_name=fid.upper(),
                               field_type="MATRIX", description=f"{fid} desc")
                db.add(df)
                await db.flush()
                defs[fid] = df.id
            db.add(DataFieldCellStats(
                datafield_ref=defs[fid], universe=universe, delay=1, is_active=active,
                coverage=0.9,
            ))
    await db.commit()
    return ds.id


@pytest.mark.asyncio
class TestGetDatasetFieldsCellJoin:
    async def test_returns_active_cell_fields_with_dict_shape(self, db_session):
        from backend.tasks.fetch_helpers import _get_dataset_fields
        await _mk_dataset_with_fields(
            db_session, dataset_id="pv1", region="USA",
            cells={"TOP3000": [("close", True), ("volume", False)]},
        )
        out = await _get_dataset_fields(db_session, "pv1", "USA", "TOP3000")
        # volume's TOP3000 cell is inactive → excluded; close included.
        assert [f["id"] for f in out] == ["close"]
        # dict shape preserved (validator/prompt depend on exactly these keys).
        assert out[0] == {"id": "close", "name": "CLOSE", "description": "close desc", "type": "MATRIX"}

    async def test_per_universe_is_active_independent(self, db_session):
        # Same field, active in TOP1000 but inactive in TOP3000 → universe-scoped.
        from backend.tasks.fetch_helpers import _get_dataset_fields
        await _mk_dataset_with_fields(
            db_session, dataset_id="pv1", region="USA",
            cells={
                "TOP3000": [("close", False)],
                "TOP1000": [("close", True)],
            },
        )
        assert await _get_dataset_fields(db_session, "pv1", "USA", "TOP3000") == []
        out = await _get_dataset_fields(db_session, "pv1", "USA", "TOP1000")
        assert [f["id"] for f in out] == ["close"]

    async def test_unsynced_universe_returns_empty(self, db_session):
        # A universe with no datafield cells → no fields (graceful, not a crash).
        from backend.tasks.fetch_helpers import _get_dataset_fields
        await _mk_dataset_with_fields(
            db_session, dataset_id="pv1", region="USA",
            cells={"TOP3000": [("close", True)]},
        )
        assert await _get_dataset_fields(db_session, "pv1", "USA", "TOPSP500") == []


@pytest.mark.asyncio
class TestAttributionAnyUniverse:
    async def test_field_dataset_map_region_only_fix(self, db_session):
        """The latent-bug fix: build_field_dataset_map keys by region ALONE now,
        so attribution works for ANY universe (pre-refactor a non-TOP3000 universe
        found no datasets rows → empty map → NULL alpha.dataset_id → bandit blind)."""
        _clear_cache()
        await _mk_dataset_with_fields(
            db_session, dataset_id="analyst4", region="USA",
            cells={"TOPSP500": [("anl4_ni", True)]},
        )
        # Query with a universe the dataset was synced under AND a different one —
        # both resolve, because the def is universe-invariant.
        for universe in ("TOPSP500", "TOP3000", "WHATEVER"):
            _clear_cache()
            m = await build_field_dataset_map(db_session, "USA", universe)
            assert m.get("anl4_ni") == "analyst4", universe


@pytest.mark.asyncio
async def test_four_table_unique_constraints():
    """The def + cell_stats UKs are wired as designed."""
    from backend.models import (
        DatasetMetadata, DatasetCellStats, DataField, DataFieldCellStats,
    )

    def _uk(model):
        return {
            tuple(c.name for c in con.columns)
            for con in model.__table__.constraints
            if con.__class__.__name__ == "UniqueConstraint"
        }

    assert ("dataset_id", "region") in _uk(DatasetMetadata)
    assert ("dataset_ref", "delay", "universe") in _uk(DatasetCellStats)
    assert ("dataset_id", "field_id") in _uk(DataField)
    assert ("datafield_ref", "delay", "universe") in _uk(DataFieldCellStats)
