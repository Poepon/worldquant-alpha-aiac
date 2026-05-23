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

3. **Stale-lock recovery via TTL**: if a worker dies between lock acquire
   and its ``finally`` release, the lock self-expires after the TTL
   (CASCADE_LOCK_TTL_SEC, default 10800s), after which a replacement can
   re-acquire. (The watchdog force-clear / takeover overrides were removed
   with cascade retirement, 2026-05-24.)
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


# V-27.1: the lock value is now a structured JSON blob instead of a bare
# token string:
#   {"token", "run_id", "worker_pid", "acquired_at", "lineage", "v": 2}
# `token` is still the CAS identity; the rest is diagnostic only (under
# Celery --pool=solo a pid can't prove liveness, so it's log-only). A bare
# string left by a pre-V-27.1 worker is treated as an implicit v1 token —
# every lock-read path goes through _decode_lock_value for compatibility.

# Lua: only DELETE if the stored value's token still matches ours. Decodes
# the JSON value; a value that isn't our JSON shape is treated wholesale as
# a legacy token. `redis.call` runs atomically server-side, so there's no
# window between the GET-compare and the DEL where another worker sneaks in.
_RELEASE_LUA_V2 = """
local cur = redis.call('get', KEYS[1])
if cur == false then return 0 end
local tok
local ok, decoded = pcall(cjson.decode, cur)
if ok and type(decoded) == 'table' and decoded.token ~= nil then
    tok = decoded.token
else
    tok = cur
end
if tok == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""

# Lua: CAS-EXPIRE — extend the TTL only if the stored value's token still
# matches ours. Same decode logic as _RELEASE_LUA_V2 (v2 JSON or legacy bare
# string). Returns 1 if the TTL was reset, 0 if the key vanished or is held
# by someone else. V-27.1 followup: lets a live long-running worker renew its
# lock at every round boundary instead of letting a fixed TTL expire under it.
_RENEW_LUA_V2 = """
local cur = redis.call('get', KEYS[1])
if cur == false then return 0 end
local tok
local ok, decoded = pcall(cjson.decode, cur)
if ok and type(decoded) == 'table' and decoded.token ~= nil then
    tok = decoded.token
else
    tok = cur
end
if tok == ARGV[1] then return redis.call('expire', KEYS[1], tonumber(ARGV[2])) else return 0 end
"""


def _encode_lock_value(
    token: str,
    *,
    run_id: Optional[int] = None,
    worker_pid: Optional[int] = None,
    lineage: str = "WORKER",
) -> str:
    """Serialize a structured (v2) lock value. `token` stays the CAS
    identity; run_id / worker_pid / acquired_at / lineage are diagnostic."""
    return json.dumps(
        {
            "token": token,
            "run_id": run_id,
            "worker_pid": worker_pid,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "lineage": lineage,
            "v": 2,
        }
    )


def _decode_lock_value(raw) -> Optional[dict]:
    """Normalize a raw redis lock value into a dict carrying at least
    'token'. Returns None if `raw` is None. Handles three shapes:

      - v2 JSON dict with a 'token' field      → parsed dict as-is
      - legacy bare string (pre-V-27.1, v1)    → {'token': raw, 'v': 1, ...}
      - JSON but not our shape (no 'token')    → treated as legacy, the
                                                 whole raw string is token

    This is the single backward-compatibility choke point — every path
    that reads a lock value must go through it."""
    if raw is None:
        return None
    s = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return {"token": s, "v": 1, "lineage": "LEGACY"}
    if isinstance(obj, dict) and obj.get("token") is not None:
        return obj
    return {"token": s, "v": 1, "lineage": "LEGACY"}


def acquire_cascade_lock(
    key: str,
    token: str,
    ttl_sec: int,
    *,
    run_id: Optional[int] = None,
    worker_pid: Optional[int] = None,
) -> bool:
    """SET NX EX. Returns True on acquisition, False if another worker
    already holds it. Raises on Redis errors so callers can decide
    whether to fail-closed (V-26.27).

    V-27.1: the stored value is now a structured JSON blob (see
    _encode_lock_value); `token` remains the CAS identity."""
    cli = get_redis_client()
    value = _encode_lock_value(
        token, run_id=run_id, worker_pid=worker_pid, lineage="WORKER"
    )
    return bool(cli.set(key, value, nx=True, ex=ttl_sec))


def release_cascade_lock(key: str, token: str) -> bool:
    """Atomic check-and-delete using server-side Lua. Returns True iff
    we actually held the lock (token match, v2 or legacy value) and just
    released it. Swallows redis-side exceptions (TTL will clean up);
    raising here would break the caller's ``finally``.

    V-27.1: after a watchdog takeover this is a deliberate no-op — the
    old worker calls release with its stale token, the Lua CAS sees the
    new watchdog token and returns 0 without deleting. The old worker
    cannot clobber the replacement's lock."""
    try:
        cli = get_redis_client()
        result = cli.eval(_RELEASE_LUA_V2, 1, key, token)
        return bool(result)
    except Exception as exc:
        logger.warning(
            f"[cascade-lock] Lua release failed for key={key}: {exc} "
            f"(TTL will reclaim eventually)"
        )
        return False


