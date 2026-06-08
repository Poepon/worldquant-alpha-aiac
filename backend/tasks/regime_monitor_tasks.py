"""Regime-turn monitor beat (2026-06-07, greenfield branch B).

Production is PAUSED in a regime trough (pool drained + ENABLE_POOL_PIPELINE off).
This cheap periodic probe re-sims the submitted-pool winners (+ a backlog sample)
on CURRENT BRAIN data (rolling test_period, NOT the frozen 2019-2023 window) and
asks: have the old edges recovered? If yes → the regime may be turning → re-engage.

GATES on ENABLE_REGIME_MONITOR (default OFF, hot-toggle via SUPPORTED_FLAGS). No-op
when OFF. Persists the latest snapshot + a capped history to Redis (no schema /
Alembic) for the ops surface to read. Reuses the tested BrainSimulator
(slot-throttled, timeout-guarded) so the BRAIN path can't drift from the pool's.

口径 = current IS (regime-decay sensor), NOT OS — see backend/regime_monitor.py.
"""
import json
from datetime import datetime, timezone

from loguru import logger

from backend.celery_app import celery_app
from backend.config import settings
from backend.tasks import run_async


_LATEST_KEY = "regime_monitor:latest"
_HISTORY_KEY = "regime_monitor:history"
_HISTORY_CAP = 90


def _on() -> bool:
    return bool(getattr(settings, "ENABLE_REGIME_MONITOR", False))


async def _load_probe_specs(db, backlog_n: int):
    """13 submitted (the core sensor) + top-N backlog by Sharpe (broader sensor).
    Each spec carries expression + structural settings columns + baseline Sharpe."""
    from sqlalchemy import text as _text
    cols = (
        "alpha_id, expression, region, universe, delay, decay, neutralization, "
        "truncation, (metrics->>'sharpe')::float AS baseline_sharpe"
    )
    sub = (await db.execute(_text(
        f"SELECT {cols} FROM alphas WHERE date_submitted IS NOT NULL"
    ))).mappings().all()
    bk = (await db.execute(_text(
        f"SELECT {cols} FROM alphas WHERE can_submit IS TRUE AND date_submitted IS NULL "
        f"ORDER BY (metrics->>'sharpe')::float DESC NULLS LAST LIMIT :n"
    ), {"n": int(backlog_n)})).mappings().all()

    def _spec(r, kind):
        d = dict(r)
        d["kind"] = kind
        return d

    return [_spec(r, "submitted") for r in sub] + [_spec(r, "backlog") for r in bk]


def _persist(signal: dict, rows: list) -> None:
    """Write latest snapshot + push a compact summary to the capped history list.
    Sync redis (control-plane client); soft-fail so a redis blip never crashes the beat."""
    try:
        from backend.tasks.redis_pool import get_redis_client
        r = get_redis_client()
        snapshot = {"signal": signal, "rows": rows, "computed_at": signal.get("computed_at")}
        r.set(_LATEST_KEY, json.dumps(snapshot, default=str))
        summary = {
            "computed_at": signal.get("computed_at"),
            "verdict": signal.get("verdict"),
            "turn_detected": signal.get("turn_detected"),
            "submitted_mean_resim": (signal.get("submitted") or {}).get("mean_resim"),
            "n_recovered_total": signal.get("n_recovered_total"),
            "n_resimmed": signal.get("n_resimmed"),
        }
        r.lpush(_HISTORY_KEY, json.dumps(summary, default=str))
        r.ltrim(_HISTORY_KEY, 0, _HISTORY_CAP - 1)
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"[regime_monitor] redis persist failed (non-fatal): {ex}")


@celery_app.task(name="backend.tasks.run_regime_monitor")
def run_regime_monitor():  # pragma: no cover - thin Celery wrapper around _run
    if not _on():
        return {"skipped": "ENABLE_REGIME_MONITOR OFF"}
    try:
        return run_async(_run())
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"[regime_monitor beat] failed (non-fatal): {ex}")
        return {"error": str(ex)}


