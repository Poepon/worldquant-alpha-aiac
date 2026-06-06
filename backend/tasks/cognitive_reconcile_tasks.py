"""Pool Phase 2 (1c) — cognitive reconcile beat.

The async cognitive engine for the decoupled HG/S/E pool. FLAT drove the
hypothesis lifecycle synchronously inside the LangGraph (B5 _process_hypothesis_
feedback), which the pool retired. This beat is the pool-native replacement: it
scans recently-LANDED alphas (those whose can_submit label has had time to
settle) and drives the typed-Hypothesis lifecycle off the alphas JOIN:

  - refresh_stats        — recompute alpha_count / pass_count / can_submit_count /
                           submitted_count / sharpe rollups.
  - auto-activate        — PROPOSED → ACTIVE on the first alpha (any status).
  - PROMOTE              — ACTIVE/PROPOSED → PROMOTED on the first SUBMITTABLE
                           alpha (can_submit_count>0). NOT pass_count (which
                           counts PASS_PROVISIONAL — plan §7 guard #5).
  - attribution stamp    — a cheap heuristic (early_stop.classify_attribution)
                           for failing hypotheses; instrumentation only, bootstrap
                           corpus for the Phase-2 LLM attribution upgrade.

R3 discipline (plan §7 Track C):
  (i)   WATERMARK on alphas.created_at + a GRACE PERIOD (POOL_RECONCILE_GRACE_SEC,
        ≥2× the 30s can_submit refresh countdown). The scan upper-bound is
        ``now - grace`` so a just-landed alpha whose can_submit is still being
        refreshed is NOT read as NULL before its label settles. Mirrors the proven
        idempotent SystemConfig-timestamp watermark of dataset_weight_refresh.
  (iii) CENSORED-NOT-NEGATIVE — an alpha with can_submit=NULL simply does not add
        to can_submit_count (it is not a miss); it is re-counted whenever a later
        reconcile touches the hypothesis. We never treat NULL as a failure.

ABANDON is intentionally NOT done here (deferred): orphan PROPOSED rows are inert
in the pool (it generates fresh per-intent, never samples hypotheses), and
abandoning on a 0-can_submit window risks pruning a productive cell under the
67-backlog reality (plan §7 guard #10 / LB7). PROMOTE + activate + stats +
attribution are the load-bearing 1c behaviours.

Flag-gated: no-op unless ENABLE_POOL_COGNITIVE_RECONCILE is ON. Never raises.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async

_WM_KEY = "pool_reconcile_watermark"

# Cheap error_type bucketing for the attribution heuristic. The pool's non-PASS
# alphas all land in alpha_failures with a free-text error_type (String(100)):
# syntax/validation/operator → implementation; sim/timeout/transient → also
# implementation; anything else (LOW_SHARPE / LOW_FITNESS / HIGH_TURNOVER /
# CONCENTRATED_WEIGHT / sub-universe …) = "ran fine but failed quality" =
# hypothesis-attributable.
# NOTE keep keys specific — a too-generic token false-matches a quality name
# (e.g. "RATE" matched "concentRATEd_weight"). Rate-limit errors carry "429".
_IMPL_SYNTAX_KEYS = ("PARSE", "SYNTAX", "FIELD", "VALIDAT", "OPERATOR", "GRAMMAR", "ARITY")
_IMPL_SIM_KEYS = ("SIM", "TIMEOUT", "BRAIN", "NETWORK", "TRANSIENT", "429", "SLOT")


def _bucket_failures(error_types: List[Optional[str]]) -> Tuple[int, int, int]:
    """(syntax_fail, simulate_fail, quality_fail) from alpha_failures.error_type.

    Pure + keyword-only so it stays unit-testable without PG. Anything not
    recognised as a syntax/validation or a sim/transient error is bucketed as a
    quality fail (the alpha ran but missed a gate → hypothesis-attributable)."""
    syn = sim = qual = 0
    for et in error_types:
        u = (et or "").upper()
        if any(k in u for k in _IMPL_SYNTAX_KEYS):
            syn += 1
        elif any(k in u for k in _IMPL_SIM_KEYS):
            sim += 1
        else:
            qual += 1
    return syn, sim, qual


@celery_app.task(name="backend.tasks.run_pool_cognitive_reconcile")
def run_pool_cognitive_reconcile() -> Dict[str, Any]:
    """Beat-triggered cognitive reconcile. Never raises."""
    try:
        from backend.config import settings
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[pool-reconcile] settings import failed: {ex}")
        return {"hypotheses": 0, "error": str(ex)[:200]}

    if not bool(getattr(settings, "ENABLE_POOL_COGNITIVE_RECONCILE", False)):
        logger.info("[pool-reconcile] ENABLE_POOL_COGNITIVE_RECONCILE=OFF — skip")
        return {"hypotheses": 0, "skipped_reason": "flag_off"}

    try:
        return run_async(_reconcile_async(
            grace_sec=int(getattr(settings, "POOL_RECONCILE_GRACE_SEC", 60)),
            window_days=int(getattr(settings, "POOL_RECONCILE_WINDOW_DAYS", 7)),
        ))
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[pool-reconcile] failed: {ex}")
        return {"hypotheses": 0, "error": str(ex)[:200]}


async def _reconcile_async(
    *, grace_sec: int, window_days: int, session_factory=None,
) -> Dict[str, Any]:
    """Core reconcile. ``session_factory`` is injectable for tests.

    Idempotent: re-running over the same window recomputes identical stats and
    the lifecycle transitions are rowcount-guarded no-ops once applied. The
    watermark advances to the grace-adjusted upper edge so the next run only
    covers newly-settled alphas (and a quiet window still advances it)."""
    from sqlalchemy import distinct, select, update
    from backend.agents.graph.early_stop import classify_attribution
    from backend.models import Alpha, AlphaFailure, Hypothesis, SystemConfig
    from backend.services.hypothesis_service import HypothesisService

    if session_factory is None:
        from backend.database import AsyncSessionLocal as session_factory  # noqa: N813

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    upper = now - timedelta(seconds=max(0, grace_sec))     # grace: skip too-fresh rows
    default_lower = now - timedelta(days=max(0, window_days))

    async with session_factory() as db:
        wm = (await db.execute(
            select(SystemConfig).where(SystemConfig.config_key == _WM_KEY)
        )).scalar_one_or_none()
        lower = default_lower
        if wm and wm.config_value:
            try:
                lower = datetime.fromisoformat(wm.config_value)
                if lower.tzinfo is not None:
                    lower = lower.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:  # noqa: BLE001 — malformed → window fallback
                lower = default_lower

        if upper <= lower:
            # Grace pushed the upper edge before the watermark (very fresh run
            # right after the previous one). Nothing settled yet; do NOT regress
            # the watermark — just wait for the next tick.
            return {"hypotheses": 0, "skipped_reason": "empty_window",
                    "lower": lower.isoformat(), "upper": upper.isoformat()}

        # Distinct hypotheses whose alphas LANDED in (lower, upper].
        hid_rows = (await db.execute(
            select(distinct(Alpha.hypothesis_id)).where(
                Alpha.hypothesis_id.isnot(None),
                Alpha.created_at > lower,
                Alpha.created_at <= upper,
            )
        )).all()
        hids = [r[0] for r in hid_rows if r[0] is not None]

        svc = HypothesisService(db)
        activated = promoted = attributed = 0
        for hid in hids:
            try:
                stats = await svc.refresh_stats(hid)
                if stats.can_submit_count > 0:
                    # SUBMITTABLE → PROMOTE (covers PROPOSED→PROMOTED directly).
                    if await svc.mark_promoted(hid):
                        promoted += 1
                elif stats.alpha_count > 0:
                    # Has evidence but no submittable winner yet.
                    if await svc.mark_active(hid):
                        activated += 1
                    # Attribution stamp for the still-failing (0 PASS) ones.
                    if stats.pass_count == 0:
                        ets = (await db.execute(
                            select(AlphaFailure.error_type)
                            .where(AlphaFailure.hypothesis_id == hid)
                        )).scalars().all()
                        syn, sim, qual = _bucket_failures(ets)
                        attr = classify_attribution(
                            alpha_count=stats.alpha_count,
                            pass_count=stats.pass_count,
                            syntax_fail_count=syn,
                            simulate_fail_count=sim,
                            quality_fail_count=qual,
                        )
                        if attr != "unknown":
                            h = await svc.get_by_id(hid)
                            if h is not None and h.attribution != attr:
                                await db.execute(
                                    update(Hypothesis)
                                    .where(Hypothesis.id == hid)
                                    .values(attribution=attr)
                                )
                                attributed += 1
            except Exception as ex:  # noqa: BLE001 — one bad hyp ≠ failed beat
                logger.warning(f"[pool-reconcile] hid={hid} failed (soft): {ex}")

        # Advance the watermark (even on a quiet window) so the next run only
        # covers newly-settled alphas. Done in the SAME commit as the lifecycle
        # writes — a crash before commit re-processes the window idempotently.
        if wm is None:
            db.add(SystemConfig(
                config_key=_WM_KEY,
                config_value=upper.isoformat(),
                config_type="timestamp",
                description="pool cognitive reconcile last-processed edge (alphas.created_at, grace-adjusted)",
            ))
        else:
            wm.config_value = upper.isoformat()
        await db.commit()

    logger.info(
        f"[pool-reconcile] hyps={len(hids)} activated={activated} "
        f"promoted={promoted} attributed={attributed} "
        f"window=({lower:%Y-%m-%d %H:%M}, {upper:%Y-%m-%d %H:%M}]"
    )
    return {
        "hypotheses": len(hids),
        "activated": activated,
        "promoted": promoted,
        "attributed": attributed,
    }
