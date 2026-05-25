"""Dataset-steering value bandit — daily mining_weight refresh cron.

Tier A of plan dataset_steering_bandit_plan_v3 (breadth direction). Turns the
dormant ``DatasetMetadata.mining_weight`` into a discounted Beta-Bernoulli
posterior over per-dataset *book-marginal* yield, then samples it to steer the
FLAT mining loop off the mined-out pv1 toward high-marginal-value + under-mined
orthogonal sources.

reward (per (region, dataset_id), over the refresh window):
    S_d = #(can_submit)        — AIAC-mined alphas (task_id NOT NULL) whose
          BRAIN is.checks all PASS (no FAIL). T_d = #(real AIAC sims),
          excludes metrics._pre_brain_skip (PRESIM_SKIP).
S_d ⊆ T_d by construction → Beta β stays ≥ 0 (single nested Bernoulli count).

reward = binary can_submit (v6, 2026-05-23). Evolution:
- v1 used (can_submit AND _iqc_marginal.delta_score>0): ~20 hits across all USA
  → too sparse → posterior collapsed to prior → mining_weight degenerated to the
  pure exploration floor → steered FLAT onto proven-weak under-mined datasets.
- v2-v5 chased an EDGE signal (graded sharpe/fitness). But edge ≠ value:
  model16 has 110 sharpe≥1.25 alphas yet 0/110 can_submit (all fail
  CONCENTRATED_WEIGHT — sparse fscore fields concentrate book weight), while pv1
  has 61 can_submit. Steering on edge points at fool's gold.
- v6 targets SUBMITTABLE yield directly: reward = (can_submit ? 1 : 0). A graded
  edge term is dead under this gate anyway — can_submit ⟹ sharpe≥1.25 ∧
  fitness≥1.0 ⟹ any min(sharpe/1.25,fitness/1.0) caps at 1.0. So the whole
  graded/convex apparatus retires; binary is honest + matches "yield = count of
  submittable alphas". Two reward fixes vs v1: (a) drop the over-sparse
  delta_score AND-clause (can_submit alone = 69 hits, denser); (b) ONLY count
  AIAC-mined (task_id NOT NULL) — the v1 _iqc_marginal stamp accidentally
  excluded BRAIN-synced user alphas; a pure can_submit read would ingest 4665
  synced rows and re-pollute, so the task_id filter is mandatory.
Limitation: can_submit ≠ portfolio-marginal value (a submittable-but-redundant
alpha still scores 1); the v1 delta_score captured that but was too sparse.

Discounted, pull-indexed (selection_strategy.discounted_thompson_update):
    g = γ^T_d ;  α' = g·α + S_d ;  β' = g·β + (T_d − S_d)
γ once per real sim this window → heavily-mined arms forget fast, quiet arms
barely drift. Then mining_weight = θ~Beta(α,β) + floor·exp(−pulls/τ).

Windowing: SystemConfig watermark ``dataset_bandit_watermark`` bounds the
window (lower, run_started] — re-run / double-fire safe (cf. cognitive-layer
bandit). On the first run (bandit_state empty — e.g. after a re-seed TRUNCATE)
SEED from ALL history (α=1+S_hist, β=1+(T_hist−S_hist), pulls=T_hist) and set
the watermark to now, so the *next* window only covers post-seed sims (g≈1).

First-run arms = (datasets with AIAC history) ∪ (all active catalog datasets).
A zero-history catalog dataset (s=t=0) seeds α=1, β=COLDSTART_BETA (pessimistic,
mean 1/(1+β)≈0.33 at β=2), pulls=0 → its mining_weight is driven by the
exploration floor, NOT left at the column default 1.0 (which would dominate the
bandit's sub-1.0 weights in ORDER BY / weighted_choice). Pessimistic (not
Beta(1,1)) so an untested source can't outrank a proven submitter; the floor
(full at pulls=0, decays with mining) still gives it exploration budget above a
tried-and-failed source. [H1, plan review C]

Unresolved-dataset_id alphas (dataset_id NULL) AND BRAIN-synced user alphas
(task_id NULL) are EXCLUDED from every arm's S_d/T_d — only AIAC-mined sims count.

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

def _classify(metrics: Any, can_submit: Any) -> Tuple[bool, float]:
    """Return ``(is_real_sim, reward)`` for one alpha row.

    real sim = NOT metrics._pre_brain_skip   (PRESIM_SKIP never hit BRAIN)
    reward   = 1.0 if can_submit else 0.0     (v6: SUBMITTABLE yield)

    ``can_submit`` = BRAIN is.checks all PASS (no FAIL) — already encodes
    LOW_SHARPE / LOW_FITNESS / HIGH_TURNOVER / CONCENTRATED_WEIGHT / sub-universe,
    so a graded edge term is dead under this gate (can_submit ⟹ above-threshold).
    NULL/None can_submit (not yet refreshed from BRAIN) → treated as 0 (unknown =
    not-submittable, conservative) but still counts as a real pull. See module
    docstring for why v6 is binary + why edge (v2-v5) pointed at fool's gold.
    Pure + dialect-free so the PRESIM_SKIP exclusion stays unit-testable w/o PG.
    """
    m = metrics if isinstance(metrics, dict) else {}
    if m.get("_pre_brain_skip"):
        return False, 0.0  # pre-sim skip — never consumed a BRAIN sim
    return True, (1.0 if can_submit is True else 0.0)


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
            # v6: pessimistic cold-start prior for zero-history catalog datasets.
            coldstart_beta=float(getattr(settings, "DATASET_BANDIT_COLDSTART_BETA", 2.0)),
        ))
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[dataset-bandit] refresh failed: {ex}")
        return {"updated_datasets": 0, "error": str(ex)[:200]}


async def _aggregate(db, *, lower=None, upper=None) -> Dict[Tuple[str, str], Tuple[float, int]]:
    """Return {(region, dataset_id): (s_d, t_d)} over (lower, upper] window.

    When lower/upper are None this is the ALL-HISTORY aggregation (seed).
    EXCLUDED (B1, v6): rows with dataset_id NULL (unresolved attribution) AND
    BRAIN-synced user alphas (``task_id`` NULL) — only AIAC-mined sims count.
    Without the task_id filter the v6 can_submit reward would ingest ~4665 synced
    user alphas (they carry dataset_id + can_submit) and re-pollute the signal.
    Classification is Python-side (``_classify``) so the query is plain SELECT
    (dialect-free, sqlite-testable). ``s_d`` = Σ reward = #(can_submit AIAC sims).
    """
    from sqlalchemy import select

    from backend.models import Alpha

    stmt = select(
        Alpha.region, Alpha.dataset_id, Alpha.can_submit, Alpha.metrics
    ).where(Alpha.dataset_id.isnot(None), Alpha.task_id.isnot(None))
    if lower is not None:
        stmt = stmt.where(Alpha.created_at > lower)
    if upper is not None:
        stmt = stmt.where(Alpha.created_at <= upper)

    agg: Dict[Tuple[str, str], list] = {}
    for region, dataset_id, can_submit, metrics in (await db.execute(stmt)).all():
        real, reward = _classify(metrics, can_submit)
        if not real:
            continue
        bucket = agg.setdefault((region, dataset_id), [0.0, 0])  # [s_d, t_d]
        bucket[0] += reward
        bucket[1] += 1
    return {k: (v[0], v[1]) for k, v in agg.items()}


async def _refresh_async(
    *, gamma: float, floor_c: float, tau: float, window_days: int,
    coldstart_beta: float = 2.0,
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

    from backend.models import BanditState, DatasetMetadata, DatasetCellStats, SystemConfig
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
        # First run (no posteriors yet — e.g. after a re-seed TRUNCATE): seed
        # EVERY historical arm from all-time value-yield (pv1's sims are mostly
        # older than the window, so a window-only seed would miss it). Subsequent
        # runs need only the window: arms in states get a discounted update; a
        # newly-appeared dataset (in window, not in states) is seeded from its
        # window counts. Watermark→now after seeding so the next window covers
        # only post-seed sims (g=γ^small≈1; review C-1).
        first_run = not states
        history = await _aggregate(db) if first_run else {}
        # H1 (v6): on the seed run, also enroll ALL active catalog datasets so
        # none is left at the column default mining_weight=1.0 (which would
        # dominate the bandit's sub-1.0 weights). Zero-history ones seed from a
        # pessimistic cold-start (s=t=0 → β=coldstart_beta). DISTINCT collapses
        # multi-universe rows to the (region, dataset_id) arm key.
        catalog_arms: set = set()
        if first_run:
            # is_active moved to dataset_cell_stats — a (region, dataset) arm is
            # "active" if it has at least one active cell. DISTINCT collapses cells.
            cat_rows = (await db.execute(
                select(DatasetMetadata.region, DatasetMetadata.dataset_id)
                .join(DatasetCellStats, DatasetCellStats.dataset_ref == DatasetMetadata.id)
                .where(DatasetCellStats.is_active.is_(True))
                .distinct()
            )).all()
            catalog_arms = {(r[0], r[1]) for r in cat_rows if r[0] and r[1]}
        arms = (set(history) | catalog_arms) if first_run else (set(window) | set(states))

        seeded = updated = 0
        details: Dict[str, Dict[str, Any]] = {}
        for key in sorted(arms):  # deterministic order → reproducible RNG draws
            region, dataset_id = key
            s_d, t_d = window.get(key, (0, 0))
            row = states.get(key)

            if row is None:
                # SEED: all-time on first run, else this window (new arm).
                s_seed, t_seed = history.get(key, (0, 0)) if first_run else (s_d, t_d)
                s_seed = min(s_seed, t_seed)  # defensive (this branch skips the update clamp)
                alpha = 1.0 + s_seed
                # Zero-history catalog arm (t_seed==0): pessimistic cold-start
                # β=coldstart_beta so an untested source can't outrank a proven
                # submitter; otherwise the all-history Bernoulli posterior. [H1]
                beta = float(coldstart_beta) if t_seed == 0 else 1.0 + max(0, t_seed - s_seed)
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

                # Write back mining_weight to every (universe, delay) cell of this
                # (region, dataset) — the bandit arm is universe-agnostic, so all
                # cells share its weight. mining_weight moved to dataset_cell_stats
                # (cell-stats normalization); the cell's region is via dataset_ref.
                await db.execute(
                    text(
                        "UPDATE dataset_cell_stats SET mining_weight = :w "
                        "WHERE dataset_ref IN ("
                        "  SELECT id FROM datasets WHERE region = :r AND dataset_id = :d"
                        ")"
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
