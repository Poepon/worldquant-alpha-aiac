"""On-demand backlog current-data re-sim (v2, 2026-06-08, greenfield branch B).

Human-triggered (NOT a beat): re-sim one or a batch of backlog candidates on
CURRENT BRAIN data and classify decay vs the frozen-IS baseline. Reuses the
tested regime path (make_variant + BrainSimulator chunked) so the BRAIN path
can't drift. Results land in Redis keyed by job_id for the ops page to poll.

Gated on ENABLE_RESIM_BACKLOG. Read-only — simulate only, never submit.
口径 = current IS (NOT OS — BRAIN hides realized OS). See
docs/submit_backlog_resim_current_design_2026-06-08.md.

Post adversarial-review `wqxbthho4`: NO decay perturbation — re-sim with stored
settings; a result that returns EXACTLY at baseline (BRAIN dedup) is honestly
marked ``unmeasurable_cached`` (review H1/H7), not force-perturbed.
"""
import json
from datetime import datetime, timezone

from loguru import logger

from backend.celery_app import celery_app
from backend.config import settings
from backend.tasks import run_async


JOB_KEY_FMT = "resim_backlog:job:{job_id}"
LOCK_KEY = "resim_backlog:lock"


def _on() -> bool:
    return bool(getattr(settings, "ENABLE_RESIM_BACKLOG", False))


def _redis():
    from backend.tasks.redis_pool import get_redis_client
    return get_redis_client()


def _write_job(job_id: str, payload: dict) -> None:
    """Persist job state (sync redis, soft-fail). TTL keeps it pollable for a week."""
    try:
        ttl = int(getattr(settings, "RESIM_BACKLOG_RESULT_TTL_SEC", 604800))
        _redis().set(JOB_KEY_FMT.format(job_id=job_id), json.dumps(payload, default=str), ex=ttl)
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"[resim_backlog] job persist failed (non-fatal): {ex}")


def _release_lock() -> None:
    try:
        _redis().delete(LOCK_KEY)
    except Exception:  # noqa: BLE001
        pass


async def _load_specs(db, alpha_pks):
    """Load expression + structural settings + frozen baseline for the given PKs.
    Returns specs in the SAME order as alpha_pks (skips missing)."""
    from sqlalchemy import text as _text, bindparam
    cols = (
        "id AS alpha_pk, alpha_id, expression, region, universe, delay, decay, "
        "neutralization, truncation, can_submit, "
        "(metrics->>'sharpe')::float AS baseline_sharpe, "
        "(metrics->>'margin')::float AS baseline_margin"
    )
    stmt = _text(f"SELECT {cols} FROM alphas WHERE id IN :pks").bindparams(
        bindparam("pks", expanding=True))
    rows = (await db.execute(stmt, {"pks": [int(p) for p in alpha_pks]})).mappings().all()
    by_pk = {int(r["alpha_pk"]): dict(r) for r in rows}
    return [by_pk[int(pk)] for pk in alpha_pks if int(pk) in by_pk]


def _regime_reuse_map():
    """brain_id → resim_sharpe from the latest regime snapshot IF it is fresh
    enough to reuse (avoids re-simming what regime just measured). Empty on any
    miss/blip."""
    try:
        reuse_sec = int(getattr(settings, "RESIM_BACKLOG_REUSE_REGIME_SEC", 21600))
        raw = _redis().get("regime_monitor:latest")
        if not raw:
            return {}
        snap = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        ca = snap.get("computed_at")
        if not ca:
            return {}
        ts = datetime.fromisoformat(str(ca).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > reuse_sec:
            return {}
        out = {}
        for row in snap.get("rows") or []:
            aid = row.get("alpha_id")
            rs = row.get("resim_sharpe")
            if aid is not None and rs is not None and not row.get("error"):
                out[str(aid)] = float(rs)
        return out
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"[resim_backlog] regime reuse map failed (non-fatal): {ex}")
        return {}


