"""OpsService — orchestration layer for /api/v1/ops/* endpoints.

Source: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.1, §1.6.

Sits between the ``ops`` router and the various P0/P1/P2 services. Holds
the cross-cutting concerns:

* manual Celery task triggers — whitelist + per-task and global throttling
* "recent runs" lookup against the Celery result backend
* (Phase 2/3) high-level page composers like ``get_overview()`` that fan
  out to several services + the OpsReportReader

Constructed once per request by ``Depends(get_ops_service)`` in the
router. Most methods only need the Celery app + Redis; the AsyncSession
is wired through so child services can use it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
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
