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

import json
import logging
from dataclasses import dataclass, asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import select, update, func, case
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.models import (
    Alpha,
    AlphaFailure,
    Hypothesis,
    HypothesisKind,
    HypothesisStatus,
)
from backend.services.base import BaseService

if TYPE_CHECKING:  # avoid runtime circular import
    from backend.services.hypothesis_health_service import LLMThesisScore

logger = logging.getLogger("services.hypothesis")


def _safe_avg_num(v) -> Optional[float]:
    """Stripped-down `_safe_num` for the baseline-stamp helper.

    Avoids importing alpha_health_service here (which would create a
    cycle hypothesis_service → alpha_health_service → tests of
    hypothesis_service). Mirrors the same NaN/inf/bool rejection.
    """
    import math
    if v is None or isinstance(v, bool):
        return None
    if not isinstance(v, (int, float)):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return float(v)


def _hit_to_dict(hit) -> Dict[str, Any]:
    """Coerce a TriggerHit dataclass / plain dict into a JSONB-safe dict.

    Used by ``mark_triggered`` so callers can pass either dataclass
    instances (the production path) or plain dicts (test seeding).
    """
    if is_dataclass(hit):
        return asdict(hit)
    if isinstance(hit, dict):
        return dict(hit)
    raise TypeError(f"trigger hit must be dataclass or dict, got {type(hit)!r}")


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
    """Result of refresh_stats — what got recomputed for one hypothesis.

    V-26.13 (2026-05-13): `alpha_count` now counts attempts across both the
    `alphas` table (PASS / PASS_PROVISIONAL / REJECTED) AND `alpha_failures`
    (validation / sim errors). Pre-fix only the alphas table was counted,
    which left a hypothesis with 50 failed-validation attempts at
    alpha_count=0 — auto_activate_if_eligible never fired and B6 abandon
    could not trigger. `fail_count` is the alpha_failures subset; surfaced
    in the dataclass so callers (e.g. should_abandon_hypothesis) can
    distinguish "no evidence" from "tried but failed". Not persisted yet —
    schema migration would be needed.
    """

    hypothesis_id: int
    alpha_count: int
    pass_count: int
    sharpe_avg: Optional[float]
    sharpe_max: Optional[float]
    fail_count: int = 0  # subset of alpha_count, from alpha_failures


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
        # V-26.45 (2026-05-13): order so PROPOSED-but-untouched hypotheses
        # win over already-tested ones. Pre-fix used `created_at desc`
        # alone, which meant a freshly-PROPOSED hypothesis from yesterday
        # could be starved out of the top-50 window by today's new ones
        # — and a never-tried PROPOSED row in the older tail was unlikely
        # to ever be sampled. Two-key sort:
        #   1. alpha_count = 0 first (asc on a bool: untouched ranks before
        #      tested) — gives PROPOSED rows preference until they actually
        #      have evidence.
        #   2. created_at desc within each bucket so freshness still
        #      tie-breaks.
        from sqlalchemy import case as _case
        untouched_first = _case(
            (Hypothesis.alpha_count == 0, 0),
            else_=1,
        ).label("_untouched_rank")
        stmt = (
            select(Hypothesis)
            .where(
                Hypothesis.region == region,
                Hypothesis.is_active.is_(True),
                Hypothesis.status.in_(valid_states),
            )
            .order_by(untouched_first.asc(), Hypothesis.created_at.desc())
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
        kept indefinitely for KB even after the task ends.

        P1-C part 2 (2026-05-15): on successful UPDATE the method also calls
        ``_stamp_baseline_if_missing`` under a SAVEPOINT (MFX-3) so the
        ``baseline_metrics`` snapshot for the dropped_sharpe trigger is
        recorded at the exact PROMOTED-flip moment. SAVEPOINT isolation
        means a stamp failure cannot roll back the PROMOTED UPDATE — we
        log a warning and the next run picks it up.
        """
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
        promoted = (result.rowcount or 0) > 0
        if promoted:
            # MFX-3: wrap the stamp in a SAVEPOINT so its failure cannot
            # propagate up and rollback the PROMOTED UPDATE. Mirrors the
            # persistence.py V-27.92 pattern. ``begin_nested()`` on a session
            # with no active transaction issues an implicit BEGIN first.
            try:
                async with self.db.begin_nested():
                    await self._stamp_baseline_if_missing(hypothesis_id)
            except Exception as e:  # pragma: no cover — non-fatal best-effort
                logger.warning(
                    f"[hypothesis] baseline stamp failed (non-fatal) for "
                    f"hid={hypothesis_id}: {type(e).__name__}: {e}"
                )
        return promoted

    async def _stamp_baseline_if_missing(self, hypothesis_id: int) -> None:
        """Freeze the (sharpe_avg, fitness_avg, turnover_avg) of all PASS
        alphas under this hypothesis at the current moment.

        SFX-14: PASS only (not PASS_PROVISIONAL) — provisional starts can
        be low-sharpe and would warp the baseline towards false-negative
        T1 triggers. If only PROV alphas exist at PROMOTED time, baseline
        stays NULL and the next call (after a real PASS lands) fills it in.

        MFX-1: also records ``n_alphas`` so the T1 evaluator can skip
        small-sample (n<3) baselines.

        MFX-4: orders by ``Alpha.id`` ASC, not ``date_created`` (the
        BRAIN-supplied timestamp can refer to alphas pre-dating the DB
        INSERT order). The DB-monotonic PK is the right "first 3 seeds"
        signal.

        No-op if ``baseline_metrics`` is already populated (idempotent).
        """
        h = await self.get_by_id(hypothesis_id)
        if h is None or h.baseline_metrics is not None:
            return
        pass_alphas = (await self.db.execute(
            select(Alpha)
            .where(
                Alpha.hypothesis_id == hypothesis_id,
                Alpha.quality_status == "PASS",
            )
            .order_by(Alpha.id.asc())
        )).scalars().all()
        if not pass_alphas:
            return  # only PROV alphas under this hypothesis — wait

        def _avg(getter):
            vals = [_safe_avg_num(getter(a)) for a in pass_alphas]
            vals = [v for v in vals if v is not None]
            return (sum(vals) / len(vals)) if vals else None

        blob = {
            "stamped_at": datetime.now(timezone.utc).isoformat(),
            "n_alphas": len(pass_alphas),
            # Up to first 3 alpha PKs that contributed — debug breadcrumb.
            "alpha_pks_seed": [a.id for a in pass_alphas[:3]],
            "sharpe_avg":   _avg(lambda a: a.is_sharpe),
            "fitness_avg":  _avg(lambda a: a.is_fitness),
            "turnover_avg": _avg(lambda a: a.is_turnover),
        }
        await self.db.execute(
            update(Hypothesis)
            .where(Hypothesis.id == hypothesis_id)
            .values(baseline_metrics=blob)
        )
        logger.info(
            f"[hypothesis] baseline stamped hid={hypothesis_id} "
            f"n={blob['n_alphas']} sharpe_avg={blob['sharpe_avg']}"
        )

    # ------------------------------------------------------------------
    # P1-C part 2: trigger + LLM scoring helpers
    # ------------------------------------------------------------------

    def _is_postgres(self) -> bool:
        """Return True when the bound dialect is PostgreSQL. Used to
        switch trigger_detail concat between PG-native ``||`` (atomic)
        and a Python merge fallback (sqlite tests, MFX-5)."""
        try:
            bind = self.db.get_bind()
            return bind.dialect.name == "postgresql"
        except Exception:
            return False

    async def mark_triggered(
        self,
        hypothesis_id: int,
        *,
        hits: List[Any],
        source: str = "trigger_eval_beat",
    ) -> bool:
        """Append novel trigger hits to ``trigger_detail`` and flip
        ``is_triggered`` to True (idempotent).

        24h dedup: if a hit of the same ``(type, window_rounds)`` already
        exists with ``hit_at`` within the last 24h, the new hit is silently
        skipped (avoids the daily beat re-appending the same row forever).

        FIFO cap: ``trigger_detail`` is kept under
        ``settings.TRIGGER_DETAIL_MAX_ENTRIES`` — oldest entries drop.

        MFX-5: on PG we use the JSONB ``||`` concat operator so concurrent
        triggers (e.g. manual API + daily beat) never lose a hit. On
        sqlite (unit tests) there's no real concurrency, so a Python
        read-modify-write is correct.

        Returns True when at least one new hit was appended OR
        ``is_triggered`` flipped False→True. Returns False on a fully-deduped
        no-op so callers can distinguish edges.
        """
        if not hits:
            return False
        h = await self.get_by_id(hypothesis_id)
        if h is None:
            return False

        # Materialize incoming hits as dicts; compute the dedup key set
        # against existing entries within the 24h window.
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc.timestamp() - 24 * 3600
        existing = list(h.trigger_detail or [])
        existing_keys = set()
        for e in existing:
            try:
                hit_at = e.get("hit_at")
                if not hit_at:
                    continue
                t = datetime.fromisoformat(hit_at.replace("Z", "+00:00"))
                if t.timestamp() < cutoff:
                    continue
                existing_keys.add((e.get("type"), e.get("window_rounds")))
            except Exception:
                # Malformed legacy entry — ignore in dedup, don't crash.
                continue

        novel: List[Dict[str, Any]] = []
        for hit in hits:
            d = _hit_to_dict(hit)
            key = (d.get("type"), d.get("window_rounds"))
            if key in existing_keys:
                continue
            existing_keys.add(key)
            novel.append(d)

        edge = not h.is_triggered  # is this a False → True flip?
        if not novel and not edge:
            return False  # fully deduped — no audit-worthy change

        triggered_at_value = h.triggered_at or now_utc

        if self._is_postgres() and novel:
            # PG: atomic JSONB || concat. Use ``bindparam(type_=JSONB)`` so
            # SQLAlchemy/asyncpg serialise the python list as JSONB directly.
            # Passing ``json.dumps(novel)`` into ``cast(..., JSONB)`` would
            # double-encode (the JSON text gets stored as a JSONB string
            # containing the array, instead of an actual array).
            await self.db.execute(
                update(Hypothesis)
                .where(Hypothesis.id == hypothesis_id)
                .values(
                    trigger_detail=Hypothesis.trigger_detail.op("||")(
                        sa.bindparam("novel_hits", value=novel, type_=JSONB)
                    ),
                    is_triggered=True,
                    triggered_at=triggered_at_value,
                )
            )
            # Apply FIFO cap if exceeded (in-process read-modify-write —
            # cheap because the row is already in session cache).
            await self.db.refresh(h)
            merged_now = list(h.trigger_detail or [])
            cap = settings.TRIGGER_DETAIL_MAX_ENTRIES
            if len(merged_now) > cap:
                merged_now = merged_now[-cap:]
                await self.db.execute(
                    update(Hypothesis)
                    .where(Hypothesis.id == hypothesis_id)
                    .values(trigger_detail=merged_now)
                )
        else:
            # sqlite path (unit tests) + PG fallback when no novel hits but
            # we still need to flip is_triggered. Python merge — safe under
            # the test event loop where no concurrent updates exist.
            merged = (existing + novel)[-settings.TRIGGER_DETAIL_MAX_ENTRIES:]
            await self.db.execute(
                update(Hypothesis)
                .where(Hypothesis.id == hypothesis_id)
                .values(
                    trigger_detail=merged,
                    is_triggered=True,
                    triggered_at=triggered_at_value,
                )
            )
        return True

    async def update_thesis_score(
        self,
        hypothesis_id: int,
        score: "LLMThesisScore",
        *,
        scored_at: datetime,
        status: str,
    ) -> bool:
        """Persist an LLM thesis-scoring result.

        ``status`` ∈ {"ok", "fallback_failed", "fallback_schema_invalid"}
        (SFX-13). Writes ``thesis_score`` / ``ai_feedback`` /
        ``last_thesis_score_at`` / ``last_thesis_score_status`` and appends
        a snapshot to ``thesis_score_history`` (FIFO-capped at 20).
        """
        if status not in ("ok", "fallback_failed", "fallback_schema_invalid"):
            raise ValueError(f"invalid status: {status!r}")
        h = await self.get_by_id(hypothesis_id)
        if h is None:
            return False
        history = list(h.thesis_score_history or [])
        history.append({
            "scored_at": scored_at.isoformat() if scored_at else None,
            "thesis_score": score.thesis_score,
            "status": status,
            "recommended_action": score.recommended_action,
            "ai_feedback": score.ai_feedback,
        })
        history = history[-20:]
        await self.db.execute(
            update(Hypothesis)
            .where(Hypothesis.id == hypothesis_id)
            .values(
                thesis_score=float(score.thesis_score),
                ai_feedback=score.ai_feedback,
                last_thesis_score_at=scored_at,
                last_thesis_score_status=status,
                thesis_score_history=history,
            )
        )
        return True

    async def mark_abandoned(
        self, hypothesis_id: int, reason: str,
    ) -> bool:
        """→ ABANDONED. Triggered by should_abandon_hypothesis (Plan §B6) —
        N rounds with 0 PASS and HYPOTHESIS-attribution feedback. Once
        abandoned, the hypothesis is excluded from future sampling regardless
        of is_active.

        V-26.41 (2026-05-13): if `abandon_reason` is already populated
        (e.g. an earlier regime-freeze, an earlier mark_abandoned call)
        the new reason is appended with a `|` separator so the original
        diagnostic isn't lost. The column is `Text` so the 1000-char cap
        is purely defensive; in practice the rolling log fits.
        """
        if not reason:
            raise ValueError("abandon_reason required — empty rejection masks debugging")
        # Compose the new reason, preserving prior entries when present.
        existing = await self.get_by_id(hypothesis_id)
        if existing is not None and existing.abandon_reason:
            prefix = existing.abandon_reason
            new_reason = (prefix + " | " + reason)[:1000]
        else:
            new_reason = reason[:1000]
        stmt = (
            update(Hypothesis)
            .where(
                Hypothesis.id == hypothesis_id,
                # Allow re-abandon (idempotent reason update)
                Hypothesis.status != HypothesisStatus.SUPERSEDED.value,
            )
            .values(
                status=HypothesisStatus.ABANDONED.value,
                abandon_reason=new_reason,
            )
        )
        result = await self.db.execute(stmt)
        if (result.rowcount or 0) > 0:
            logger.warning(
                f"[hypothesis] abandoned id={hypothesis_id} reason={reason[:80]!r}"
            )
            return True
        return False

    async def filter_terminal_ids(self, hypothesis_ids: list[int]) -> set[int]:
        """V-27.45: return the subset of `hypothesis_ids` whose status is
        TERMINAL (ABANDONED / SUPERSEDED) — those must NOT receive new alpha /
        failure links.

        Used as a TOCTOU guard at alpha INSERT time: V-22.13 hypothesis reuse
        (generation.py) reads status in a since-closed session, and a
        concurrent B5 `mark_abandoned` may have flipped it in the race window.
        `mark_abandoned` is a terminal transition (no path back to
        ACTIVE/PROPOSED), so the status read here at INSERT time is final.

        Empty input → empty set (no query)."""
        if not hypothesis_ids:
            return set()
        stmt = select(Hypothesis.id).where(
            Hypothesis.id.in_(list(hypothesis_ids)),
            Hypothesis.status.in_([
                HypothesisStatus.ABANDONED.value,
                HypothesisStatus.SUPERSEDED.value,
            ]),
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return set(rows)

    # V-27.B (2026-05-14): mark_superseded removed — the G-refine loop
    # (abandon → refine into a SUPERSEDED child) never fired in production
    # (V-26.14: 0/673 hypotheses had a parent). HypothesisStatus.SUPERSEDED
    # + Hypothesis.parent_hypothesis_id are kept (schema unchanged) but no
    # longer written.

    async def set_active_flag(
        self, hypothesis_id: int, is_active: bool, reason: Optional[str] = None,
    ) -> bool:
        """Toggle the is_active boolean for regime-triggered freeze (Plan
        v5+ Final §简化冷冻). Does NOT change `status` — a frozen hypothesis
        is still PROMOTED/ACTIVE, just temporarily skipped by sampling.

        V-26.42 (2026-05-13): freeze/unfreeze reason text is appended to
        `abandon_reason` because the schema doesn't yet have a dedicated
        column. The pre-fix version OVERWROTE the field when there was
        no prior entry — losing the original diagnostic. Now always
        appends with a clear `[regime-freeze]` / `[regime-unfreeze]`
        prefix so a downstream reader can grep for the marker AND see
        every state transition that ever touched the row. When the
        schema gains a dedicated `lifecycle_events` JSONB column, this
        helper should move there and stop touching abandon_reason at all.
        """
        values: Dict[str, Any] = {"is_active": is_active}
        if reason:
            existing = await self.get_by_id(hypothesis_id)
            if existing:
                tag = "[regime-freeze] " if not is_active else "[regime-unfreeze] "
                marker = tag + reason
                # Append-only: never overwrite the prior abandon_reason.
                prior = existing.abandon_reason or ""
                if prior:
                    composed = prior + " | " + marker
                else:
                    composed = marker
                values["abandon_reason"] = composed[:1000]
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
        # V-26.13: count Alpha-table attempts and AlphaFailure attempts
        # separately, then sum into `alpha_count`. The two queries are
        # independent (no JOIN) — combining them via OUTER JOIN would
        # multiply rows when both tables have entries.
        alpha_stmt = (
            select(
                func.count(Alpha.id).label("alpha_attempts"),
                func.count(
                    case((Alpha.quality_status.in_(["PASS", "PASS_PROVISIONAL"]), 1))
                ).label("pass_count"),
                func.avg(Alpha.is_sharpe).label("sharpe_avg"),
                func.max(Alpha.is_sharpe).label("sharpe_max"),
            )
            .where(Alpha.hypothesis_id == hypothesis_id)
        )
        alpha_row = (await self.db.execute(alpha_stmt)).one()

        fail_stmt = (
            select(func.count(AlphaFailure.id).label("fail_attempts"))
            .where(AlphaFailure.hypothesis_id == hypothesis_id)
        )
        fail_row = (await self.db.execute(fail_stmt)).one()

        alpha_attempts = int(alpha_row.alpha_attempts or 0)
        fail_count = int(fail_row.fail_attempts or 0)
        alpha_count = alpha_attempts + fail_count
        pass_count = int(alpha_row.pass_count or 0)
        sharpe_avg = float(alpha_row.sharpe_avg) if alpha_row.sharpe_avg is not None else None
        sharpe_max = float(alpha_row.sharpe_max) if alpha_row.sharpe_max is not None else None

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
            fail_count=fail_count,
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

    async def upsert_round_stats(
        self,
        *,
        hypothesis_id: int,
        task_id: Optional[int],
        round_index: int,
        alpha_count: int,
        pass_count: int,
        syntax_fail_count: int,
        simulate_fail_count: int,
        quality_fail_count: int,
        flip_alpha_count: int = 0,
        flip_pass_count: int = 0,
        retryable_count: int = 0,
        attribution: Optional[str] = None,
        attribution_reason: Optional[str] = None,
        best_sharpe: Optional[float] = None,
    ) -> None:
        """V-27.92: upsert one (hypothesis_id, round_index, task_id) row of
        per-round detail into hypothesis_round_stats — the authoritative
        input for should_abandon_hypothesis.

        Idempotent: LangGraph can replay the same B5 round after a worker
        restart, so the conflict target is the uniqueness key and the row is
        overwritten (latest write wins) rather than duplicated.

        Counts must be the REAL attribution — flip-retry products and
        retryable (transient BRAIN failure) attempts go in flip_alpha_count /
        retryable_count and must NOT be folded into alpha_count by the caller
        (V-27.71 / V-27.61).
        """
        if task_id is None:
            # task_id is NOT NULL + part of the uniqueness key. B5 should
            # always have a task context; if it somehow doesn't, skip rather
            # than crash the feedback node — the abandon decision degrades to
            # "no detail yet" which is safe (won't false-abandon).
            logger.warning(
                f"[hypothesis] upsert_round_stats skipped for hid={hypothesis_id} "
                f"round={round_index}: task_id is None (B5 ran without task context)"
            )
            return

        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from backend.models import HypothesisRoundStats

        values = dict(
            hypothesis_id=hypothesis_id,
            task_id=task_id,
            round_index=round_index,
            alpha_count=alpha_count,
            pass_count=pass_count,
            syntax_fail_count=syntax_fail_count,
            simulate_fail_count=simulate_fail_count,
            quality_fail_count=quality_fail_count,
            flip_alpha_count=flip_alpha_count,
            flip_pass_count=flip_pass_count,
            retryable_count=retryable_count,
            attribution=attribution,
            attribution_reason=attribution_reason,
            best_sharpe=best_sharpe,
        )
        stmt = pg_insert(HypothesisRoundStats).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["hypothesis_id", "round_index", "task_id"],
            set_={
                k: stmt.excluded[k]
                for k in values
                if k not in ("hypothesis_id", "round_index", "task_id")
            },
        )
        await self.db.execute(stmt)

    # ------------------------------------------------------------------
    # Helper queries
    # ------------------------------------------------------------------

    # V-27.B (2026-05-14): find_unused_refined removed — it was the
    # node_hypothesis pickup query for G-refine'd children, but the
    # G-refine loop never produced any (no parent.status=SUPERSEDED rows
    # ever existed), so this query always returned None.

    async def rounds_active(self, hypothesis_id: int) -> int:
        """Plan v5+ §Phase 3 prep — count rounds this hypothesis has been
        evaluated in.

        V-27.120: reads the precise round_index from hypothesis_round_stats
        instead of the old 60-second-bucket estimate over alpha created_at
        timestamps (which under-counted whenever rounds ran faster than the
        bucket, feeding low values into Phase 3 readiness reports). Each row
        in hypothesis_round_stats is one (hypothesis, round, task) the
        hypothesis was evaluated in — the uniqueness key guarantees no
        double-counting, so a plain COUNT(*) is the round count.

        Pre-migration hypotheses have no detail rows and return 0 — Phase 3
        readiness is an analytics use and fresh data fills in quickly.

        Used by Phase 3 readiness analysis to answer:
        "Do older hypotheses (more rounds_active) PASS more reliably?"
        """
        from sqlalchemy import select as _sel, func as _f
        from backend.models import HypothesisRoundStats

        stmt = (
            _sel(_f.count())
            .select_from(HypothesisRoundStats)
            .where(HypothesisRoundStats.hypothesis_id == hypothesis_id)
        )
        result = await self.db.execute(stmt)
        return int(result.scalar() or 0)

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
