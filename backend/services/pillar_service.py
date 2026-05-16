"""PillarService (P3 ops dashboard, 2026-05-16).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare` (Five
Pillars), ops dashboard plan §1.7.

Abstracted from ``backend/tasks/pillar_balance_check.py:_run_async()``
so /ops/pillar-balance can compute the same report on demand without
spawning the Celery task. The task itself becomes a thin wrapper that
calls ``compute_balance_report()`` and persists the JSON to
``docs/pillar_balance/<sh-date>.json``.

Design notes:

* All public methods are pure-async + side-effect-free except for the
  one DB SELECT they need; writes / file IO stay in the task layer.
* ``compute_balance_report`` is byte-for-byte equivalent to the previous
  task output (modulo the ``generated_at_utc`` timestamp which moves
  every run). Verified by the regression test
  ``test_pillar_service_byte_for_byte.py``.
* ``get_next_pillar_for_region`` exposes the deficit-driven nudge logic
  for the /ops/pillar/deficit-recommendation endpoint AND for future
  migration of ``agents/graph/nodes/generation.py`` (left out of scope
  for P3 to keep the mining loop decoupled — see plan §8).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from backend.services.base import BaseService

logger = logging.getLogger("services.pillar")

SH_TZ = timezone(timedelta(hours=8))


class PillarService(BaseService):
    """Five-pillar (momentum/value/quality/volatility/sentiment) balance
    aggregator over the last N days of alphas, with deficit + nudge logic.

    Designed for the ops dashboard read path. The mining-time nudge in
    ``agents/graph/nodes/generation.py`` is unchanged in P3 — it still
    runs its own inline query because moving it here would require
    plumbing AsyncSession into the LangGraph node state, a bigger
    refactor than this PR.
    """

    DEFAULT_LOOKBACK_DAYS = 7

    # ------------------------------------------------------------------
    # Core: compute_balance_report — replaces task's _run_async body
    # ------------------------------------------------------------------

    async def compute_balance_report(
        self,
        *,
        lookback_days: Optional[int] = None,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Build the per-region pillar shares report.

        Output schema matches docs/pillar_balance/<date>.json exactly so
        the same React page renders identical structures whether the
        data came from the daily beat archive or a live recompute.

        Args:
            lookback_days: window over which alphas count. None → 7.
            now_utc: injection point for tests; defaults to current UTC.
        """
        from backend.config import settings  # lazy to avoid cycle
        from backend.models import Alpha, Hypothesis
        from backend.pillar_classifier import PILLAR_VALUES, infer_pillar

        lookback = lookback_days or self.DEFAULT_LOOKBACK_DAYS
        now_utc = now_utc or datetime.now(timezone.utc)
        sh_now = now_utc.astimezone(SH_TZ)
        # asyncpg rejects offset-aware ↔ naive comparisons on a TIMESTAMP
        # WITHOUT TIME ZONE column — strip tz off the cutoff before WHERE.
        cutoff = (now_utc - timedelta(days=lookback)).replace(tzinfo=None)
        target = getattr(settings, "PILLAR_TARGET_DISTRIBUTION", {}) or {}

        grouped_rows = await self._aggregate_grouped(Alpha, Hypothesis, cutoff)
        null_rows = await self._aggregate_null_pillar(Alpha, Hypothesis, cutoff)

        by_region: Dict[str, Dict[str, int]] = {}
        inferred: Dict[str, Dict[str, int]] = {}

        for region, pillar, count in grouped_rows:
            region = region or "UNKNOWN"
            bucket = by_region.setdefault(region, {})
            key = pillar if pillar else "unknown"
            bucket[key] = bucket.get(key, 0) + int(count or 0)

        for region, expression in null_rows:
            region = region or "UNKNOWN"
            inferred_pillar = infer_pillar(expression=expression or "")
            ibucket = inferred.setdefault(region, {})
            ibucket[inferred_pillar] = ibucket.get(inferred_pillar, 0) + 1

        regions: Dict[str, Dict[str, Any]] = {}
        all_regions = set(by_region.keys()) | set(inferred.keys())
        for region in sorted(all_regions):
            stamped = by_region.get(region, {})
            legacy_inferred = inferred.get(region, {})
            regions[region] = self._build_region_block(
                stamped=stamped,
                legacy_inferred=legacy_inferred,
                target=target,
            )

        return {
            "report_date": sh_now.strftime("%Y-%m-%d"),
            "generated_at_utc": now_utc.isoformat(),
            "lookback_days": lookback,
            "pillar_values": sorted(PILLAR_VALUES),
            "regions": regions,
            "totals": {
                "regions_checked": len(regions),
                "stamped_alphas": sum(
                    r["stamped_total"] for r in regions.values()
                ),
                "legacy_inferred_alphas": sum(
                    r["legacy_inferred_total"] for r in regions.values()
                ),
            },
        }

    # ------------------------------------------------------------------
    # Nudge helper — single-region "what's most under-represented?"
    # ------------------------------------------------------------------

    async def get_next_pillar_for_region(
        self,
        region: str,
        *,
        lookback_days: Optional[int] = None,
        skew_threshold: float = 0.0,
    ) -> Optional[str]:
        """Return the pillar with the largest deficit for ``region``,
        or None if all targets are met (every deficit ≤ ``skew_threshold``).

        Computes the same report as ``compute_balance_report`` but picks
        out one region. For ops dashboards the full report is usually
        cheaper — call ``compute_balance_report`` and slice in JavaScript;
        this method exists so future migration of
        ``agents/graph/nodes/generation.py``'s nudge block (currently
        inline) has a clean target.
        """
        report = await self.compute_balance_report(lookback_days=lookback_days)
        block = (report.get("regions") or {}).get(region)
        if not block:
            return None
        deficits = block.get("deficits") or {}
        if not deficits:
            return None
        top_pillar, top_value = max(deficits.items(), key=lambda kv: kv[1])
        if top_value > skew_threshold:
            return top_pillar
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _aggregate_grouped(
        self, Alpha, Hypothesis, cutoff,
    ) -> List[Tuple[str, Optional[str], int]]:
        """SUM by (region, pillar) for alphas in the lookback window.

        Uses an OUTER JOIN so legacy ``hypothesis_id IS NULL`` rows
        contribute to the `unknown` bucket (and are inferred separately
        via _aggregate_null_pillar).
        """
        stmt = (
            select(
                Alpha.region,
                Hypothesis.pillar,
                func.count(Alpha.id),
            )
            .select_from(Alpha)
            .outerjoin(Hypothesis, Alpha.hypothesis_id == Hypothesis.id)
            .where(Alpha.created_at >= cutoff)
            .group_by(Alpha.region, Hypothesis.pillar)
        )
        rows = (await self.db.execute(stmt)).all()
        # Coerce to plain tuples so the dataclass-free caller doesn't
        # depend on SQLAlchemy Row internals.
        return [(r[0], r[1], r[2]) for r in rows]

    async def _aggregate_null_pillar(
        self, Alpha, Hypothesis, cutoff,
    ) -> List[Tuple[str, str]]:
        """Pull expression-level rows for alphas whose hypothesis has no
        pillar set, so the caller can run ``infer_pillar`` over them."""
        stmt = (
            select(Alpha.region, Alpha.expression)
            .select_from(Alpha)
            .outerjoin(Hypothesis, Alpha.hypothesis_id == Hypothesis.id)
            .where(
                Alpha.created_at >= cutoff,
                Hypothesis.pillar.is_(None),
            )
        )
        rows = (await self.db.execute(stmt)).all()
        return [(r[0], r[1]) for r in rows]

    @staticmethod
    def _build_region_block(
        *,
        stamped: Dict[str, int],
        legacy_inferred: Dict[str, int],
        target: Dict[str, float],
    ) -> Dict[str, Any]:
        """Compute the per-region payload — same math as the task version.

        Kept verbatim with the task to preserve byte-for-byte report
        equivalence (verified by regression test).
        """
        # Stamped denominator excludes ``unknown`` so legacy backlog
        # doesn't dilute fresh-share computation.
        stamped_total = sum(c for p, c in stamped.items() if p in target)
        shares = {
            p: (stamped.get(p, 0) / stamped_total) if stamped_total else 0.0
            for p in target
        }
        deficits = {
            p: max(0.0, target.get(p, 0.0) - shares.get(p, 0.0))
            for p in target
        }
        skew = max(shares.values()) - min(shares.values()) if shares else 0.0
        top_def = (
            max(deficits.items(), key=lambda kv: kv[1])
            if deficits else (None, 0.0)
        )
        return {
            "stamped_counts": stamped,
            "stamped_total": stamped_total,
            "unknown_count": stamped.get("unknown", 0),
            "legacy_inferred_counts": legacy_inferred,
            "legacy_inferred_total": sum(legacy_inferred.values()),
            "shares": {k: round(v, 3) for k, v in shares.items()},
            "target": target,
            "deficits": {k: round(v, 3) for k, v in deficits.items()},
            "skew": round(skew, 3),
            "next_pillar": top_def[0] if (top_def[1] or 0) > 0 else None,
        }
