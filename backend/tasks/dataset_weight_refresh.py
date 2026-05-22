"""Dataset-steering value bandit — daily mining_weight refresh cron.

Tier A of plan dataset_steering_bandit_plan_v3 (breadth direction). Turns the
dormant ``DatasetMetadata.mining_weight`` into a discounted Beta-Bernoulli
posterior over per-dataset *book-marginal* yield, then samples it to steer the
FLAT mining loop off the mined-out pv1 toward high-marginal-value + under-mined
orthogonal sources.

reward (per (region, dataset_id), over the refresh window):
    S_d = #(can_submit  AND  _iqc_marginal.delta_score > 0)   # book-marginal-positive
    T_d = #(real BRAIN sims)  — excludes metrics._pre_brain_skip (PRESIM_SKIP)
S_d ⊆ T_d by construction → Beta β stays ≥ 0 (review C1: single nested Bernoulli
success count, no continuous bump in v1).

Discounted, pull-indexed (selection_strategy.discounted_thompson_update):
    g = γ^T_d ;  α' = g·α + S_d ;  β' = g·β + (T_d − S_d)
γ once per real sim this window → heavily-mined arms forget fast, quiet arms
barely drift. Then mining_weight = θ~Beta(α,β) + floor·exp(−pulls/τ).

Windowing: SystemConfig watermark ``dataset_bandit_watermark`` bounds the
window (lower, run_started] — re-run / double-fire safe (cf. cognitive-layer
bandit). On the first run for a (region, dataset_id) there is no bandit_state
row → SEED from ALL history (α=1+S_hist, β=1+(T_hist−S_hist), pulls=T_hist) and
set the watermark to now, so the *next* window only covers post-seed sims and
g=γ^(small)≈1 — the seed isn't wiped (plan review C-1).

Unresolved-dataset_id alphas (derive_dataset_id returned None → dataset_id
NULL) are EXCLUDED from every arm's T_d — never folded into pv1 (review N-2).

flag-gated: no-op unless ENABLE_DATASET_VALUE_BANDIT is ON. Never raises.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async

_WM_KEY = "dataset_bandit_watermark"

def _classify(metrics: Any, can_submit: Any) -> Tuple[bool, bool]:
    """Return ``(is_real_sim, is_book_marginal_positive)`` for one alpha row.

    The two canonical reward signals (mirrors the three-gate submission rule):
      real sim       = NOT metrics._pre_brain_skip   (PRESIM_SKIP never hit BRAIN)
      book-marginal  = can_submit AND _iqc_marginal.delta_score > 0

    ``delta_score`` is the codebase's canonical "adds value to the team
    portfolio" gate (config IQC_AUTO_AUDIT + frontend filter + the
    can_submit/self-corr/IQC three-gate). Pure + dialect-free so the
    PRESIM_SKIP exclusion and the marginal gate are unit-testable without PG.
    """
    m = metrics if isinstance(metrics, dict) else {}
    if m.get("_pre_brain_skip"):
        return False, False  # pre-sim skip — never consumed a BRAIN sim
    iqc = m.get("_iqc_marginal")
    delta = iqc.get("delta_score") if isinstance(iqc, dict) else None
    book = bool(can_submit) and isinstance(delta, (int, float)) and not isinstance(delta, bool) and delta > 0
    return True, book


@celery_app.task(name="backend.tasks.run_dataset_weight_refresh")
def run_dataset_weight_refresh() -> Dict[str, Any]:
    """Beat-triggered dataset-steering bandit refresh. Never raises."""
    try:
        from backend.config import settings
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[dataset-bandit] settings import failed: {ex}")
        return {"updated_datasets": 0, "error": str(ex)[:200]}

    if not bool(getattr(settings, "ENABLE_DATASET_VALUE_BANDIT", False)):
        logger.info("[dataset-bandit] ENABLE_DATASET_VALUE_BANDIT=OFF — skip")
        return {"updated_datasets": 0, "skipped_reason": "flag_off"}

    try:
        return run_async(_refresh_async(
            gamma=float(getattr(settings, "DATASET_BANDIT_GAMMA", 0.95)),
            floor_c=float(getattr(settings, "DATASET_BANDIT_FLOOR_C", 0.1)),
            tau=float(getattr(settings, "DATASET_BANDIT_FLOOR_TAU", 500.0)),
            window_days=int(getattr(settings, "DATASET_BANDIT_WINDOW_DAYS", 7)),
        ))
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[dataset-bandit] refresh failed: {ex}")
        return {"updated_datasets": 0, "error": str(ex)[:200]}


async def _aggregate(db, *, lower=None, upper=None) -> Dict[Tuple[str, str], Tuple[int, int]]:
    """Return {(region, dataset_id): (s_d, t_d)} over (lower, upper] window.

    When lower/upper are None this is the ALL-HISTORY aggregation (seed). Rows
    with dataset_id NULL (unresolved attribution) are excluded — never folded
    into pv1 (review N-2). Classification is Python-side (``_classify``) so the
    query is plain SELECT (dialect-free, sqlite-testable; mirrors the
    cognitive-layer bandit cron) — no JSONB-cast SQL.
    """
    from sqlalchemy import select

    from backend.models import Alpha

    stmt = select(
        Alpha.region, Alpha.dataset_id, Alpha.can_submit, Alpha.metrics
    ).where(Alpha.dataset_id.isnot(None))
    if lower is not None:
        stmt = stmt.where(Alpha.created_at > lower)
    if upper is not None:
        stmt = stmt.where(Alpha.created_at <= upper)

    agg: Dict[Tuple[str, str], list] = {}
    for region, dataset_id, can_submit, metrics in (await db.execute(stmt)).all():
        real, book = _classify(metrics, can_submit)
        if not real:
            continue
        bucket = agg.setdefault((region, dataset_id), [0, 0])  # [s_d, t_d]
        bucket[1] += 1
        if book:
            bucket[0] += 1
    return {k: (v[0], v[1]) for k, v in agg.items()}


async def _refresh_async(
    *, gamma: float, floor_c: float, tau: float, window_days: int,
    session_factory=None, rng=None, dry_run: bool = False,
) -> Dict[str, Any]:
    """Core refresh. ``session_factory``/``rng`` are injectable for tests
    (production uses the global AsyncSessionLocal + a fresh Thompson RNG).

    ``dry_run=True`` computes the full per-dataset posterior + sampled weight
    and returns the details, but writes NOTHING (no bandit_state upsert, no
    mining_weight UPDATE, no watermark advance) — the pre-flight acceptance
    preview (plan verification §1). Safe to run against production before the
    flag is flipped."""
    from sqlalchemy import select, text

    from backend.models import BanditState, SystemConfig
    from backend.selection_strategy import (
        discounted_thompson_update,
        thompson_sample_weight,
    )

    if session_factory is None:
        from backend.database import AsyncSessionLocal as session_factory  # noqa: N813

    run_started = datetime.now(timezone.utc).replace(tzinfo=None)
    default_lower = (datetime.now(timezone.utc) - timedelta(days=max(0, window_days))).replace(tzinfo=None)
    rng = rng or random.Random()  # production Thompson draw; tests inject their own

    async with session_factory() as db:
        # --- watermark (idempotent, non-overlapping window) ---
        wm_row = (await db.execute(
            select(SystemConfig).where(SystemConfig.config_key == _WM_KEY)
        )).scalar_one_or_none()
        lower = default_lower
        if wm_row and wm_row.config_value:
            try:
                lower = datetime.fromisoformat(wm_row.config_value)
                if lower.tzinfo is not None:
                    lower = lower.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:  # noqa: BLE001 — malformed → window fallback
                lower = default_lower

        # Existing posteriors keyed by (region, dataset_id).
        state_rows = (await db.execute(select(BanditState))).scalars().all()
        states = {(r.region, r.dataset_id): r for r in state_rows}

        window = await _aggregate(db, lower=lower, upper=run_started)
        # First run (no posteriors yet): seed EVERY historical arm from
        # all-time value-yield — pv1's 2280 sims are mostly older than the
        # window, so a window-only seed would miss it (plan: seed from the
        # 5776 backfilled rows). Subsequent runs need only the window: arms
        # in states get a discounted update; a newly-appeared dataset (in
        # window, not in states) is seeded from its window counts (≈ its full
        # history since it's brand new). Watermark→now after seeding so the
        # next window covers only post-seed sims (g=γ^small≈1; review C-1).
        first_run = not states
        history = await _aggregate(db) if first_run else {}
        arms = set(history) if first_run else (set(window) | set(states))

        seeded = updated = 0
        details: Dict[str, Dict[str, Any]] = {}
        for key in sorted(arms):  # deterministic order → reproducible RNG draws
            region, dataset_id = key
            s_d, t_d = window.get(key, (0, 0))
            row = states.get(key)

            if row is None:
                # SEED: all-time on first run, else this window (new arm).
                s_seed, t_seed = history.get(key, (0, 0)) if first_run else (s_d, t_d)
                alpha = 1.0 + s_seed
                beta = 1.0 + max(0, t_seed - s_seed)
                pulls = t_seed
                seeded += 1
                _kind = "seed"
            else:
                alpha, beta = discounted_thompson_update(
                    float(row.alpha_param or 1.0), float(row.beta_param or 1.0),
                    s_d, t_d, gamma=gamma,
                )
                pulls = int(row.pulls or 0) + t_d
                updated += 1
                _kind = "update"

            weight = thompson_sample_weight(alpha, beta, pulls, floor_c=floor_c, tau=tau, rng=rng)

            if not dry_run:
                # Upsert posterior via ORM add/mutate (dialect-free; mirrors
                # the cognitive-layer bandit cron). pulls_at_last_refresh
                # snapshots cumulative pulls for window audit; the discount
                # itself uses the watermark-windowed T_d (= pulls −
                # pulls_at_last_refresh).
                if row is None:
                    db.add(BanditState(
                        region=region, dataset_id=dataset_id, pulls=pulls,
                        alpha_param=alpha, beta_param=beta, pulls_at_last_refresh=pulls,
                    ))
                else:
                    row.pulls = pulls
                    row.alpha_param = alpha
                    row.beta_param = beta
                    row.pulls_at_last_refresh = pulls

                # Write back mining_weight for every universe of this (region,
                # dataset). DatasetMetadata.__tablename__ == "datasets".
                await db.execute(
                    text(
                        "UPDATE datasets SET mining_weight = :w "
                        "WHERE region = :r AND dataset_id = :d"
                    ),
                    {"w": float(weight), "r": region, "d": dataset_id},
                )
            details[f"{region}:{dataset_id}"] = {
                "kind": _kind, "s_d": s_d, "t_d": t_d,
                "alpha": round(alpha, 3), "beta": round(beta, 3),
                "weight": round(weight, 4), "pulls": pulls,
            }

        if dry_run:
            await db.rollback()  # belt-and-suspenders: discard any ORM state
        else:
            # Advance watermark even when nothing matched (quiet window).
            if wm_row is None:
                db.add(SystemConfig(
                    config_key=_WM_KEY,
                    config_value=run_started.isoformat(),
                    config_type="timestamp",
                    description="dataset-steering bandit reward last-processed edge",
                ))
            else:
                wm_row.config_value = run_started.isoformat()

            await db.commit()

    logger.info(
        f"[dataset-bandit]{' DRY-RUN' if dry_run else ''} seeded={seeded} "
        f"updated={updated} window=({lower:%Y-%m-%d %H:%M}, "
        f"{run_started:%Y-%m-%d %H:%M}] first_run={first_run}"
    )
    return {
        "updated_datasets": seeded + updated,
        "seeded": seeded,
        "updated": updated,
        "dry_run": dry_run,
        "details": details,
    }
