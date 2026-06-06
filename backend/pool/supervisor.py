"""Pool supervisor (Phase 1b B6) — Popen-respawn parent.

Launches POOL_N_HG HG + POOL_K_S S + POOL_K_E E worker subprocesses
(``python -m backend.pool.run_worker <role>``), respawns dead ones (rate-limited
by POOL_RESPAWN_BACKOFF_SEC to avoid crash-loop spin), PARKS a role (no respawn)
while ``pool:{role}:drain`` is set, and reconciles ``pool:workers:alive`` — the
SET the celery_app worker_process_init guarded reset reads to avoid zeroing a
shared brain:concurrent_sims while sibling sims are in flight.

The beat can only Stop-Process, not launch (run.bat uses cmd /k, no supervisor) —
this IS the launcher (plan §6 #4: Popen-respawn over NSSM for cross-platform
dev). Idle if ENABLE_POOL_PIPELINE is OFF.

``reconcile_once`` is the testable core (inject spawn_fn / now_fn / redis_fn);
``run`` is the thin poll loop. INERT until ENABLE_POOL_PIPELINE.
"""
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from backend.config import settings
from backend.pool.drain import is_draining

_ROLES = ("hg", "s", "e")
_REGISTRY_KEY = "pool:workers:alive"


def _default_spawn(role: str) -> Any:
    return subprocess.Popen([sys.executable, "-m", "backend.pool.run_worker", role])


def _role_targets() -> Dict[str, int]:
    return {
        "hg": int(getattr(settings, "POOL_N_HG", 1)),
        "s": int(getattr(settings, "POOL_K_S", 2)),
        "e": int(getattr(settings, "POOL_K_E", 1)),
    }


class PoolSupervisor:
    def __init__(self, *, spawn_fn: Optional[Callable[[str], Any]] = None,
                 now_fn: Optional[Callable[[], float]] = None,
                 redis_fn: Optional[Callable[[], Any]] = None,
                 draining_fn: Optional[Callable[[str], bool]] = None):
        self._spawn = spawn_fn or _default_spawn
        self._now = now_fn or time.monotonic
        self._redis_fn = redis_fn
        self._draining = draining_fn or is_draining
        self._procs: Dict[str, List[Dict[str, Any]]] = {r: [] for r in _ROLES}
        # None = never spawned (distinct from now()==0.0 on the first poll, which
        # would otherwise re-trigger the "fresh, ignore backoff" path).
        self._last_spawn: Dict[str, Optional[float]] = {r: None for r in _ROLES}
        self._stop = False

    @staticmethod
    def _alive(handle: Dict[str, Any]) -> bool:
        return handle["proc"].poll() is None

    def reconcile_once(self) -> List[str]:
        """Prune dead, respawn to target (unless draining / within backoff),
        update the alive registry. Returns the alive worker ids."""
        targets = _role_targets()
        backoff = float(getattr(settings, "POOL_RESPAWN_BACKOFF_SEC", 10.0))
        now = self._now()
        alive_ids: List[str] = []
        for role in _ROLES:
            self._procs[role] = [h for h in self._procs[role] if self._alive(h)]
            draining = bool(self._draining(role))
            need = targets[role] - len(self._procs[role])
            if need > 0 and not draining:
                fresh = self._last_spawn[role] is None
                if fresh or (now - self._last_spawn[role]) >= backoff:
                    for _ in range(need):
                        proc = self._spawn(role)
                        self._procs[role].append(
                            {"proc": proc, "id": f"{role}-{getattr(proc, 'pid', '?')}"})
                    self._last_spawn[role] = now
            alive_ids.extend(h["id"] for h in self._procs[role])
        self._update_registry(alive_ids)
        return alive_ids

    def _redis(self):
        if self._redis_fn is not None:
            return self._redis_fn()
        from backend.tasks.redis_pool import get_redis_client
        return get_redis_client()

    def _update_registry(self, alive_ids: List[str]) -> None:
        # ATOMIC delete+sadd in one MULTI/EXEC: a plain delete-then-sadd leaves a
        # window where scard(pool:workers:alive)==0, and celery_app's guarded
        # brain:concurrent_sims reset reads exactly this scard — a worker_process_
        # init landing in that window would zero the shared sim-slot counter while
        # live sims still hold slots → over-cap 429 cascade (the 2026-05-31 leak the
        # guard exists to prevent). The pipeline makes the rewrite indivisible.
        try:
            r = self._redis()
            pipe = r.pipeline(transaction=True)
            pipe.delete(_REGISTRY_KEY)
            if alive_ids:
                pipe.sadd(_REGISTRY_KEY, *alive_ids)
            pipe.execute()
        except Exception as ex:  # noqa: BLE001 — registry is advisory
            logger.debug(f"[pool.supervisor] registry update failed: {ex}")

    def terminate_all(self) -> None:
        for role in _ROLES:
            for h in self._procs[role]:
                try:
                    h["proc"].terminate()
                except Exception:  # noqa: BLE001
                    pass
            self._procs[role] = []
        self._update_registry([])

    def run(self) -> None:  # pragma: no cover - long-running loop
        if not bool(getattr(settings, "ENABLE_POOL_PIPELINE", False)):
            logger.info("[pool.supervisor] ENABLE_POOL_PIPELINE OFF — idle, not launching workers")
            return
        poll = float(getattr(settings, "POOL_SUPERVISOR_POLL_SEC", 5.0))
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, lambda *_a: setattr(self, "_stop", True))
            except Exception:  # noqa: BLE001
                pass
        logger.info(f"[pool.supervisor] starting (targets={_role_targets()})")
        try:
            while not self._stop:
                self.reconcile_once()
                time.sleep(poll)
        finally:
            self.terminate_all()
            logger.info("[pool.supervisor] stopped, workers terminated")


def main() -> int:  # pragma: no cover
    PoolSupervisor().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