async def _run() -> dict:
    from backend.database import AsyncSessionLocal
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.services.optimization.simulator import BrainSimulator
    from backend.regime_monitor import make_variant, compute_regime_signal

    backlog_n = int(getattr(settings, "REGIME_MONITOR_BACKLOG_SAMPLE", 10))
    recovery_gate = float(getattr(settings, "REGIME_MONITOR_RECOVERY_SHARPE", 1.25))
    turn_mean = float(getattr(settings, "REGIME_MONITOR_TURN_MEAN_SHARPE", 1.0))
    turn_min_recovered = int(getattr(settings, "REGIME_MONITOR_TURN_MIN_RECOVERED", 1))
    turn_frac = float(getattr(settings, "REGIME_MONITOR_TURN_RECOVERED_FRAC", 0.5))
    turn_max_decay = float(getattr(settings, "REGIME_MONITOR_TURN_MAX_DECAY", -0.25))
    stale_eps = float(getattr(settings, "REGIME_MONITOR_STALE_EPS", 1e-3))
    min_fresh = int(getattr(settings, "REGIME_MONITOR_MIN_FRESH", 3))

    async with AsyncSessionLocal() as db:
        specs = await _load_probe_specs(db, backlog_n)
    if not specs:
        logger.info("[regime_monitor] no probe alphas (no submitted/backlog) — skip")
        return {"skipped": "no_probe_alphas"}

    variants = [make_variant(s) for s in specs]
    # Chunked re-sim: concurrent WITHIN a chunk of exactly the sim-slot ceiling,
    # serial ACROSS chunks. This is faster than one-at-a-time AND loses no data.
    # Why the chunk size = slot limit matters: every sim in a chunk acquires a
    # slot IMMEDIATELY (count 0→N, none exceeds), so its 600s wait_for covers
    # only the real sim — no slot-queue wait to eat the budget → 0 sim_timeouts.
    # A chunk finishes in max-of-N (not sum-of-N) time → ~1.5-2x faster than
    # serial. Between chunks, run_batch's asyncio.gather drains every slot before
    # the next chunk fires, so there is never cross-chunk contention.
    # Contrast: firing ALL 23 at once (run_batch(variants, len)) lost 13/23 to
    # sim_timeout(600s) — the wait_for budget got eaten by slot-queue waiting
    # (2026-06-08 live test). The double slot-acquire that deadlocked the
    # 2026-06-07 0/23 run is root-fixed (1 slot/sim, simulator.py), which is what
    # makes within-chunk concurrency safe. See
    # reference_brainsim_double_acquire_deadlock.
    chunk = max(1, int(getattr(settings, "BRAIN_SIM_SLOT_LIMIT_USER", 3)))
    async with BrainAdapter() as brain:
        sim = BrainSimulator(brain)
        results = []
        for i in range(0, len(variants), chunk):
            batch = variants[i : i + chunk]
            results.extend(await sim.run_batch(batch, budget=len(batch)))

    # run_batch preserves input order → zip specs with results.
    probes, rows = [], []
    for s, res in zip(specs, results):
        resim = getattr(res, "sharpe", None)
        err = getattr(res, "error", None)
        probes.append({
            "alpha_id": s["alpha_id"], "kind": s["kind"],
            "baseline_sharpe": s.get("baseline_sharpe"), "resim_sharpe": resim,
        })
        rows.append({
            "alpha_id": s["alpha_id"], "kind": s["kind"],
            "baseline_sharpe": s.get("baseline_sharpe"),
            "resim_sharpe": resim, "error": err,
        })

    signal = compute_regime_signal(
        probes, recovery_gate=recovery_gate,
        turn_mean_threshold=turn_mean, turn_min_recovered=turn_min_recovered,
        turn_recovered_frac=turn_frac, turn_max_decay=turn_max_decay,
        stale_eps=stale_eps, min_fresh=min_fresh,
    )
    signal["computed_at"] = datetime.now(timezone.utc).isoformat()
    _persist(signal, rows)

    msg = (
        f"[regime_monitor] verdict={signal['verdict']} "
        f"submitted_mean_resim={(signal['submitted'] or {}).get('mean_resim')} "
        f"(baseline={(signal['submitted'] or {}).get('mean_baseline')}) "
        f"recovered={signal['n_recovered_total']}/{signal['n_resimmed']}"
    )
    if signal["turn_detected"]:
        logger.warning(f"🟢 REGIME TURN SIGNAL — {msg} recovered_ids={signal['recovered_ids']}")
    else:
        logger.info(msg)
    return signal