def renew_cascade_lock(key: str, token: str, ttl_sec: int) -> bool:
    """V-27.1 followup: extend the lock TTL iff `token` still owns it
    (server-side Lua CAS-EXPIRE). Returns True iff the TTL was renewed.

    Why this exists: CASCADE_LOCK_TTL_SEC defaults to 3h but a
    CONTINUOUS_CASCADE worker runs indefinitely. Without renewal the lock
    expires under a perfectly healthy worker, which then self-terminates on
    the next round-boundary ownership check (MISSING) — and worse, the
    watchdog can take over the freed lock while the old worker is still
    mid-round. Call this at every round boundary alongside the ownership
    check.

    Swallows Redis errors → False: the ownership check that runs alongside
    is the real safety gate; a transient Redis blip here must not crash a
    live worker."""
    try:
        cli = get_redis_client()
        return bool(cli.eval(_RENEW_LUA_V2, 1, key, token, int(ttl_sec)))
    except Exception as exc:
        logger.warning(f"[cascade-lock] renew failed for key={key}: {exc}")
        return False


def verify_lock_ownership(key: str, token: str) -> str:
    """Check whether `token` still owns the lock at `key`. Returns:

      - "OWNED"   : stored value's token matches `token`
      - "LOST"    : the key holds a different token (taken over)
      - "MISSING" : the key does not exist (TTL expired / cleared)
      - "UNKNOWN" : Redis was unreachable

    SAFETY FLOOR (V-27.1): the caller MUST NOT treat "UNKNOWN" as "LOST".
    A Redis blip must never make a live worker self-terminate — if every
    cascade worker self-killed on a transient Redis error that would be
    strictly worse than the original double-run bug."""
    try:
        cli = get_redis_client()
        held = cli.get(key)
    except Exception as exc:
        logger.warning(
            f"[cascade-lock] ownership check failed for key={key}: {exc} "
            f"(returning UNKNOWN — caller must keep running)"
        )
        return "UNKNOWN"
    if held is None:
        return "MISSING"
    decoded = _decode_lock_value(held)
    if decoded and decoded.get("token") == token:
        return "OWNED"
    return "LOST"


def peek_lock_holder(key: str) -> Optional[dict]:
    """Read + decode the current lock value (for logging / diagnostics).
    Returns the structured dict (see _decode_lock_value), or None if Redis
    is unreachable or the key is absent.

    V-27.1: return type changed from Optional[str] to Optional[dict] —
    callers that want the bare token should read ``(holder or {}).get('token')``."""
    try:
        cli = get_redis_client()
        held = cli.get(key)
        return _decode_lock_value(held)
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
