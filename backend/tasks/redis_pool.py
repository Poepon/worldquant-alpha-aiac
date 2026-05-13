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
