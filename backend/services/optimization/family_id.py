"""Derive ``parent_alpha_family_id`` for a new alpha row.

The family_id points at the root of the parent chain. Root rows
(parent_alpha_id IS NULL) have family_id = self.id (written post-INSERT).
Descendants have family_id = parent.family_id (one DB hop — every parent
already has its family_id set, so this is O(1) per call).

Used by Persister before INSERT so optimization-derived alphas are
deduplicatable per family across cycles. Backfill of the historical
``alphas`` table runs once in the Phase 16-A Alembic migration via
WITH RECURSIVE.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §5.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Alpha


async def derive_parent_alpha_family_id(
    parent_alpha_id: Optional[int], db: AsyncSession
) -> Optional[int]:
    """Return the family_id for a new alpha whose parent is ``parent_alpha_id``.

    - ``parent_alpha_id IS None`` → returns None (caller will treat the new
      row itself as a root and set family_id = self.id post-INSERT if
      desired; optimization-created alphas always HAVE a parent).
    - Parent has family_id set → returns that value.
    - Parent has family_id NULL (legacy row pre-backfill) → returns parent's
      own id (it becomes its own family root). This matches the WITH
      RECURSIVE backfill semantics so the migration + new INSERTs converge.
    """
    if parent_alpha_id is None:
        return None
    row = (
        await db.execute(
            select(Alpha.parent_alpha_family_id, Alpha.id).where(
                Alpha.id == parent_alpha_id
            )
        )
    ).first()
    if row is None:
        return None  # parent doesn't exist (shouldn't happen via FK; defensive)
    family_id, parent_id = row
    return int(family_id) if family_id is not None else int(parent_id)
