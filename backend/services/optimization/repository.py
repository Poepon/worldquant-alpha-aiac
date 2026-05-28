"""OptimizationRunRepository implementation — cycle lifecycle persistence.

Mirrors the protocol in :mod:`backend.services.optimization.protocols`.
Splits the lifecycle into four explicit calls so the orchestrator never
has to know about ``OptimizationRun`` column names directly.

All writes are scoped to whatever ``AsyncSession`` is injected — callers
own the commit boundary so a cycle that fails mid-way doesn't leave a
half-finished row visible to telemetry. Stage A's beat task wraps each
cycle in a fresh session.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import OptimizationRun


class OptimizationRunRepositoryImpl:
    """Concrete repo for ``optimization_runs``."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def open_cycle(
        self,
        parent_alpha_id: int,
        generator_name: str,
        trigger_source: str,
        sim_budget_granted: int,
    ) -> int:
        row = OptimizationRun(
            parent_alpha_id=int(parent_alpha_id),
            generator_name=str(generator_name),
            trigger_source=str(trigger_source),
            sim_budget_granted=int(sim_budget_granted),
        )
        self.db.add(row)
        await self.db.flush()  # populate row.id without committing
        return int(row.id)

    async def record_persist(
        self,
        opt_run_id: int,
        n_variants: int,
        n_winners: int,
        sim_spent: int,
    ) -> None:
        row = await self._load(opt_run_id)
        row.n_variants = int(n_variants)
        row.n_winners = int(n_winners)
        row.sim_budget_used = int(sim_spent)
        await self.db.flush()

    async def record_submit(self, opt_run_id: int, n_submitted: int) -> None:
        row = await self._load(opt_run_id)
        row.n_submitted = int(n_submitted)
        await self.db.flush()

    async def finish_cycle(
        self, opt_run_id: int, error: Optional[str] = None
    ) -> None:
        row = await self._load(opt_run_id)
        row.cycle_finished_at = datetime.utcnow()
        if error:
            row.error = str(error)
        await self.db.flush()

    async def _load(self, opt_run_id: int) -> OptimizationRun:
        result = await self.db.execute(
            select(OptimizationRun).where(OptimizationRun.id == int(opt_run_id))
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise LookupError(f"OptimizationRun {opt_run_id} not found")
        return row
