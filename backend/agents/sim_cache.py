"""Phase 3 R9 BRAIN simulate result cache (2026-05-18).

Pure-helpers + ``cached_simulate_batch`` wrapper around
``BrainAdapter.simulate_batch`` that:
  1. Computes per-expression cache key from (region, universe, expression,
     canonical-settings).
  2. Partitions inputs into (cached, uncached).
  3. Calls real ``brain.simulate_batch`` for uncached only.
  4. Persists fresh results (success-only by default) to ``simulation_cache``.
  5. Reassembles in original order.

Soft-fail: any cache DB error → fall back to direct BRAIN call. NEVER
blocks a sim. Mirrors r5_judge / family_classifier soft-fail philosophy.

Per master plan §4.5 R9 (1-2/3 人日; this PR ships the read/write/integration
in one go since scope is crisp).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings as _settings
from backend.models.simulation_cache import SimulationCache

logger = logging.getLogger(__name__)


# Settings keys that influence sim result — must match BrainAdapter
# simulate_batch kwargs to keep cache scope correct.
SETTINGS_KEYS = (
    "delay", "decay", "neutralization", "truncation", "test_period",
)


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _canonical_settings(settings_dict: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Project only known sim-settings keys + sort for stable JSON."""
    s = settings_dict or {}
    return {k: s.get(k) for k in SETTINGS_KEYS}


