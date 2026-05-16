"""OpsService — orchestration layer for /api/v1/ops/* endpoints.

Source: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.1, §1.6.

Sits between the ``ops`` router and the various P0/P1/P2 services. Holds
the cross-cutting concerns:

* manual Celery task triggers — whitelist + per-task and global throttling
* "recent runs" lookup against the Celery result backend
* Phase 2 page composers (``get_alpha_health``, ``get_hypothesis_health``,
  ``get_overview``) — they read from docs via OpsReportReader, derive
  KPI aggregates, and shape the payload into something the React table /
  chart components can consume without per-row computation.

Why no fresh_service is wired into Phase 2 endpoints: the underlying
``AlphaHealthService.run_full_check`` requires a constructed
BaselineProvider + LLMService, neither of which is appropriate to spin
up inside a request handler. Instead we read whatever the daily beat
last produced and offer a Rerun button that sends the Celery task —
data refreshes on the next GET after the task completes.

Constructed once per request by ``Depends(get_ops_service)`` in the
router.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("services.ops")


# ---------------------------------------------------------------------------
# Whitelist + throttle constants
# ---------------------------------------------------------------------------

# Celery task names that the ops console is allowed to trigger by name.
# These map 1:1 to the daily beat schedule in backend/celery_app.py;
# nothing else (mining tasks, sync_datasets, etc.) is exposed.
_ALLOWED_TRIGGER_NAMES: frozenset[str] = frozenset({
    "backend.tasks.run_alpha_health_check",
    "backend.tasks.run_hypothesis_health_check",
    "backend.tasks.run_pillar_balance_check",
    "backend.tasks.run_negative_knowledge_extract",
    "backend.tasks.run_macro_narrative_extract",
    "backend.tasks.run_regime_infer",
    "backend.tasks.monitor_llm_op_hallucinations",
    "backend.tasks.run_daily_feedback",
})

# Per-task: don't allow another trigger within this many seconds. Stops
# accidental double-clicks + casual abuse without preventing legitimate
# back-to-back debugging.
PER_TASK_THROTTLE_SEC = 60

# Global: no more than N triggers across all tasks per minute. Catches
# the case where someone scripts a loop against the API.
GLOBAL_THROTTLE_LIMIT = 10
GLOBAL_THROTTLE_WINDOW_SEC = 60

# Redis key prefixes — keep in sync with what /ops/feature-flags etc. use
_PER_TASK_KEY = "aiac:ops_trigger_throttle:{task}"
_GLOBAL_KEY = "aiac:ops_trigger_global_count"


# ---------------------------------------------------------------------------
# Errors used by the router
# ---------------------------------------------------------------------------

class OpsTriggerError(Exception):
    """Base class for trigger-related router-translatable errors."""
    http_status = 400


class UnknownTaskError(OpsTriggerError):
    http_status = 400


class PerTaskThrottledError(OpsTriggerError):
    http_status = 409


class GlobalThrottledError(OpsTriggerError):
    http_status = 429


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TriggerResult:
    task_id: str
    name: str
    accepted_at: datetime
    throttle_remaining_sec: int


@dataclass
class TaskRunRecord:
    """Light view of a Celery result-backend entry for /ops/tasks/recent-runs."""
    task_id: str
    name: Optional[str]
    status: Optional[str]
    date_done: Optional[str]
    result: Any


@dataclass
class SourcedPayload:
    """Standard wrapper for any /ops/* GET endpoint payload.

    The ``source`` field is one of OpsReportReader.SOURCE_* and lets the
    frontend render an "is this fresh?" tag without inspecting the body.
    """
    source: str
    data: Dict[str, Any]
    fetched_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AlphaHealthSummary:
    """Aggregated view of one alpha_health_check report for the dashboard.

    Derived fields are computed once on the server so the React side gets
    chart-ready arrays instead of having to fold over the records list.
    """
    report_date: Optional[str]
    band_counts: Dict[str, int]            # {GREEN, YELLOW, ORANGE, RED, UNKNOWN: N}
    band_pcts: Dict[str, float]            # 0-100
    by_region: Dict[str, Dict[str, int]]   # {region: {band: count}}
    total_alphas: int
    failed: int
    record_count: int                      # alphas that made it into `records`
    source: str
    stale_days: Optional[int] = None       # set when source==docs_archived fallback


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class OpsService:
    """Stateless orchestration; constructed per request.

    The ``db`` session is held for child-service composition (alpha health,
    pillar, etc.) which arrives in Phase 2/3 of the plan. Phase 1 only
    uses Redis + Celery; the session is held but unused.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Manual task trigger — Celery .send_task with throttle + whitelist
    # ------------------------------------------------------------------

    async def trigger_task(
        self,
        name: str,
        kwargs: Optional[Dict[str, Any]] = None,
        *,
        actor: str = "ops_console",
    ) -> TriggerResult:
        """Send a Celery task by name, after whitelist + throttle checks.

        Raises :class:`UnknownTaskError`, :class:`PerTaskThrottledError`,
        or :class:`GlobalThrottledError` for the router to translate to
        409 / 429 without exposing infra details.

        Side effect on Redis: per-task SETNX (60s) + global counter
        (60s window). Both are best-effort — if Redis is down we
        fail-open (correctness > rate-limiting).
        """
        if name not in _ALLOWED_TRIGGER_NAMES:
            raise UnknownTaskError(
                f"task {name!r} is not in the ops trigger whitelist"
            )

        # ---- throttle ----------------------------------------------------
        per_task_remaining = self._check_per_task_throttle(name)
        if per_task_remaining > 0:
            raise PerTaskThrottledError(
                f"task {name} was triggered <{PER_TASK_THROTTLE_SEC}s ago "
                f"({per_task_remaining}s remaining)"
            )

        if not self._check_and_bump_global_throttle():
            raise GlobalThrottledError(
                f"global ops-trigger limit hit "
                f"({GLOBAL_THROTTLE_LIMIT}/{GLOBAL_THROTTLE_WINDOW_SEC}s)"
            )

        # ---- send -------------------------------------------------------
        # Lazy import keeps test environments without Celery happy
        from backend.celery_app import celery_app
        try:
            async_result = celery_app.send_task(name, kwargs=kwargs or {})
            task_id = async_result.id
        except Exception as ex:
            logger.error("[ops] send_task failed for %s: %s", name, ex)
            # Roll back the per-task lock so the operator can retry
            self._clear_per_task_throttle(name)
            raise OpsTriggerError(f"send_task failed: {ex}") from ex

        logger.info(
            "[ops] task triggered name=%s task_id=%s actor=%s kwargs=%s",
            name, task_id, actor, kwargs,
        )

        return TriggerResult(
            task_id=task_id,
            name=name,
            accepted_at=datetime.utcnow(),
            throttle_remaining_sec=PER_TASK_THROTTLE_SEC,
        )

    async def list_recent_celery_runs(
        self,
        task_name: Optional[str] = None,
        limit: int = 20,
    ) -> List[TaskRunRecord]:
        """Walk the Celery result backend (Redis) for recent task results.

        Result keys are ``celery-task-meta-<uuid>`` JSON blobs. Default TTL
        is 86400s. We KEYS-scan and filter; for our scale (<1k results
        per day) the cost is negligible. If Redis is down we return [].
        """
        try:
            cli = self._redis()
        except Exception as ex:
            logger.warning("[ops] redis unavailable for recent runs: %s", ex)
            return []

        results: List[TaskRunRecord] = []
        try:
            import json
            # SCAN is preferred over KEYS for large keyspaces — but for
            # <1k task-meta keys per day, KEYS is fine and simpler.
            keys = cli.keys("celery-task-meta-*") or []
            for key in keys:
                try:
                    raw = cli.get(key)
                    if raw is None:
                        continue
                    blob = json.loads(raw)
                except Exception:
                    continue
                if task_name and blob.get("task") != task_name:
                    # Some Celery versions store name under different keys
                    if blob.get("name") != task_name:
                        continue
                results.append(TaskRunRecord(
                    task_id=blob.get("task_id") or key.decode().split("-")[-1]
                    if isinstance(key, bytes) else str(key).split("-")[-1],
                    name=blob.get("task") or blob.get("name"),
                    status=blob.get("status"),
                    date_done=blob.get("date_done"),
                    result=blob.get("result"),
                ))
        except Exception as ex:
            logger.warning("[ops] recent runs scan failed: %s", ex)
            return []

        # Newest first, capped to `limit`
        results.sort(key=lambda r: r.date_done or "", reverse=True)
        return results[:max(1, min(limit, 200))]

    # ------------------------------------------------------------------
    # Throttle helpers — module-private, fail-open on Redis blip
    # ------------------------------------------------------------------

    @staticmethod
    def _redis():
        """Returns a Redis client or raises (caller decides what to do)."""
        from backend.tasks.redis_pool import get_redis_client  # lazy
        return get_redis_client()

    def _check_per_task_throttle(self, name: str) -> int:
        """Return seconds remaining on the per-task lock; 0 if free.

        Uses SETNX-style: SET key value EX 60 NX. If the SET succeeds we
        own the lock. If it fails the key already exists; we look up its
        TTL to give the operator a precise "wait N seconds" message.
        """
        try:
            cli = self._redis()
        except Exception as ex:
            logger.debug("[ops] redis throttle check skipped: %s", ex)
            return 0

        key = _PER_TASK_KEY.format(task=name)
        try:
            ok = cli.set(key, "1", ex=PER_TASK_THROTTLE_SEC, nx=True)
            if ok:
                return 0
            # Key already there; read TTL for a friendlier error
            ttl = cli.ttl(key)
            return max(0, int(ttl)) if ttl is not None else 0
        except Exception as ex:
            logger.debug("[ops] redis throttle check failed: %s", ex)
            return 0  # fail-open

    def _clear_per_task_throttle(self, name: str) -> None:
        """Roll back the lock so the operator can retry after a send failure."""
        try:
            cli = self._redis()
            cli.delete(_PER_TASK_KEY.format(task=name))
        except Exception:
            pass

    def _check_and_bump_global_throttle(self) -> bool:
        """Return True if under the global limit + bumped; False if over."""
        try:
            cli = self._redis()
        except Exception:
            return True  # fail-open

        try:
            count = cli.incr(_GLOBAL_KEY)
            if count == 1:
                # First in this minute — set the window expiry
                cli.expire(_GLOBAL_KEY, GLOBAL_THROTTLE_WINDOW_SEC)
            return count <= GLOBAL_THROTTLE_LIMIT
        except Exception:
            return True  # fail-open

    # ==================================================================
    # Phase 2 — Alpha Health composers
    # ==================================================================
    # All Phase 2 page methods follow the same shape:
    #
    #   1. ask OpsReportReader for a payload + source tag
    #   2. derive a summary (counts / pcts / by_region etc.)
    #   3. return ``(summary, raw_payload)`` so the router can attach the
    #      ``source`` tag to the response and the React side gets both the
    #      KPI numbers AND the underlying records for drill-down tables.
    #
    # No DB access is needed for any of these — the daily beat already
    # baked everything into docs/<kind>/<date>.json.

    @staticmethod
    def _summarize_alpha_health(
        payload: Dict[str, Any], source: str,
    ) -> AlphaHealthSummary:
        """Compute band counts + per-region breakdown from a raw report."""
        records = payload.get("records") or payload.get("alphas") or []
        bands: Counter = Counter()
        per_region: Dict[str, Counter] = {}
        for r in records:
            band = (r.get("health_band") or r.get("band") or "UNKNOWN").upper()
            bands[band] += 1
            region = r.get("region") or "UNKNOWN"
            per_region.setdefault(region, Counter())[band] += 1
        total = sum(bands.values())
        pcts = (
            {b: round(100.0 * n / total, 1) for b, n in bands.items()}
            if total else {b: 0.0 for b in bands}
        )
        # Totals reported by the daily task — preferred over derived counts
        # when the file carries them, so we don't disagree with the audit log.
        reported_total = (
            payload.get("totals", {}).get("checked")
            if isinstance(payload.get("totals"), dict)
            else None
        )
        return AlphaHealthSummary(
            report_date=payload.get("report_date"),
            band_counts=dict(bands),
            band_pcts=pcts,
            by_region={
                region: dict(counts) for region, counts in per_region.items()
            },
            total_alphas=reported_total if isinstance(reported_total, int) else total,
            failed=int(payload.get("failed", 0) or 0),
            record_count=len(records),
            source=source,
            stale_days=payload.get("_stale_days"),
        )

    async def get_alpha_health(
        self, target: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Return the latest alpha_health_check summary + raw records.

        Router shape: ``{summary: AlphaHealthSummary, payload: <raw>, source: <tag>}``
        — the summary is the KPI strip + per-region stacked bar source,
        the payload is what the drill-down table needs.
        """
        # Lazy import keeps cycle-free; ops_service is allowed to depend on
        # the reader but not vice versa.
        from backend.services.ops_report_reader import OpsReportReader

        reader = OpsReportReader()
        payload, source = await reader.get_or_compute(
            "alpha_health_check", target,
        )
        summary = self._summarize_alpha_health(payload, source)
        return {"summary": summary, "payload": payload, "source": source}

    async def get_alpha_health_history(
        self, days: int = 30,
    ) -> List[Dict[str, Any]]:
        """Trend series — one entry per available day, oldest→newest.

        Each entry is a flat dict with the same fields as
        ``AlphaHealthSummary`` plus the ``_date`` stamp from the reader,
        ready to feed straight into a Recharts ``<LineChart>`` dataKey.
        """
        from backend.services.ops_report_reader import OpsReportReader

        reader = OpsReportReader()
        raw_days = await reader.list_recent("alpha_health_check", days=days)
        out: List[Dict[str, Any]] = []
        for entry in raw_days:
            summary = self._summarize_alpha_health(entry, source="docs_archived")
            out.append({
                "_date": entry.get("_date"),
                "band_counts": summary.band_counts,
                "band_pcts": summary.band_pcts,
                "total_alphas": summary.total_alphas,
            })
        # Chronological for line chart UX (oldest left, newest right)
        out.sort(key=lambda d: d["_date"] or "")
        return out

    async def get_alpha_health_records(
        self,
        *,
        target: Optional[date] = None,
        bands: Optional[List[str]] = None,
        region: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Filtered drill-down list for the records Table.

        ``bands`` is case-insensitive; passing None means "all bands".
        Empty filter result returns ``{records: [], source: <tag>}``
        (not a 404) so the UI stays consistent.
        """
        result = await self.get_alpha_health(target)
        records = (
            result["payload"].get("records")
            or result["payload"].get("alphas")
            or []
        )
        wanted_bands = (
            {b.upper() for b in bands} if bands else None
        )
        filtered = []
        for r in records:
            if wanted_bands and (r.get("health_band") or "").upper() not in wanted_bands:
                continue
            if region and r.get("region") != region:
                continue
            filtered.append(r)
            if len(filtered) >= limit:
                break
        return {
            "records": filtered,
            "total_unfiltered": len(records),
            "source": result["source"],
        }

    # ==================================================================
    # Phase 2 — Hypothesis Health composers
    # ==================================================================

    @staticmethod
    def _summarize_hypothesis_health(
        payload: Dict[str, Any], source: str,
    ) -> Dict[str, Any]:
        """Compute trigger / score aggregates for the dashboard KPI row.

        We deliberately keep this as a plain dict (rather than a dataclass)
        because the trigger taxonomy is open-ended — new trigger types
        may be added without code changes to OpsService.
        """
        hyps = payload.get("hypotheses") or payload.get("records") or []
        triggered = [h for h in hyps if h.get("is_triggered")]
        # Trigger-type histogram is the data behind the heatmap
        trigger_hist: Counter = Counter()
        for h in triggered:
            detail = h.get("trigger_detail") or {}
            for fired_type in (detail.get("fired") or []):
                trigger_hist[fired_type] += 1
        # thesis_score buckets (0-20, 20-40, ...) for histogram chart
        score_buckets: Counter = Counter()
        for h in hyps:
            s = h.get("thesis_score")
            if s is None:
                continue
            bucket = int(s) // 20 * 20
            score_buckets[f"{bucket}-{bucket + 20}"] += 1
        scored = [h["thesis_score"] for h in hyps
                  if isinstance(h.get("thesis_score"), (int, float))]
        avg = round(sum(scored) / len(scored), 2) if scored else None
        return {
            "report_date": payload.get("report_date"),
            "total_active": len(hyps),
            "total_triggered": len(triggered),
            "avg_thesis_score": avg,
            "trigger_histogram": dict(trigger_hist),
            "score_buckets": dict(score_buckets),
            "source": source,
            "stale_days": payload.get("_stale_days"),
        }

    async def get_hypothesis_health(
        self, target: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Wrap ``hypothesis_health_check`` archive + derive KPI summary."""
        from backend.services.ops_report_reader import OpsReportReader

        reader = OpsReportReader()
        payload, source = await reader.get_or_compute(
            "hypothesis_health_check", target,
        )
        summary = self._summarize_hypothesis_health(payload, source)
        return {"summary": summary, "payload": payload, "source": source}

    async def get_hypothesis_health_history(
        self, days: int = 30,
    ) -> List[Dict[str, Any]]:
        """30-day trend of triggered count + avg score for the line chart."""
        from backend.services.ops_report_reader import OpsReportReader

        reader = OpsReportReader()
        raw_days = await reader.list_recent("hypothesis_health_check", days=days)
        out = []
        for entry in raw_days:
            s = self._summarize_hypothesis_health(entry, source="docs_archived")
            out.append({
                "_date": entry.get("_date"),
                "total_active": s["total_active"],
                "total_triggered": s["total_triggered"],
                "avg_thesis_score": s["avg_thesis_score"],
            })
        out.sort(key=lambda d: d["_date"] or "")
        return out

    async def get_hypothesis_transitions(
        self,
        hypothesis_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Audit rows from ``hypothesis_status_transitions``.

        Direct DB read — the audit table is small (one row per is_triggered
        flip), no aggregation needed. Filterable by hyp_id for the
        per-hypothesis drill-down Drawer.
        """
        from sqlalchemy import desc, select
        from backend.models.transition import HypothesisStatusTransition

        stmt = select(HypothesisStatusTransition).order_by(
            desc(HypothesisStatusTransition.transitioned_at)
        ).limit(max(1, min(limit, 500)))
        if hypothesis_id is not None:
            stmt = stmt.where(
                HypothesisStatusTransition.hypothesis_id == hypothesis_id
            )
        rows = (await self.db.execute(stmt)).scalars().all()
        # HypothesisStatusTransition tracks the is_triggered edge (not status
        # transitions per se — see models/transition.py:49-99 for why).
        return [
            {
                "id": r.id,
                "hypothesis_id": r.hypothesis_id,
                "old_is_triggered": r.old_is_triggered,
                "new_is_triggered": r.new_is_triggered,
                "sharpe_at_transition": r.sharpe_at_transition,
                "reason": r.reason,
                "source": r.source,
                "transitioned_at": (
                    r.transitioned_at.isoformat() if r.transitioned_at else None
                ),
            }
            for r in rows
        ]

    # ==================================================================
    # Phase 2 — Overview composer (fan-out to multiple readers)
    # ==================================================================

    async def get_overview(self) -> Dict[str, Any]:
        """Top-of-dashboard summary — last-night beat status grid +
        per-region health snapshot + top triggers + top pitfalls.

        Designed so a single GET fills the entire /ops/overview page;
        the React side does not chain calls.
        """
        from backend.services.ops_report_reader import OpsReportReader

        reader = OpsReportReader()

        alpha_p, alpha_src = await reader.get_or_compute("alpha_health_check")
        hyp_p, hyp_src = await reader.get_or_compute("hypothesis_health_check")
        pillar_p, pillar_src = await reader.get_or_compute("pillar_balance")
        regime_p, regime_src = await reader.get_or_compute("regime_state")
        neg_p, neg_src = await reader.get_or_compute("negative_knowledge")
        macro_p, macro_src = await reader.get_or_compute("macro_narratives")
        llm_p, llm_src = await reader.get_or_compute("llm_op_monitor")

        alpha_summary = self._summarize_alpha_health(alpha_p, alpha_src)
        hyp_summary = self._summarize_hypothesis_health(hyp_p, hyp_src)

        # Per-region regime tag (Phase 3 page covers details)
        regime_regions = (regime_p.get("regions") or {}) if isinstance(regime_p, dict) else {}
        region_regime = {
            r: data.get("regime") for r, data in regime_regions.items()
            if isinstance(data, dict)
        }

        # Top 5 fired pitfalls — surfaces the most common failure mode
        top_pitfalls = (neg_p.get("top_patterns") or [])[:5]

        return {
            "beat_status": {
                "alpha_health_check": {"source": alpha_src, "date": alpha_p.get("report_date")},
                "hypothesis_health_check": {"source": hyp_src, "date": hyp_p.get("report_date")},
                "pillar_balance": {"source": pillar_src, "date": pillar_p.get("report_date")},
                "regime_infer": {"source": regime_src, "date": regime_p.get("report_date")},
                "negative_knowledge_extract": {"source": neg_src, "date": neg_p.get("report_date")},
                "macro_narrative_extract": {"source": macro_src, "date": macro_p.get("report_date")},
                "llm_op_monitor": {"source": llm_src, "date": llm_p.get("report_date") or llm_p.get("date")},
            },
            "alpha_health_summary": alpha_summary,
            "hypothesis_health_summary": hyp_summary,
            "region_regime": region_regime,
            "top_pitfalls": top_pitfalls,
        }
