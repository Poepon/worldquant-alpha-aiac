"""derive_parent_alpha_family_id — real-ORM coverage.

Per feedback_orm_constructor_real_test, helpers that touch SQLAlchemy
ORM constructors / queries must have at least one in-memory aiosqlite
test that actually runs the SQL — mock-only suites hide schema typos
+ silently-dropped field assignments.

Topology under test:

    root_a  (parent_alpha_id=None,  family_id=self.id)
      └── child_b (parent_alpha_id=root_a.id, family_id=root_a.id)
            └── grandchild_c (parent_alpha_id=child_b.id, family_id=?)

derive(child_b.id) MUST return root_a.id (parent has family_id set).
derive(root_a.id)  MUST return root_a.id (parent IS root — fallback to
parent.id when parent's family_id is NULL pre-backfill).
"""
from __future__ import annotations

import pytest

from backend.models import Alpha
from backend.services.optimization.family_id import (
    derive_parent_alpha_family_id,
)


async def _add_alpha(
    db_session, *, alpha_id: str, parent_id=None, family_id=None,
) -> int:
    a = Alpha(
        alpha_id=alpha_id,
        expression=f"dummy_{alpha_id}",
        region="USA",
        universe="TOP3000",
        parent_alpha_id=parent_id,
        parent_alpha_family_id=family_id,
    )
    db_session.add(a)
    await db_session.flush()
    return int(a.id)


@pytest.mark.asyncio
async def test_derive_returns_none_when_parent_is_none(db_session):
    assert await derive_parent_alpha_family_id(None, db_session) is None


@pytest.mark.asyncio
async def test_derive_falls_back_to_parent_id_when_family_id_is_null(db_session):
    """Pre-backfill / legacy root row: parent has no family_id → derive
    returns parent's own id (it becomes its own root). Matches the
    WITH RECURSIVE backfill semantics in the migration."""
    root_id = await _add_alpha(
        db_session, alpha_id="root-1", parent_id=None, family_id=None,
    )
    assert await derive_parent_alpha_family_id(root_id, db_session) == root_id


@pytest.mark.asyncio
async def test_derive_returns_parents_family_id(db_session):
    """1-hop: child's family_id = parent's family_id."""
    root_id = await _add_alpha(
        db_session, alpha_id="root-2", parent_id=None, family_id=None,
    )
    # Simulate the post-backfill state where root has family_id = self.id
    root = await db_session.get(Alpha, root_id)
    root.parent_alpha_family_id = root_id
    await db_session.flush()

    child_id = await _add_alpha(
        db_session, alpha_id="child-2",
        parent_id=root_id, family_id=root_id,
    )
    # Now insert a grandchild — its family_id derived from child should
    # equal root_id (because child.family_id = root_id).
    derived = await derive_parent_alpha_family_id(child_id, db_session)
    assert derived == root_id


@pytest.mark.asyncio
async def test_derive_returns_none_when_parent_does_not_exist(db_session):
    """Defensive: caller hands a parent_id that doesn't exist → None
    (would normally be prevented by FK but we don't trust callers)."""
    assert (
        await derive_parent_alpha_family_id(999999, db_session) is None
    )


@pytest.mark.asyncio
async def test_derive_two_hops_resolves_via_parent_family_id(db_session):
    """2-hop chain (grandchild derives from child; child already has
    family_id set to root). Verifies we don't actually walk the chain;
    one DB read is enough because every parent already has family_id
    populated by the time a child is being inserted."""
    root_id = await _add_alpha(
        db_session, alpha_id="root-3", parent_id=None, family_id=None,
    )
    root = await db_session.get(Alpha, root_id)
    root.parent_alpha_family_id = root_id
    await db_session.flush()

    child_id = await _add_alpha(
        db_session, alpha_id="child-3",
        parent_id=root_id, family_id=root_id,
    )
    grandchild_id = await _add_alpha(
        db_session, alpha_id="gc-3",
        parent_id=child_id, family_id=root_id,
    )

    # Hand the grandchild id — its child (if any) should still derive root_id
    derived = await derive_parent_alpha_family_id(grandchild_id, db_session)
    assert derived == root_id