def compute_cache_key(
    *,
    region: str,
    universe: str,
    expression: str,
    settings: Optional[Dict[str, Any]],
) -> str:
    """sha256(canonical-inputs)[:64] — stable across processes."""
    canonical = json.dumps(
        {
            "region": (region or "").upper(),
            "universe": universe or "",
            "expression": expression or "",
            "settings": _canonical_settings(settings),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]


def _expression_hash(expression: str) -> str:
    return hashlib.sha256((expression or "").encode("utf-8")).hexdigest()[:64]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def get_cached(
    db: AsyncSession,
    cache_key: str,
    *,
    ttl_days: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Look up + return cached result_json, or None on miss / expired / error.

    On hit: bumps ``accessed_at`` + ``access_count`` (best-effort,
    soft-fail). On miss / expired: returns None. On any DB error: returns
    None + log warn.

    NOTE — requires a fresh / dedicated AsyncSession. The access-stats path
    issues ``await db.commit()`` to persist the accessed_at/access_count
    bump, which would clobber any in-flight transaction on a request-scoped
    session. Call sites (``cached_simulate_batch``) construct a session
    from ``AsyncSessionLocal`` for exactly this reason — do the same if you
    add a new caller.
    """
    try:
        ttl = int(ttl_days if ttl_days is not None
                  else getattr(_settings, "SIMULATION_CACHE_TTL_DAYS", 14))
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, ttl))
        row = (await db.execute(
            select(SimulationCache).where(SimulationCache.cache_key == cache_key)
        )).scalar_one_or_none()
        if row is None or (row.cached_at and row.cached_at < cutoff):
            # MISS / EXPIRED: the SELECT autobegan a read transaction. Without
            # this rollback the read txn lingers idle-in-transaction on the
            # caller's session until it's next committed/closed — one of the
            # idle-in-txn leak sources surfaced 2026-06-05 (see
            # reference_flat_idle_in_txn_lock_leak). rollback is safe (read-only
            # on this path) and releases the connection promptly.
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None  # expired rows left on disk for forensic / re-warm
        # Update access stats (best-effort)
        try:
            row.accessed_at = datetime.now(timezone.utc)
            row.access_count = (row.access_count or 0) + 1
            await db.commit()
        except Exception as ex:
            logger.debug(f"[sim_cache] access-stats commit failed (non-fatal): {ex}")
            try:
                await db.rollback()
            except Exception:
                pass
        return dict(row.result_json) if isinstance(row.result_json, dict) else None
    except Exception as ex:
        logger.warning(f"[sim_cache] get_cached failed (fall back): {ex}")
        return None


async def set_cached(
    db: AsyncSession,
    *,
    cache_key: str,
    region: str,
    universe: str,
    expression: str,
    settings: Optional[Dict[str, Any]],
    result: Dict[str, Any],
) -> bool:
    """Insert or update a cache row. Returns True on success, False on error.

    Respects ``SIMULATION_CACHE_ONLY_SUCCESS``: when True (default), only
    caches results with ``result.success == True`` (avoid pinning transient
    errors). Soft-fail on DB error.

    Bug-#7 fix (2026-05-18): race-free Postgres ``INSERT ... ON CONFLICT
    (cache_key) DO UPDATE`` replaces the prior SELECT-then-INSERT path. Two
    concurrent workers that both missed in :func:`get_cached` for the same
    expression now collapse to a single winning INSERT; the loser hits the
    DO UPDATE branch which only bumps ``access_count`` + ``accessed_at`` and
    deliberately KEEPS the first writer's ``result_json`` / ``cached_at`` /
    ``success`` / ``expression`` / ``settings_json`` (no clobber). Either
    way no UNIQUE-violation rollback churn.
    """
    try:
        if getattr(_settings, "SIMULATION_CACHE_ONLY_SUCCESS", True):
            if not bool(result.get("success", False)):
                return False
        now = datetime.now(timezone.utc)
        values = {
            "cache_key": cache_key,
            "region": (region or "").upper(),
            "universe": universe or "",
            "expression": expression,
            "expression_hash": _expression_hash(expression),
            "settings_json": _canonical_settings(settings),
            "result_json": result,
            "success": bool(result.get("success", False)),
            "cached_at": now,
            "accessed_at": now,
            "access_count": 1,
        }
        stmt = pg_insert(SimulationCache).values(**values)
        # On conflict: keep the first writer's payload — only bump access
        # stats. Mirrors get_cached's hit-path bookkeeping. We intentionally
        # do NOT re-warm cached_at on a conflicting INSERT: doing so would
        # extend the TTL for free on every duplicate INSERT and break the
        # "freshly cached vs. recently accessed" distinction. Re-warming is
        # the caller's job (do a fresh set after an explicit invalidate).
        stmt = stmt.on_conflict_do_update(
            index_elements=["cache_key"],
            set_={
                "access_count": SimulationCache.access_count + 1,
                "accessed_at": now,
            },
        )
        await db.execute(stmt)
        await db.commit()
        return True
    except Exception as ex:
        logger.warning(f"[sim_cache] set_cached failed (non-fatal): {ex}")
        try:
            await db.rollback()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Main wrapper API
# ---------------------------------------------------------------------------

async def cached_simulate_batch(
    db: AsyncSession,
    brain,                # BrainAdapter
    *,
    expressions: List[str],
    region: str = "USA",
    universe: str = "TOP3000",
    delay: int = 1,
    decay: int = 4,
    neutralization: str = "SUBINDUSTRY",
    truncation: float = 0.08,
    test_period: str = "P2Y0M",
) -> List[Dict[str, Any]]:
    """Cached wrapper around BrainAdapter.simulate_batch.

    Per master plan §4.5 R9 algorithm:
      1. Compute cache_key for each expression
      2. Look up cached results (parallelizable but sequential here for simplicity)
      3. Partition: cached_results[i] is dict if hit else None; uncached_indices
         = [i for i, r in enumerate(cached) if r is None]
      4. Call brain.simulate_batch only for uncached expressions
      5. Cache new results
      6. Reassemble in original order
    """
    if not expressions:
        return []
    settings_dict = {
        "delay": delay,
        "decay": decay,
        "neutralization": neutralization,
        "truncation": truncation,
        "test_period": test_period,
    }

    # Step 1+2: compute keys + lookup cached
    cache_keys: List[str] = []
    cached_results: List[Optional[Dict[str, Any]]] = []
    for expr in expressions:
        key = compute_cache_key(
            region=region, universe=universe, expression=expr,
            settings=settings_dict,
        )
        cache_keys.append(key)
        hit = await get_cached(db, key)
        cached_results.append(hit)

    # Step 3: partition
    uncached_indices = [i for i, r in enumerate(cached_results) if r is None]
    hit_count = len(expressions) - len(uncached_indices)
    if hit_count:
        logger.info(
            f"[sim_cache] {hit_count}/{len(expressions)} cache hits, "
            f"{len(uncached_indices)} BRAIN sims to run"
        )

    # Phase 4 PR0.6 (Sprint 0, 2026-05-19) — F-A1 fix (2026-05-19 review):
    # Stamp must land inside ``result["metrics"]`` (the nested dict that
    # evaluation.py:1267 propagates to ``alpha.metrics``), NOT at the top
    # level — top-level keys are dropped when ``updated.metrics =
    # res.get("metrics", {}) or {}`` runs. Use the final stamp name
    # ``_simulation_cache_hit`` so evaluation doesn't need to rename.
    for _r in cached_results:
        if not isinstance(_r, dict):
            continue
        _m = _r.get("metrics")
        if not isinstance(_m, dict):
            _m = {}
            _r["metrics"] = _m
        _m["_simulation_cache_hit"] = True

    if not uncached_indices:
        # 100% cache hit — return all cached
        return [r for r in cached_results if r is not None]

    # Step 4: call BRAIN for uncached
    uncached_exprs = [expressions[i] for i in uncached_indices]
    try:
        brain_results = await brain.simulate_batch(
            expressions=uncached_exprs,
            region=region, universe=universe,
            delay=delay, decay=decay,
            neutralization=neutralization, truncation=truncation,
            test_period=test_period,
        )
    except Exception as ex:
        # BRAIN failure — return cached + failures for uncached
        logger.error(f"[sim_cache] BRAIN simulate_batch failed: {ex}")
        brain_results = [
            {"success": False, "error": f"sim_failed: {ex}"}
            for _ in uncached_exprs
        ]

    # Step 5: cache new (successful) results
    for offset, idx in enumerate(uncached_indices):
        if offset < len(brain_results):
            await set_cached(
                db,
                cache_key=cache_keys[idx],
                region=region, universe=universe,
                expression=expressions[idx],
                settings=settings_dict,
                result=brain_results[offset],
            )

    # Step 6: reassemble in original order
    final: List[Dict[str, Any]] = []
    brain_iter = iter(brain_results)
    for i, hit in enumerate(cached_results):
        if hit is not None:
            final.append(hit)
        else:
            try:
                final.append(next(brain_iter))
            except StopIteration:
                final.append({"success": False, "error": "missing_brain_result"})
    return final


__all__ = [
    "SETTINGS_KEYS",
    "compute_cache_key",
    "get_cached",
    "set_cached",
    "cached_simulate_batch",
]
