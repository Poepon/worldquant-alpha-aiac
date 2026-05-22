"""Tests for sync_fields_from_brain is_active reconciliation (2026-05-22).

User directive: when syncing fields from BRAIN, a field BRAIN returns →
is_active=True (re-activate; BRAIN is the source of truth for what's valid).
Fields BRAIN no longer returns KEEP their current status — deactivation is
delegated to the mining-driven prune (it deactivates only fields BRAIN rejects
at SIMULATE time). So sync never wipes a dataset / never deactivates on absence.
"""
import pytest
from sqlalchemy import select

from backend.tasks.sync_tasks import _reconcile_dataset_fields


async def _setup(db):
    from backend.models import DataField, DatasetMetadata
    ds = DatasetMetadata(dataset_id="pv1", region="USA", universe="TOP3000", name="pv1")
    db.add(ds)
    await db.flush()

    def mk(fid, active):
        return DataField(dataset_id=ds.id, region="USA", universe="TOP3000", delay=1,
                         field_id=fid, field_name=fid, field_type="MATRIX", is_active=active)
    db.add_all([
        mk("f_keep", True),      # active, BRAIN returns  → stays active
        mk("f_stale", True),     # active, BRAIN DROPS    → STAYS active (prune's job)
        mk("f_revive", False),   # inactive, BRAIN returns→ re-activate
        mk("f_dormant", False),  # inactive, BRAIN DROPS  → stays inactive
    ])
    await db.flush()
    return ds


def _bf(fid):
    return {"id": fid, "name": fid, "type": "MATRIX"}


async def _states(db):
    from backend.models import DataField
    return {r.field_id: r.is_active
            for r in (await db.execute(select(DataField))).scalars().all()}


@pytest.mark.asyncio
async def test_sync_activates_present_preserves_missing(db_session):
    ds = await _setup(db_session)
    # BRAIN returns f_keep + f_revive + a brand-new field; NOT f_stale/f_dormant.
    stats = await _reconcile_dataset_fields(
        db_session, ds, [_bf("f_keep"), _bf("f_revive"), _bf("f_new")],
        region="USA", universe="TOP3000", delay=1,
    )
    await db_session.commit()

    st = await _states(db_session)
    assert st["f_keep"] is True
    assert st["f_revive"] is True       # re-activated (BRAIN returns it)
    assert st["f_new"] is True          # newly inserted, active
    assert st["f_stale"] is True        # PRESERVED — sync must NOT deactivate
    assert st["f_dormant"] is False     # PRESERVED — left as-is
    assert stats["new"] == 1 and stats["updated"] == 2 and stats["returned"] == 3
    assert "deactivated" not in stats   # sync no longer deactivates


@pytest.mark.asyncio
async def test_empty_response_changes_nothing(db_session):
    ds = await _setup(db_session)
    stats = await _reconcile_dataset_fields(
        db_session, ds, [], region="USA", universe="TOP3000", delay=1,
    )
    await db_session.commit()

    st = await _states(db_session)
    # Every field keeps its prior status — no activation, no deactivation.
    assert st == {"f_keep": True, "f_stale": True, "f_revive": False, "f_dormant": False}
    assert stats["new"] == 0 and stats["updated"] == 0 and stats["returned"] == 0
