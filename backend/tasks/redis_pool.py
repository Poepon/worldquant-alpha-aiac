"""Shared Redis client + small Redis-backed helpers.

Provides a module-level ConnectionPool ``get_redis_client`` reused across the
codebase (error-KB for SELF_CORRECT learning, IQC-audit lock, simulate-slot
semaphore, etc.).

NOTE (Phase 1c-delete): the cascade-lock helpers (acquire/release/renew/verify/
peek_cascade_lock + the JSON lock-value codec) were removed with the FLAT
cascade path — they had no caller once mining_tasks.py was deleted.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# V-27.81 — simulate dedup in-flight lock
# ---------------------------------------------------------------------------
#
# filter_unsimulated_expressions (selection_strategy.py) SELECTs which
# expression hashes already exist in `alphas`, but between that SELECT and
# the actual brain.simulate_alpha() call another worker can simulate the
# same (hash, region, universe) — both burn a BRAIN slot for one alpha.
# This lock makes "I'm about to simulate this expression" visible across
# workers: SET NX EX before simulate, DELETE after (success or failure);
# the TTL reclaims it if the worker dies mid-simulate.

_SIMULATE_DEDUP_LOCK_TTL_SEC = 900  # > slowest single BRAIN simulate


def _simulate_lock_key(expr_hash: str, region: str, universe: str) -> str:
    return f"sim_dedup:{region}:{universe}:{expr_hash}"


def claim_simulate_slot(
    expr_hash: str, region: str, universe: str
) -> Optional[str]:
    """SET NX EX with a per-claim random token. Returns the token string iff
    this caller now owns the simulate slot for (expr_hash, region, universe),
    or None if another worker already holds it.

    V-27.81 followup: the slot value used to be a constant "1", so
    release_simulate_slot was a blind DELETE — if this slot's 900s TTL
    expired and another worker re-claimed, the original worker's release
    would delete the new holder's slot (the V-26.4 blind-delete pattern).
    The token lets release do a Lua CAS instead. Callers must keep the
    returned token and hand it back to release_simulate_slot.

    Fail-OPEN on Redis error (returns a token == pre-V-27.81 "proceed"
    behaviour): a Redis outage must never block simulation. The `alpha_id`
    unique constraint still bounds data correctness if a duplicate slips
    through. The returned token is unusable (the SET never landed) but
    release's CAS will simply no-op, which is harmless."""
    token = uuid.uuid4().hex
    try:
        cli = get_redis_client()
        got = cli.set(
            _simulate_lock_key(expr_hash, region, universe),
            token, nx=True, ex=_SIMULATE_DEDUP_LOCK_TTL_SEC,
        )
        return token if got else None
    except Exception as exc:
        logger.warning(f"[sim_dedup_lock] claim failed (fail-open): {exc}")
        return token


def release_simulate_slot(
    expr_hash: str, region: str, universe: str, token: str
) -> None:
    """Best-effort CAS release after simulate completes (success OR failure).
    Deletes the slot only if it still holds `token` (server-side Lua), so a
    worker whose TTL already expired and was re-claimed by someone else does
    not delete the new holder's slot. TTL is the safety net for an
    unreleased slot."""
    try:
        get_redis_client().eval(
            _RELEASE_LUA_V2, 1,
            _simulate_lock_key(expr_hash, region, universe), token,
        )
    except Exception:
        pass
