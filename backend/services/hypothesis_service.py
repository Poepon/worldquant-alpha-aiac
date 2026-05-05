"""Hypothesis Service - Business logic for typed Hypothesis lifecycle.

Plan v5+ §Phase 2 B7: CRUD + lifecycle state machine + stats aggregation
for the typed Hypothesis introduced by B1.

Lifecycle state machine:

    PROPOSED ──first alpha──> ACTIVE ──first PASS──> PROMOTED
        │                       │
        │                       └──abandon criterion──> ABANDONED
        │
        └──supersede──> SUPERSEDED  (replaced by child hypothesis)

Plus an orthogonal `is_active` boolean toggled by monthly regime review
(Plan v5+ Final §简化冷冻) — when False, sampling skips the hypothesis
without changing its lifecycle state.

Stats are denormalized on the hypothesis row (alpha_count / pass_count /
sharpe_avg / sharpe_max) for cheap frontend aggregation, but the source
of truth is alphas.hypothesis_id JOIN — refresh_stats() reconciles them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Alpha,
    Hypothesis,
    HypothesisKind,
    HypothesisStatus,
)
from backend.services.base import BaseService

logger = logging.getLogger("services.hypothesis")


@dataclass
class HypothesisCreateData:
    """Input for create_hypothesis. Mirrors the typed Hypothesis dataclass
    plus operational fields not in the dataclass (region/dataset_pool/
    target_tier/etc)."""

    statement: str
    region: str
    rationale: Optional[str] = None
    universe: Optional[str] = None

    kind: str = HypothesisKind.INVESTMENT_THESIS.value
    target_tier: int = 1

    expected_signal: str = "unknown"
    confidence: str = "medium"
    novelty: str = "established"

    key_fields: Optional[List[str]] = None
    suggested_operators: Optional[List[str]] = None
    dataset_pool: Optional[List[str]] = None

    parent_alpha_id: Optional[int] = None
    parent_hypothesis_id: Optional[int] = None
    experiment_variant: Optional[str] = None


@dataclass
class HypothesisStats:
    """Result of refresh_stats — what got recomputed for one hypothesis."""

    hypothesis_id: int
    alpha_count: int
    pass_count: int
    sharpe_avg: Optional[float]
    sharpe_max: Optional[float]


class HypothesisService(BaseService):
    """CRUD + lifecycle + stats for typed Hypothesis rows."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_hypothesis(self, data: HypothesisCreateData) -> Hypothesis:
        """Insert a new Hypothesis row in PROPOSED state.

        Used by node_hypothesis_propose (Phase 2 B3) which must persist the
        row BEFORE downstream code_gen sees state.current_hypothesis_id —
        the time-ordering hard constraint defends against post-hoc
        rationalization (Plan v5+ §A 4 道 post-hoc 防御).
        """
        h = Hypothesis(
            statement=data.statement,
            rationale=data.rationale,
            kind=data.kind,
            target_tier=data.target_tier,
            expected_signal=data.expected_signal,
            confidence=data.confidence,
            novelty=data.novelty,
            key_fields=data.key_fields or [],
            suggested_operators=data.suggested_operators or [],
            region=data.region,
            universe=data.universe,
            dataset_pool=data.dataset_pool or [],
            parent_alpha_id=data.parent_alpha_id,
            parent_hypothesis_id=data.parent_hypothesis_id,
            experiment_variant=data.experiment_variant,
            status=HypothesisStatus.PROPOSED.value,
            is_active=True,
        )
        self.db.add(h)
        await self.flush()
        await self.refresh(h)
        logger.info(
            f"[hypothesis] created id={h.id} kind={h.kind} tier=T{h.target_tier} "
            f"region={h.region} variant={h.experiment_variant}"
        )
        return h

    async def get_by_id(self, hypothesis_id: int) -> Optional[Hypothesis]:
        return await super().get_by_id(Hypothesis, hypothesis_id)

    async def list_active(
        self,
        region: str,
        *,
        kind: Optional[str] = None,
        target_tier: Optional[int] = None,
        experiment_variant: Optional[str] = None,
        include_proposed: bool = True,
        limit: int = 50,
    ) -> List[Hypothesis]:
        """Active hypotheses available for sampling. Excludes ABANDONED /
        SUPERSEDED and rows where is_active=False (regime-frozen).

        include_proposed=True is the normal sampling path (PROPOSED hypotheses
        haven't been tested yet but are still candidates). False excludes them
        — useful for "give me hypotheses that already produced ≥1 alpha"
        queries.
        """
        valid_states = (
            [HypothesisStatus.PROPOSED.value, HypothesisStatus.ACTIVE.value, HypothesisStatus.PROMOTED.value]
            if include_proposed
            else [HypothesisStatus.ACTIVE.value, HypothesisStatus.PROMOTED.value]
        )
        stmt = (
            select(Hypothesis)
            .where(
                Hypothesis.region == region,
                Hypothesis.is_active.is_(True),
                Hypothesis.status.in_(valid_states),
            )
            .order_by(Hypothesis.created_at.desc())
            .limit(limit)
        )
        if kind is not None:
            stmt = stmt.where(Hypothesis.kind == kind)
        if target_tier is not None:
            stmt = stmt.where(Hypothesis.target_tier == target_tier)
        if experiment_variant is not None:
            stmt = stmt.where(Hypothesis.experiment_variant == experiment_variant)

        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    async def mark_active(self, hypothesis_id: int) -> bool:
        """PROPOSED → ACTIVE. Idempotent: no-op if already ACTIVE/PROMOTED.

        Called once from B5 feedback when the first alpha lands under this
        hypothesis (regardless of PASS/FAIL). Distinguishes "tried but no
        signal" from "never tried".
        """
        stmt = (
            update(Hypothesis)
            .where(
                Hypothesis.id == hypothesis_id,
                Hypothesis.status == HypothesisStatus.PROPOSED.value,
            )
            .values(status=HypothesisStatus.ACTIVE.value)
        )
        result = await self.db.execute(stmt)
        return (result.rowcount or 0) > 0

    async def mark_promoted(self, hypothesis_id: int) -> bool:
        """ACTIVE/PROPOSED → PROMOTED. Promoted = produced ≥1 PASS alpha;
        kept indefinitely for KB even after the task ends."""
        stmt = (
            update(Hypothesis)
            .where(
                Hypothesis.id == hypothesis_id,
                Hypothesis.status.in_([
                    HypothesisStatus.PROPOSED.value,
                    HypothesisStatus.ACTIVE.value,
                ]),
            )
            .values(status=HypothesisStatus.PROMOTED.value)
        )
        result = await self.db.execute(stmt)
        return (result.rowcount or 0) > 0

    async def mark_abandoned(
        self, hypothesis_id: int, reason: str,
    ) -> bool:
        """→ ABANDONED. Triggered by should_abandon_hypothesis (Plan §B6) —
        N rounds with 0 PASS and HYPOTHESIS-attribution feedback. Once
        abandoned, the hypothesis is excluded from future sampling regardless
        of is_active."""
        if not reason:
            raise ValueError("abandon_reason required — empty rejection masks debugging")
        stmt = (
            update(Hypothesis)
            .where(
                Hypothesis.id == hypothesis_id,
                # Allow re-abandon (idempotent reason update)
                Hypothesis.status != HypothesisStatus.SUPERSEDED.value,
            )
            .values(
                status=HypothesisStatus.ABANDONED.value,
                abandon_reason=reason[:1000],  # text column but bound length
            )
        )
        result = await self.db.execute(stmt)
        if (result.rowcount or 0) > 0:
            logger.warning(
                f"[hypothesis] abandoned id={hypothesis_id} reason={reason[:80]!r}"
            )
            return True
        return False

    async def mark_superseded(
        self, hypothesis_id: int, child_hypothesis_id: int,
    ) -> bool:
        """Hypothesis replaced by a refined child (Plan v5+ §B5 refine
        action). Records the lineage via child.parent_hypothesis_id and
        flips parent.status to SUPERSEDED."""
        # Verify child references the parent
        child = await self.get_by_id(child_hypothesis_id)
        if child is None:
            raise ValueError(f"child hypothesis {child_hypothesis_id} not found")
        if child.parent_hypothesis_id != hypothesis_id:
            raise ValueError(
                f"child {child_hypothesis_id} does not reference parent "
                f"{hypothesis_id} (has parent_hypothesis_id={child.parent_hypothesis_id})"
            )
        stmt = (
            update(Hypothesis)
            .where(Hypothesis.id == hypothesis_id)
            .values(status=HypothesisStatus.SUPERSEDED.value)
        )
        result = await self.db.execute(stmt)
        return (result.rowcount or 0) > 0

    async def set_active_flag(
        self, hypothesis_id: int, is_active: bool, reason: Optional[str] = None,
    ) -> bool:
        """Toggle the is_active boolean for regime-triggered freeze (Plan
        v5+ Final §简化冷冻). Does NOT change `status` — a frozen hypothesis
        is still PROMOTED/ACTIVE, just temporarily skipped by sampling."""
        values: Dict[str, Any] = {"is_active": is_active}
        if reason:
            existing = await self.get_by_id(hypothesis_id)
            if existing:
                tag = "[regime-freeze] " if not is_active else "[regime-unfreeze] "
                values["abandon_reason"] = (
                    tag + reason[:900]
                    if not existing.abandon_reason
                    else (existing.abandon_reason + " | " + tag + reason[:500])[:1000]
                )
        stmt = (
            update(Hypothesis)
            .where(Hypothesis.id == hypothesis_id)
            .values(**values)
        )
        result = await self.db.execute(stmt)
        if (result.rowcount or 0) > 0:
            logger.info(
                f"[hypothesis] set_active id={hypothesis_id} is_active={is_active} "
                f"reason={reason}"
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Stats aggregation
    # ------------------------------------------------------------------

    async def refresh_stats(self, hypothesis_id: int) -> HypothesisStats:
        """Recompute aggregated stats from the alphas JOIN. Source of truth
        is alphas.hypothesis_id; this method updates the denormalized cols.

        Called from:
          - B5 feedback (after a round's alphas land)
          - B7 batch refresh_all_stats (periodic reconcile)
          - Frontend stats endpoint when stale
        """
        stmt = (
            select(
                func.count(Alpha.id).label("alpha_count"),
                func.count(
                    case((Alpha.quality_status.in_(["PASS", "PASS_PROVISIONAL"]), 1))
                ).label("pass_count"),
                func.avg(Alpha.is_sharpe).label("sharpe_avg"),
                func.max(Alpha.is_sharpe).label("sharpe_max"),
            )
            .where(Alpha.hypothesis_id == hypothesis_id)
        )
        result = await self.db.execute(stmt)
        row = result.one()
        alpha_count = int(row.alpha_count or 0)
        pass_count = int(row.pass_count or 0)
        sharpe_avg = float(row.sharpe_avg) if row.sharpe_avg is not None else None
        sharpe_max = float(row.sharpe_max) if row.sharpe_max is not None else None

        await self.db.execute(
            update(Hypothesis)
            .where(Hypothesis.id == hypothesis_id)
            .values(
                alpha_count=alpha_count,
                pass_count=pass_count,
                sharpe_avg=sharpe_avg,
                sharpe_max=sharpe_max,
            )
        )
        return HypothesisStats(
            hypothesis_id=hypothesis_id,
            alpha_count=alpha_count,
            pass_count=pass_count,
            sharpe_avg=sharpe_avg,
            sharpe_max=sharpe_max,
        )

    async def refresh_all_stats(
        self, *, only_active: bool = True, batch_size: int = 100,
    ) -> int:
        """Batch refresh: re-aggregate for every Hypothesis. Returns count
        refreshed. only_active=True (default) skips ABANDONED/SUPERSEDED
        which won't change stats anymore."""
        stmt = select(Hypothesis.id)
        if only_active:
            stmt = stmt.where(
                Hypothesis.status.in_([
                    HypothesisStatus.PROPOSED.value,
                    HypothesisStatus.ACTIVE.value,
                    HypothesisStatus.PROMOTED.value,
                ])
            )
        result = await self.db.execute(stmt)
        ids = [row[0] for row in result.fetchall()]
        for hid in ids:
            await self.refresh_stats(hid)
        return len(ids)

    # ------------------------------------------------------------------
    # Helper queries
    # ------------------------------------------------------------------

    async def pass_rate(self, hypothesis_id: int) -> Optional[float]:
        """alpha_count == 0 ? None : pass_count / alpha_count. None means
        'no alphas yet' — callers must distinguish that from 0.0 rate."""
        h = await self.get_by_id(hypothesis_id)
        if h is None or h.alpha_count == 0:
            return None
        return h.pass_count / h.alpha_count

    async def auto_promote_if_eligible(self, hypothesis_id: int) -> bool:
        """If the hypothesis has ≥1 PASS alpha and is currently PROPOSED or
        ACTIVE, transition to PROMOTED. Convenience wrapper around the PASS-
        gate check that B5 feedback uses."""
        stats = await self.refresh_stats(hypothesis_id)
        if stats.pass_count > 0:
            return await self.mark_promoted(hypothesis_id)
        return False

    async def auto_activate_if_eligible(self, hypothesis_id: int) -> bool:
        """PROPOSED → ACTIVE on first alpha (any status). Convenience for B5."""
        stats = await self.refresh_stats(hypothesis_id)
        if stats.alpha_count > 0:
            return await self.mark_active(hypothesis_id)
        return False
