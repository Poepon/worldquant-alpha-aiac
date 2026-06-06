"""Pool worker process entry (Phase 1b B6).

    python -m backend.pool.run_worker {hg|s|e}

The thin CLI the supervisor launches as a resident subprocess. Runs the role's
loop until SIGTERM/SIGINT (graceful: finishes the in-flight candidate, then the
loop's should_stop breaks) or the process is force-terminated (the in-flight
row's lease expires → lease-recycle reclaims it). Exits immediately if
ENABLE_POOL_PIPELINE is OFF, so an accidental launch in the FLAT-only world is
a no-op.
"""
import asyncio
import os
import signal
import sys

from loguru import logger

from backend.config import settings
from backend.pool.workers import e_loop, hg_loop, s_loop

_LOOPS = {"hg": hg_loop, "s": s_loop, "e": e_loop}
_stop = {"v": False}


def _request_stop(*_a) -> None:
    _stop["v"] = True


async def _run_warmed(role: str, worker_id: str) -> None:
    """Warm the feature-flag override cache from the DB BEFORE the first node runs,
    then run the role loop.

    Pool workers are STANDALONE processes (not Celery / not FastAPI), so they never
    receive the worker_process_init / lifespan feature-flag refresher. Without this
    warm, this process's ``_flag_override_cache`` is empty → LLM_FUNCTION_MODEL_MAP
    is unset → every LLM call falls back to the DEAD ``gpt-4`` @ api.openai.com
    default (empty key) → 401 → 0 candidates. (It also makes every ENABLE_ flag read
    the config default instead of the live DB value.) Mirrors celery_app's
    start_sync_refresher / main.py's start_async_refresher."""
    try:
        from backend.feature_flag_runtime import _async_refresh_once, start_async_refresher
        await _async_refresh_once()   # blocking initial warm — cache ready before first claim
        start_async_refresher()       # keep it fresh every REFRESH_INTERVAL_SEC in this loop
        logger.info(f"[pool.{role}] feature-flag cache warmed (LLM routing live)")
    except Exception as ex:  # noqa: BLE001 — warm failure must not block the worker
        logger.warning(f"[pool.{role}] feature-flag cache warm failed (continuing): {ex}")
    await _LOOPS[role](worker_id=worker_id, should_stop=lambda: _stop["v"])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _LOOPS:
        print("usage: python -m backend.pool.run_worker {hg|s|e}", file=sys.stderr)
        return 2
    role = argv[0]
    if not bool(getattr(settings, "ENABLE_POOL_PIPELINE", False)):
        logger.info(f"[pool.{role}] ENABLE_POOL_PIPELINE OFF — exiting")
        return 0

    worker_id = f"{role}-{os.getpid()}"
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_stop)
        except Exception:  # noqa: BLE001 — not all signals settable on Windows
            pass

    logger.info(f"[pool.{role}] worker {worker_id} starting")
    try:
        asyncio.run(_run_warmed(role, worker_id))
    except KeyboardInterrupt:
        pass
    logger.info(f"[pool.{role}] worker {worker_id} stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
