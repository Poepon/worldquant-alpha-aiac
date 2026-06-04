"""Auto-submit beat — automate the orthogonal backlog drain (2026-06-04).

Fires every ``AUTO_SUBMIT_BEAT_INTERVAL_HOURS`` (default 6h). One firing:

  1. G0 master kill-switch: ``ENABLE_AUTO_SUBMIT`` False OR ``AUTO_SUBMIT_MODE``
     == 'off' → early-exit ``{"skipped": ...}``.
  2. Single-flight beat lock (Redis NX) so two workers can't double-run.
  3. Per region in ``AUTO_SUBMIT_REGIONS`` (one at a time — self_corr pool is
     same-region): build the strict orthogonal drain order
     (``auto_submit_selector.compute_auto_submit_candidates``).
  4. G1 region gate: the live recon verdict must meet ``AUTO_SUBMIT_REQUIRE_RECON_VERDICT``
     (Stage1 'supported'); else the whole region is skipped (fail-closed — never
     route on an unvalidated offline ΔSharpe sign).
  5. Per candidate (in submit order) apply the fail-closed guard stack G3-G9.
     - SHADOW: gate-passers → audit ``would_submit`` (NO submit); failures →
       ``skipped`` with the failing gate. This is the human-review surface.
     - LIVE: gate-passers, up to PER_RUN_CAP / DAILY_CAP, → ``AlphaService.submit_alpha``
       (which keeps its own irreversible gates G10) → audit ``submitted`` / ``rejected``.

Submission is irreversible — the design is fail-closed everywhere: any missing /
stale / un-measured signal, any exception, Redis-down-in-live → the candidate is
NOT submitted. Default OFF; default mode 'shadow'.

Source: auto-submit design (2026-06-04) §1 guard stack + §2 safety + §5 staging.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.celery_app import celery_app
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.tasks import run_async

logger = logging.getLogger("tasks.auto_submit")

_BEAT_LOCK_KEY = "auto_submit:beat_inflight"
_BEAT_LOCK_TTL = 1800  # 30min — comfortably > worst-case PER_RUN_CAP slow BRAIN submits
# CAS release (mirrors submit_alpha's Lua) — only delete the lock if WE still hold
# it, so a run that overran its TTL can't steal a later beat's lock.
_LOCK_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)


def _utc_day_key() -> str:
    return "auto_submit:count:" + datetime.now(timezone.utc).strftime("%Y%m%d")


def _mode() -> str:
    m = str(getattr(settings, "AUTO_SUBMIT_MODE", "shadow")).strip().lower()
    return m if m in ("off", "shadow", "live") else "off"


async def _get_redis():
    try:
        from backend.adapters.brain_adapter import BrainAdapter
        return await BrainAdapter._get_slot_redis()
    except Exception as ex:  # noqa: BLE001
        logger.warning("[auto_submit] redis unavailable: %s", ex)
        return None


async def _redis_decr(redis, key: str) -> None:
    """Release a daily-cap reservation (incr-first scheme). Never raises."""
    if redis is None:
        return
    try:
        await redis.decr(key)
    except Exception:  # noqa: BLE001
        pass


@celery_app.task(name="backend.tasks.run_auto_submit_cycle")
def run_auto_submit_cycle() -> Dict[str, Any]:
    """Celery entry point (Windows --pool=solo friendly via run_async)."""
    return run_async(_run())


async def _run() -> Dict[str, Any]:
    # G0 — master kill-switch.
    if not bool(getattr(settings, "ENABLE_AUTO_SUBMIT", False)):
        return {"skipped": "flag_off"}
    mode = _mode()
    if mode == "off":
        return {"skipped": "mode_off"}

    redis = await _get_redis()
    beat_run_id = uuid.uuid4().hex[:16]

    # Single-flight: NX lock valued with our run id (CAS release below avoids
    # stealing a later beat's lock if ours expired under a slow BRAIN submit).
    # Best-effort: Redis down → proceed without the lock (the atomic daily-cap
    # reservation + per-alpha submit_lock still bound submissions).
    holds_lock = False
    if redis is not None:
        try:
            holds_lock = bool(
                await redis.set(_BEAT_LOCK_KEY, beat_run_id, nx=True, ex=_BEAT_LOCK_TTL)
            )
            if not holds_lock:
                return {"skipped": "beat_inflight"}
        except Exception as ex:  # noqa: BLE001
            logger.warning("[auto_submit] beat lock failed (continuing): %s", ex)

    # LIVE requires Redis to enforce the daily cap. If Redis is down, degrade to
    # shadow for this run (never over-submit because we couldn't count).
    effective_mode = mode
    if mode == "live" and redis is None:
        logger.warning("[auto_submit] LIVE requested but Redis down → degrading to shadow this run")
        effective_mode = "shadow"
    regions = [
        r.strip() for r in str(getattr(settings, "AUTO_SUBMIT_REGIONS", "USA")).split(",")
        if r.strip()
    ]
    require_verdict = str(
        getattr(settings, "AUTO_SUBMIT_REQUIRE_RECON_VERDICT", "supported")
    ).strip().lower()
    per_run_cap = int(getattr(settings, "AUTO_SUBMIT_PER_RUN_CAP", 2))
    daily_cap = int(getattr(settings, "AUTO_SUBMIT_DAILY_CAP", 4))

    summary: Dict[str, Any] = {
        "mode": effective_mode, "beat_run_id": beat_run_id,
        "submitted": 0, "would_submit": 0, "skipped": 0, "errors": 0,
        "regions": {},
    }
    submitted_this_run = 0
    try:
        for region in regions:
            if effective_mode == "live" and submitted_this_run >= per_run_cap:
                summary["regions"][region] = {"skipped": "per_run_cap_reached"}
                continue
            res = await _run_region(
                region=region, mode=effective_mode, beat_run_id=beat_run_id,
                require_verdict=require_verdict,
                run_budget=max(0, per_run_cap - submitted_this_run),
                daily_cap=daily_cap, redis=redis,
            )
            summary["regions"][region] = res
            submitted_this_run += int(res.get("submitted", 0))
            for k in ("submitted", "would_submit", "skipped", "errors"):
                summary[k] += int(res.get(k, 0))
    finally:
        if redis is not None and holds_lock:
            try:
                await redis.eval(_LOCK_RELEASE_LUA, 1, _BEAT_LOCK_KEY, beat_run_id)
            except Exception:  # noqa: BLE001
                pass
    return summary


def _verdict_ok(verdict: Optional[str], require: str) -> bool:
    if require == "weak":
        return verdict in ("supported", "weak")
    # default / Stage1: only an affirmatively supported verdict
    return verdict == "supported"


async def _run_region(
    *, region: str, mode: str, beat_run_id: str, require_verdict: str,
    run_budget: int, daily_cap: int, redis,
) -> Dict[str, Any]:
    from backend.auto_submit_selector import (
        compute_auto_submit_candidates, evaluate_guard_stack,
    )
    from backend.models import AutoSubmitAudit

    res = {"submitted": 0, "would_submit": 0, "skipped": 0, "errors": 0,
           "recon_verdict": None, "n_candidates": 0}

    async with AsyncSessionLocal() as db:
        try:
            sel = await compute_auto_submit_candidates(db, region=region, settings=settings)
        except Exception as ex:  # noqa: BLE001
            logger.exception("[auto_submit] candidate build failed for region=%s: %s", region, ex)
            return {**res, "error": f"{type(ex).__name__}: {ex}"}

        recon = sel.get("recon_stat") or {}
        verdict = recon.get("verdict")
        res["recon_verdict"] = verdict
        ordered: List[Dict[str, Any]] = sel.get("ordered") or []
        res["n_candidates"] = len(ordered)
        sign_ok = bool(sel.get("sign_routing_ok"))

        # G1 — region-level recon gate (fail-closed). Skip region if the offline
        # ΔSharpe sign isn't validated against BRAIN at the required level.
        if not (sign_ok and _verdict_ok(verdict, require_verdict)):
            res["skipped_region"] = f"recon_{verdict}"
            logger.info(
                "[auto_submit] region=%s skipped (recon verdict=%s, require=%s)",
                region, verdict, require_verdict,
            )
            return res

        now = datetime.now(timezone.utc)
        # Lazily opened only when we actually submit (live).
        brain = None
        svc = None
        try:
            for cand in ordered:
                try:
                    ev = evaluate_guard_stack(
                        cand, sign_routing_ok=sign_ok, settings=settings, now_utc=now,
                    )
                except Exception as ex:  # noqa: BLE001 — guard must never crash the beat
                    logger.exception("[auto_submit] guard eval failed alpha_pk=%s: %s",
                                     cand.get("id"), ex)
                    await _audit(db, AutoSubmitAudit, cand, region, mode, beat_run_id,
                                 outcome="error", skip_reason=f"guard_error:{type(ex).__name__}",
                                 gate_results={"error": str(ex)})
                    res["errors"] += 1
                    continue

                gate_results = {"gates": ev["gates"], "signals": ev["signals"]}

                if not ev["passed"]:
                    await _audit(db, AutoSubmitAudit, cand, region, mode, beat_run_id,
                                 outcome="skipped", skip_reason=ev["skip_reason"],
                                 gate_results=gate_results)
                    res["skipped"] += 1
                    continue

                # Gate-passer.
                if mode == "shadow":
                    await _audit(db, AutoSubmitAudit, cand, region, mode, beat_run_id,
                                 outcome="would_submit", gate_results=gate_results)
                    res["would_submit"] += 1
                    continue

                # LIVE — enforce caps before the irreversible action.
                if res["submitted"] >= run_budget:
                    res["cap_note"] = "per_run_cap_reached"
                    break

                # ATOMIC daily-cap RESERVATION (incr-first): reserve a slot BEFORE
                # the irreversible submit. incr is atomic so two concurrent beats
                # (e.g. a stale-lock overrun) can't both pass the cap (fixes the
                # read-then-check TOCTOU), and a post-submit incr can never be lost.
                # If we don't end up submitting (over-cap / rejected / error) we
                # DECR the reservation back so a rejection doesn't burn the day's cap.
                day_key = _utc_day_key()
                try:
                    reserved = int(await redis.incr(day_key))
                    await redis.expire(day_key, 36 * 3600)
                except Exception as ex:  # noqa: BLE001 — can't reserve → don't submit
                    logger.warning("[auto_submit] daily reserve failed → stop live: %s", ex)
                    res["cap_note"] = "daily_count_unavailable"
                    break
                if reserved > daily_cap:
                    await _redis_decr(redis, day_key)  # release the over-cap reservation
                    res["cap_note"] = "daily_cap_reached"
                    break

                # Open the adapter + service once, on first live submit.
                if svc is None:
                    from backend.adapters.brain_adapter import BrainAdapter
                    from backend.services.alpha_service import AlphaService
                    brain = BrainAdapter()
                    await brain.__aenter__()
                    svc = AlphaService(db)

                pk = int(cand["id"])
                try:
                    result = await svc.submit_alpha(pk, brain_adapter=brain)
                except Exception as ex:  # noqa: BLE001 — never let a submit error abort the run
                    logger.exception("[auto_submit] submit_alpha raised alpha_pk=%s: %s", pk, ex)
                    await _redis_decr(redis, day_key)  # release reservation on error
                    await _audit(db, AutoSubmitAudit, cand, region, mode, beat_run_id,
                                 outcome="error", skip_reason=f"submit_error:{type(ex).__name__}",
                                 gate_results=gate_results, brain_response={"error": str(ex)})
                    res["errors"] += 1
                    continue

                if result.get("submitted"):
                    await _audit(db, AutoSubmitAudit, cand, region, mode, beat_run_id,
                                 outcome="submitted", gate_results=gate_results,
                                 brain_response=result)
                    res["submitted"] += 1
                    logger.info("[auto_submit] LIVE submitted alpha_pk=%s region=%s", pk, region)
                else:
                    await _redis_decr(redis, day_key)  # rejection didn't submit → free the slot
                    await _audit(db, AutoSubmitAudit, cand, region, mode, beat_run_id,
                                 outcome="rejected", skip_reason=result.get("reason"),
                                 gate_results=gate_results, brain_response=result)
                    res["skipped"] += 1
        finally:
            if brain is not None:
                try:
                    await brain.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
    return res


async def _audit(
    db, AutoSubmitAudit, cand: Dict[str, Any], region: str, mode: str,
    beat_run_id: str, *, outcome: str, gate_results: Dict[str, Any],
    skip_reason: Optional[str] = None, brain_response: Optional[Dict[str, Any]] = None,
) -> None:
    """Write one audit row (own commit, never raises — audit must not abort the beat)."""
    try:
        row = AutoSubmitAudit(
            alpha_pk=int(cand.get("id")),
            alpha_brain_id=cand.get("_brain_id"),
            region=region,
            mode=mode,
            outcome=outcome,
            skip_reason=skip_reason,
            gate_results=gate_results or {},
            brain_response=brain_response,
            beat_run_id=beat_run_id,
        )
        db.add(row)
        await db.commit()
    except Exception as ex:  # noqa: BLE001
        logger.warning("[auto_submit] audit write failed (alpha_pk=%s, %s): %s",
                       cand.get("id"), outcome, ex)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# can_submit periodic refresh beat (2026-06-04)
# ---------------------------------------------------------------------------
_CS_REFRESH_LOCK_KEY = "can_submit_refresh:inflight"
_CS_REFRESH_LOCK_TTL = 1800  # 30min — generous upper bound for a paced batch


@celery_app.task(name="backend.tasks.run_can_submit_refresh")
def run_can_submit_refresh() -> Dict[str, Any]:
    """Celery entry point (Windows --pool=solo friendly via run_async)."""
    return run_async(_run_can_submit_refresh())


async def _run_can_submit_refresh() -> Dict[str, Any]:
    """Re-check can_submit for the can_submit=True / unsubmitted backlog against
    BRAIN, stalest-first, to keep the verdict + its ``_brain_can_submit_at``
    freshness stamp current (auto-submit G4) and demote alphas BRAIN now rejects.

    Read-only BRAIN GETs, paced 1 req/s. Gated by ENABLE_CAN_SUBMIT_REFRESH
    (default OFF). Single-flight via a Redis NX lock so it can't overlap itself
    or a manual batch refresh.
    """
    if not bool(getattr(settings, "ENABLE_CAN_SUBMIT_REFRESH", False)):
        return {"skipped": "flag_off"}

    import asyncio as _asyncio
    from sqlalchemy import text as _text
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.services.alpha_service import AlphaService

    redis = await _get_redis()
    holds_lock = False
    if redis is not None:
        try:
            holds_lock = bool(
                await redis.set(_CS_REFRESH_LOCK_KEY, "1", nx=True, ex=_CS_REFRESH_LOCK_TTL)
            )
            if not holds_lock:
                return {"skipped": "refresh_inflight"}
        except Exception as ex:  # noqa: BLE001
            logger.warning("[can_submit_refresh] lock failed (continuing): %s", ex)

    max_n = int(getattr(settings, "CAN_SUBMIT_REFRESH_MAX_PER_RUN", 200))
    refreshed = still = flipped = skipped = 0
    try:
        async with AsyncSessionLocal() as db:
            # stalest-first: never-stamped (NULL) first, then oldest ISO stamp
            # (ISO text sorts chronologically). Bounds BRAIN GETs to max_n/run.
            ids = [r[0] for r in (await db.execute(_text(
                """
                SELECT id FROM alphas
                WHERE can_submit IS TRUE AND date_submitted IS NULL AND alpha_id IS NOT NULL
                ORDER BY (metrics->>'_brain_can_submit_at') ASC NULLS FIRST, id
                LIMIT :lim
                """
            ), {"lim": max_n})).all()]
            if not ids:
                return {"scanned": 0, "refreshed": 0}
            svc = AlphaService(db)
            async with BrainAdapter() as ba:
                for aid in ids:
                    await _asyncio.sleep(1.0)
                    try:
                        res = await svc.refresh_can_submit(aid, brain_adapter=ba)
                    except Exception as ex:  # noqa: BLE001 — per-alpha failure never aborts the run
                        logger.warning("[can_submit_refresh] alpha_pk=%s failed: %s", aid, ex)
                        skipped += 1
                        continue
                    if res is None:
                        skipped += 1
                        continue
                    refreshed += 1
                    if res.get("can_submit"):
                        still += 1
                    else:
                        flipped += 1
            logger.info(
                "[can_submit_refresh] scanned=%d refreshed=%d still=%d flipped_false=%d skipped=%d",
                len(ids), refreshed, still, flipped, skipped,
            )
            return {
                "scanned": len(ids), "refreshed": refreshed,
                "still_can_submit": still, "flipped_false": flipped, "skipped": skipped,
            }
    finally:
        if redis is not None and holds_lock:
            try:
                await redis.delete(_CS_REFRESH_LOCK_KEY)
            except Exception:  # noqa: BLE001
                pass