async def _run(job_id: str, alpha_pks) -> dict:
    from backend.database import AsyncSessionLocal
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.services.optimization.simulator import BrainSimulator
    from backend.regime_monitor import make_variant
    from backend.resim_backlog import build_resim_verdict

    stale_eps = float(getattr(settings, "RESIM_BACKLOG_STALE_EPS", 1e-3))
    margin_floor = float(getattr(settings, "RESIM_BACKLOG_MARGIN_FLOOR_BPS", 5.0))
    stable_ratio = float(getattr(settings, "RESIM_VERDICT_STABLE_RATIO", 0.9))
    soft_ratio = float(getattr(settings, "RESIM_VERDICT_SOFT_RATIO", 0.6))

    async with AsyncSessionLocal() as db:
        specs = await _load_specs(db, alpha_pks)
    total = len(specs)
    base = {"job_id": job_id, "status": "running", "total": total, "done": 0,
            "results": [], "started_at": datetime.now(timezone.utc).isoformat()}
    _write_job(job_id, base)
    if not specs:
        base.update(status="done", note="no_matching_alphas",
                    finished_at=datetime.now(timezone.utc).isoformat())
        _write_job(job_id, base)
        return base

    reuse = _regime_reuse_map()

    def _verdict_row(spec, resim_sharpe, resim_margin, err, reused):
        bms = (spec.get("baseline_margin") or 0.0)
        v = build_resim_verdict(
            baseline_sharpe=spec.get("baseline_sharpe"),
            resim_sharpe=resim_sharpe,
            resim_margin_bps=(resim_margin * 10000.0) if resim_margin is not None else None,
            can_submit=bool(spec.get("can_submit")),
            error=err,
            stale_eps=stale_eps, margin_floor_bps=margin_floor,
            stable_ratio=stable_ratio, soft_ratio=soft_ratio,
        )
        v.update(alpha_pk=int(spec["alpha_pk"]), brain_id=spec.get("alpha_id"),
                 baseline_margin_bps=round(bms * 10000.0, 2), reused_from_regime=reused)
        return v

    # Split: reuse regime's fresh resim where available; sim the rest chunked.
    to_sim, results = [], []
    for s in specs:
        aid = str(s.get("alpha_id"))
        if aid in reuse:
            results.append(_verdict_row(s, reuse[aid], None, None, True))
        else:
            to_sim.append(s)
    base["done"] = len(results)
    base["results"] = results
    _write_job(job_id, base)

    if to_sim:
        # chunk = slot ceiling minus 1 (leave a slot for the pool if it resumes);
        # within-chunk concurrent, across-chunk serial — same shape as regime
        # (cc3b6f1): each sim acquires immediately → its 600s wait_for covers only
        # the real sim, no slot-queue starvation. Root fix = 1 slot/sim.
        chunk = max(1, int(getattr(settings, "BRAIN_SIM_SLOT_LIMIT_USER", 3)) - 1)
        variants = [make_variant(s) for s in to_sim]
        async with BrainAdapter() as brain:
            sim = BrainSimulator(brain)
            for i in range(0, len(variants), chunk):
                batch_specs = to_sim[i:i + chunk]
                batch_vars = variants[i:i + chunk]
                res = await sim.run_batch(batch_vars, budget=len(batch_vars))
                for s, r in zip(batch_specs, res):
                    results.append(_verdict_row(
                        s, getattr(r, "sharpe", None), getattr(r, "margin", None),
                        getattr(r, "error", None), False))
                base["done"] = len(results)
                base["results"] = results
                _write_job(job_id, base)  # incremental → UI fills as chunks land

    base.update(status="done", done=len(results),
                finished_at=datetime.now(timezone.utc).isoformat())
    _write_job(job_id, base)
    logger.info(f"[resim_backlog] job {job_id} done: {len(results)}/{total} "
                f"(reused {len(results) - len(to_sim)})")
    return base


@celery_app.task(name="backend.tasks.resim_backlog_current", bind=True)
def resim_backlog_current(self, job_id: str, alpha_pks):  # pragma: no cover - celery wrapper
    if not _on():
        _write_job(job_id, {"job_id": job_id, "status": "error",
                            "error": "ENABLE_RESIM_BACKLOG OFF"})
        _release_lock()
        return {"skipped": "ENABLE_RESIM_BACKLOG OFF"}
    try:
        return run_async(_run(job_id, alpha_pks))
    except Exception as ex:  # noqa: BLE001 — one failed batch must not wedge the lock
        logger.warning(f"[resim_backlog] job {job_id} failed: {ex}")
        _write_job(job_id, {"job_id": job_id, "status": "error", "error": str(ex)})
        return {"error": str(ex)}
    finally:
        _release_lock()
