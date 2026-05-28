"""Persister — write winner VariantSimResults into the alphas table.

Field mapping mirrors :file:`scripts/_persist_and_submit_15621_variant.py`
(the manual one-shot that produced alpha 15720) and
:file:`backend/agents/graph/nodes/persistence.py:398-450` (the live mining
persist path). Differences are intentional and small:

  - ``parent_alpha_id`` and ``optimization_run_id`` are always set (Persister
    only ever writes optimization-derived rows; mining writes its own).
  - ``parent_alpha_family_id`` is derived via
    :func:`backend.services.optimization.family_id.derive_parent_alpha_family_id`
    BEFORE insert. The migration's WITH RECURSIVE backfill guarantees every
    pre-existing parent already has its own family_id set, so we can do
    one DB read per winner instead of walking the chain.
  - ``metrics["_origin"]`` is stamped from the variant's generator_name —
    ``"opt:settings_sweep"`` for Stage A — so the backlog UI can filter
    mining-origin vs optimization-origin without joining tables.
  - ``metrics["_self_corr"]`` is computed asynchronously when a
    CorrelationService is injected; soft-fails to ``None`` on any error
    so a corr cache miss never blocks the persist.

ON CONFLICT semantics: the Alpha.alpha_id UNIQUE constraint may collide
when BRAIN re-issues the same id for a re-simulated variant. We catch the
IntegrityError and return ``None`` in that slot — SubmitPolicy needs the
slot preserved so its decisions stay 1:1 with the input.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §6.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Alpha
from backend.services.optimization.family_id import (
    derive_parent_alpha_family_id,
)
from backend.services.optimization.protocols import VariantSimResult


logger = logging.getLogger("optimization.persister")


def _expr_hash(expression: str, settings: Dict[str, Any]) -> str:
    """sha256 over (expression, neutralization) — neut is the most-impactful
    setting axis (15621 empirical) so two variants with the same expression
    but different neut get distinct hashes for dedup."""
    neut = str(settings.get("neutralization", ""))
    payload = (expression + "|" + neut).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


class Persister:
    """Stage A Persister. Inject the db session at construction; corr_service
    is optional (None = skip self_corr stamping, value stays None)."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        corr_service: Optional[Any] = None,
        repository: Optional[Any] = None,
    ):
        self.db = db
        self.corr_service = corr_service
        # repository is the OptimizationRunRepositoryImpl — Persister calls
        # record_persist() at the end of save() to update cycle counters.
        self.repository = repository

    async def save(
        self,
        winners: List[VariantSimResult],
        parent_alpha_id: int,
        opt_run_id: int,
    ) -> List[Optional[int]]:
        if not winners:
            return []

        family_id = await derive_parent_alpha_family_id(
            parent_alpha_id, self.db
        )

        out: List[Optional[int]] = []
        for w in winners:
            pk = await self._save_one(
                w, parent_alpha_id, opt_run_id, family_id
            )
            out.append(pk)
        return out

    async def _save_one(
        self,
        winner: VariantSimResult,
        parent_alpha_id: int,
        opt_run_id: int,
        family_id: Optional[int],
    ) -> Optional[int]:
        v = winner.variant
        s = v.settings

        # Compute self_corr (soft-fail). Region needed for cache lookup;
        # Stage A always has it (variant.settings carries region).
        self_corr_value, self_corr_source = None, None
        if self.corr_service is not None and winner.brain_alpha_id:
            try:
                self_corr_value, self_corr_source = (
                    await self.corr_service.calc_self_corr(
                        alpha_id=str(winner.brain_alpha_id),
                        region=str(s.get("region", "USA")),
                    )
                )
            except Exception as ex:  # noqa: BLE001
                logger.warning(
                    "[Persister] self_corr calc failed for %s (non-fatal): %s",
                    winner.brain_alpha_id, ex,
                )

        # metrics JSONB — superset of BRAIN's raw IS metrics + audit fields.
        is_m = _extract_is_metrics(winner.sim_response)
        metrics_jsonb = {
            **(is_m or {}),
            "checks": (
                is_m.get("checks") if isinstance(is_m, dict) else None
            ) or winner.sim_response.get("checks"),
            "_sim_settings": dict(s),
            "_origin": f"opt:{v.generator_name}",
            "_optimization_tag": v.tag,
            "_self_corr": self_corr_value,
            "_self_corr_source": (
                str(self_corr_source) if self_corr_source is not None else None
            ),
        }

        alpha = Alpha(
            alpha_id=str(winner.brain_alpha_id) if winner.brain_alpha_id else None,
            expression=v.expression,
            expression_hash=_expr_hash(v.expression, s),
            region=str(s.get("region", "USA")),
            universe=str(s.get("universe", "TOP3000")),
            delay=int(s.get("delay", 1)) if s.get("delay") is not None else 1,
            decay=int(s.get("decay", 0)) if s.get("decay") is not None else 0,
            neutralization=str(s.get("neutralization", "NONE")),
            truncation=float(s.get("truncation", 0.08)),
            quality_status="PASS",
            can_submit=True,
            status="UNSUBMITTED",
            metrics=metrics_jsonb,
            is_sharpe=winner.sharpe,
            is_fitness=winner.fitness,
            is_returns=_safe(is_m, "returns"),
            is_turnover=winner.turnover,
            is_margin=winner.margin,
            is_drawdown=_safe(is_m, "drawdown"),
            is_long_count=_safe(is_m, "longCount", to_int=True),
            is_short_count=_safe(is_m, "shortCount", to_int=True),
            parent_alpha_id=int(parent_alpha_id),
            parent_alpha_family_id=family_id,
            optimization_run_id=int(opt_run_id),
            metrics_snapshot_at=datetime.utcnow(),
        )

        # SAVEPOINT (begin_nested) so a single winner's IntegrityError doesn't
        # nuke prior successful flushes in the same save() loop. Without this,
        # a multi-winner save() where winner #3 collides would db.rollback()
        # the whole session and lose winners #1 and #2 — corrupting the 1:1
        # alignment SubmitPolicy.decide depends on. Mirrors the canonical
        # pattern in backend/agents/graph/nodes/persistence.py:454.
        try:
            async with self.db.begin_nested():
                self.db.add(alpha)
                await self.db.flush()
            return int(alpha.id)
        except IntegrityError as ex:
            # Most likely Alpha.alpha_id unique collision (BRAIN re-issued an
            # id we've seen). The SAVEPOINT auto-rolled back; session stays
            # valid for the next winner. Keep the slot in the return list as
            # None so SubmitPolicy's index-aligned decisions don't drift.
            logger.info(
                "[Persister] alpha_id collision on %s (skipped): %s",
                winner.brain_alpha_id, ex,
            )
            return None


def _extract_is_metrics(sim: Dict[str, Any]) -> Dict[str, Any]:
    """Same shape spotter as the Simulator — kept here so Persister doesn't
    depend on simulator import (lower-layer module should not import from
    a peer)."""
    if not isinstance(sim, dict):
        return {}
    is_m = sim.get("is") or sim.get("metrics") or {}
    if isinstance(is_m, dict) and "sharpe" not in is_m and "metrics" in is_m:
        is_m = is_m["metrics"]
    return is_m if isinstance(is_m, dict) else {}


def _safe(d: Dict[str, Any], key: str, *, to_int: bool = False) -> Optional[Any]:
    v = d.get(key) if isinstance(d, dict) else None
    if v is None:
        return None
    try:
        return int(v) if to_int else float(v)
    except (TypeError, ValueError):
        return None
