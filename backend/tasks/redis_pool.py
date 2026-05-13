"""Shared Redis client + cascade-lock helpers.

V-26.4 / V-26.5 / V-26.7 / V-26.27 — replaces the per-task Redis client
that mining_tasks.py used to create with a module-level ConnectionPool,
and replaces the GET+DEL two-step release with a Lua-atomic CAS.

Three reasons this lives in its own module:

1. **Pool reuse** (V-26.7): every cascade dispatch was creating a fresh
   `redis.Redis.from_url(...)` and never closing it. Under task storms
   the connection count drifted up.

2. **Atomic release** (V-26.4): the previous `_release_lock` did
   ``get() -> compare -> delete()`` across two round trips. At the TTL
   boundary another worker could re-acquire between the two calls and
   we'd then delete their lock. The Lua block below runs server-side
   in one operation.

3. **Watchdog can force-clear** (V-26.5): when the watchdog revives a
   session whose worker died between lock acquire and ``finally``,
   the stale lock would still sit for 10800s blocking the replacement.
   `force_clear_cascade_lock` is the explicit override.
"""
from __future__ import annotations

from typing import Optional

import redis
from loguru import logger

from backend.config import settings


# Module-level pool. `decode_responses=False` keeps bytes so the
# existing call sites that already handle bytes-vs-str unchanged.
_pool: Optional[redis.ConnectionPool] = None


def get_redis_client() -> redis.Redis:
    """Return the shared Redis client. Lazily builds the pool on first use
    so import order doesn't matter for the celery worker."""
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=32,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
    return redis.Redis(connection_pool=_pool)


# Lua: only DELETE if the stored value still matches our token.
# Returns 1 if deleted, 0 if the key vanished or held a different token.
# `redis.call` runs atomically inside the server, so there is no window
# between the GET-compare and the DEL where another worker can sneak in.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def acquire_cascade_lock(key: str, token: str, ttl_sec: int) -> bool:
    """SET NX EX. Returns True on acquisition, False if another worker
    already holds it. Raises on Redis errors so callers can decide
    whether to fail-closed (V-26.27)."""
    cli = get_redis_client()
    return bool(cli.set(key, token, nx=True, ex=ttl_sec))


def release_cascade_lock(key: str, token: str) -> bool:
    """Atomic check-and-delete using server-side Lua. Returns True iff
    we actually held the lock and just released it. Swallows redis-side
    exceptions (TTL will clean up); raising here would break the
    caller's ``finally``."""
    try:
        cli = get_redis_client()
        result = cli.eval(_RELEASE_LUA, 1, key, token)
        return bool(result)
    except Exception as exc:
        logger.warning(
            f"[cascade-lock] Lua release failed for key={key}: {exc} "
            f"(TTL will reclaim eventually)"
        )
        return False


def force_clear_cascade_lock(key: str) -> bool:
    """Watchdog-only: DELETE regardless of token. Use when reviving a
    task whose original worker is presumed dead and we need to evict
    the stale lock so the replacement worker can ``acquire`` cleanly.

    Returns True if the key existed and was removed."""
    try:
        cli = get_redis_client()
        return bool(cli.delete(key))
    except Exception as exc:
        logger.warning(f"[cascade-lock] force-clear failed for key={key}: {exc}")
        return False


def peek_lock_holder(key: str) -> Optional[str]:
    """Read the current lock holder token (for logging). None if Redis
    is unreachable or the key is absent."""
    try:
        cli = get_redis_client()
        held = cli.get(key)
        if held is None:
            return None
        return held.decode() if isinstance(held, bytes) else str(held)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# V-26.17 — cross-worker error knowledge base for SELF_CORRECT learning
# ---------------------------------------------------------------------------
#
# Pre-fix the SELF_CORRECT node kept its "what fix worked for what error"
# memory in a module-level Python list (validation._ERROR_KNOWLEDGE_BASE).
# That meant:
#   - worker restart wiped accumulated corrections
#   - watchdog revive started from zero learning state
#   - parallel celery workers couldn't share lessons
#   - the 100-entry cap dropped the *oldest* half FIFO with no quality signal
#
# Redis LIST storage fixes the first three. Cap stays simple (LTRIM
# right side, oldest dropped) — a quality-aware eviction is its own
# follow-up.
import json as _json

_ERROR_KB_KEY = "agent:error_kb:corrections"
_ERROR_KB_MAX = 200  # cap; LTRIM trims oldest


def error_kb_record(entry: dict) -> bool:
    """LPUSH a correction entry. Returns True on success."""
    try:
        cli = get_redis_client()
        payload = _json.dumps(entry, ensure_ascii=False, default=str)
        cli.lpush(_ERROR_KB_KEY, payload)
        cli.ltrim(_ERROR_KB_KEY, 0, _ERROR_KB_MAX - 1)
        return True
    except Exception as exc:
        logger.warning(f"[error_kb] record failed: {exc}")
        return False


def error_kb_load(max_entries: int = 100) -> list:
    """Return the most-recent corrections (newest first). On Redis error
    returns an empty list — caller treats it as cold KB and proceeds.
    SELF_CORRECT only uses these as LLM in-context examples so missing
    them degrades quality, not correctness."""
    try:
        cli = get_redis_client()
        raw = cli.lrange(_ERROR_KB_KEY, 0, max_entries - 1)
    except Exception as exc:
        logger.warning(f"[error_kb] load failed: {exc}")
        return []
    out = []
    for b in raw:
        try:
            s = b.decode() if isinstance(b, bytes) else b
            out.append(_json.loads(s))
        except Exception:
            continue
    return out


def error_kb_size() -> int:
    """Current LIST length. Used by audit / monitoring scripts."""
    try:
        cli = get_redis_client()
        return int(cli.llen(_ERROR_KB_KEY) or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# V-26.84 — IQC audit in-flight lock for sweep idempotency
# ---------------------------------------------------------------------------
#
# Pre-fix `iqc_audit_backfill_sweep` re-enqueued every alpha that hadn't
# yet landed its `_iqc_marginal` metric, every beat tick. If a previous
# enqueue was still pending in celery (worker backlog, BRAIN slow) the
# alpha got piled on again, multiplying the BRAIN load. Lock with a TTL
# slightly longer than the longest expected audit (10 min) so a worker
# crash doesn't trap the alpha forever; subsequent sweep ticks will
# eventually re-attempt after the lock expires.

_IQC_AUDIT_LOCK_TTL_SEC = 600


def claim_iqc_audit_lock(alpha_pk: int) -> bool:
    """SET NX EX. Returns True iff this caller now owns the audit slot."""
    try:
        cli = get_redis_client()
        key = f"iqc_audit:inflight:{alpha_pk}"
        return bool(cli.set(key, "1", nx=True, ex=_IQC_AUDIT_LOCK_TTL_SEC))
    except Exception as exc:
        logger.warning(f"[iqc_audit_lock] claim failed for pk={alpha_pk}: {exc}")
        # Fail-open: if Redis is down we'd rather over-enqueue than starve
        # audits. Sweep cap already bounds the blast radius.
        return True


def release_iqc_audit_lock(alpha_pk: int) -> None:
    """Best-effort DELETE after audit_iqc_marginal_for_alpha completes
    (success or failure). TTL is a safety net for unreleased locks."""
    try:
        cli = get_redis_client()
        cli.delete(f"iqc_audit:inflight:{alpha_pk}")
    except Exception:
        pass
