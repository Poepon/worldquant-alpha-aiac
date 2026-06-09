"""Pool scheduler (Phase 1b B5) — weighted (region,dataset) pick → hyp_intent.

The scheduler beat fetches active dataset cells, picks N ∝ DatasetCellStats.
mining_weight (the live binary-can_submit dataset-steering bandit — reward math
verbatim, NOT the retired DatasetSelector UCB), freezes the per-intent
config_snapshot (brain_role_snapshot + llm_overrides + thresholds_version,
lifted off the legacy ExperimentRun), and INSERTs hyp_intent(PENDING) rows for
the HG pool to claim. Termination is intrinsic (queue drains); no per-session
daily_goal.

INERT until ENABLE_POOL_PIPELINE (the beat gates on it). freeze + weighted_pick
+ insert are pure/testable; _fetch_active_cells is the live query (soak-tested).
"""
import random
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models import HypothesisIntent
from backend.selection_strategy import weighted_choice
from backend.pool import stages as st


def freeze_config_snapshot() -> Dict[str, Any]:
    """Freeze the per-intent config at schedule time (replaces ExperimentRun).

    Mirrors the brain_role_snapshot built at mining_tasks.py:357-366 so a claimed
    intent evaluates with the role frozen at scheduling, not live settings.
    llm_overrides=None → the global per-function LLM routing (LLM_FUNCTION_MODEL_
    MAP) applies; no per-intent override in Phase 1.
    """
    return {
        "brain_role_snapshot": {
            "brain_consultant_mode_at_start": bool(getattr(settings, "ENABLE_BRAIN_CONSULTANT_MODE", False)),
            "effective_default_test_period": settings.effective_default_test_period,
            "effective_sharpe_submit_min": settings.effective_sharpe_submit_min,
            "effective_region_universes": dict(settings.effective_region_universes),
        },
        "llm_overrides": None,
        "thresholds_version": str(getattr(settings, "EVAL_BAND_VERSION", "flat-eval-band")),
    }


def weighted_pick(cells: List[Dict[str, Any]], n: int,
                  *, rng: Optional[random.Random] = None) -> List[Dict[str, Any]]:
    """Pick up to n DISTINCT cells ∝ mining_weight (roulette w/o replacement).

    Degrades to uniform when weights are degenerate (weighted_choice contract).
    """
    pool = list(cells)
    picks: List[Dict[str, Any]] = []
    for _ in range(max(0, min(int(n), len(pool)))):
        chosen = weighted_choice(
            pool, [float(c.get("mining_weight", 1.0) or 0.0) for c in pool], rng=rng,
        )
        if chosen is None:
            break
        picks.append(chosen)
        pool.remove(chosen)
    return picks


async def _fetch_active_cells(session: Any) -> List[Dict[str, Any]]:
    """Active (region, dataset_id, universe, delay, mining_weight) cells. region
    + dataset_id come from datasets via dataset_ref FK (DatasetCellStats has no
    region/dataset_id of its own)."""
    rows = (await session.execute(text(
        "SELECT d.region AS region, d.dataset_id AS dataset_id, "
        "       c.universe AS universe, c.delay AS delay, "
        "       COALESCE(c.mining_weight, 1.0) AS mining_weight "
        "FROM dataset_cell_stats c "
        "JOIN datasets d ON d.id = c.dataset_ref "
        "WHERE c.is_active IS TRUE "
        "  AND d.region IS NOT NULL AND d.dataset_id IS NOT NULL"
    ))).mappings().all()
    return [dict(r) for r in rows]


async def insert_intents(picks: List[Dict[str, Any]], config_snapshot: Dict[str, Any],
                         *, fanout: Optional[int] = None, session_factory: Any = None) -> int:
    """INSERT one hyp_intent(PENDING) per picked cell. Returns rows inserted."""
    if not picks:
        return 0
    factory = session_factory or AsyncSessionLocal
    fan = fanout if fanout is not None else int(getattr(settings, "ALPHAS_PER_ROUND", 10))
    async with factory() as s:
        async with s.begin():
            for c in picks:
                s.add(HypothesisIntent(
                    region=c["region"],
                    universe=c.get("universe") or "TOP3000",
                    dataset_id=c.get("dataset_id"),
                    # delay-0 is a valid value — do NOT `or 1` (0 is falsy → would
                    # silently clobber delay-0 mining to delay-1).
                    delay=int(c["delay"]) if c.get("delay") is not None else 1,
                    fanout=fan,
                    stage=st.INTENT_PENDING,
                    config_snapshot=config_snapshot,
                    thresholds_version=config_snapshot.get("thresholds_version"),
                    # Orthogonal-breadth steering (PR-B): set by _assign_target_fields
                    # only when ENABLE_FIELD_SCREENING; None = legacy no-steer.
                    target_field=c.get("target_field"),
                ))
    return len(picks)


async def _assign_target_fields(picks: List[Dict[str, Any]], *, session_factory: Any = None,
                                rng: Optional[random.Random] = None) -> int:
    """Gated (ENABLE_FIELD_SCREENING): tag an explore-fraction of picks with a
    proportional-sampled under-explored ``target_field`` (mutates pick dicts).
    No-op + zero cost when the flag is OFF. Never raises (best-effort steering)."""
    if not bool(getattr(settings, "ENABLE_FIELD_SCREENING", False)):
        return 0
    try:
        from backend.field_screener import pick_target_field
    except Exception:  # noqa: BLE001
        return 0
    frac = float(getattr(settings, "FIELD_SCREEN_EXPLORE_FRAC", 0.1))
    top_k = int(getattr(settings, "FIELD_SCREEN_TOP_K", 50))
    floor = float(getattr(settings, "FIELD_NOVELTY_FLOOR", 0.05))
    k_orth = int(getattr(settings, "FIELD_ORTHO_CREDIBILITY_K", 4))
    rng = rng or random.Random()
    factory = session_factory or AsyncSessionLocal
    tagged = 0
    seen: set = set()  # PR-D: don't steer two intents in this round to the SAME field
    async with factory() as s:
        for c in picks:
            if rng.random() >= frac or not c.get("dataset_id"):
                continue
            try:
                tf = await pick_target_field(
                    s, dataset_id=c["dataset_id"], region=c["region"],
                    universe=c.get("universe") or "TOP3000",
                    delay=int(c["delay"]) if c.get("delay") is not None else 1,
                    top_k=top_k, novelty_floor=floor, k_orth=k_orth, rng=rng)
            except Exception as ex:  # noqa: BLE001 — steering must not break scheduling
                logger.warning(f"[field-screen] pick_target_field failed: {ex}")
                tf = None
            fid = tf.get("field_id") if tf else None
            if fid and fid not in seen:
                c["target_field"] = fid
                seen.add(fid)
                tagged += 1
    return tagged


async def schedule_round(n: int, *, session_factory: Any = None,
                         rng: Optional[random.Random] = None) -> int:
    """One scheduler firing: fetch cells → weighted-pick n → freeze config →
    INSERT hyp_intent. Returns the number of intents inserted (0 if no cells)."""
    factory = session_factory or AsyncSessionLocal
    async with factory() as s:
        cells = await _fetch_active_cells(s)
    if not cells:
        logger.info("[pool.scheduler] no active cells — nothing to schedule")
        return 0
    picks = weighted_pick(cells, n, rng=rng)
    # Orthogonal-breadth field steering (PR-B, gated ENABLE_FIELD_SCREENING) —
    # tag an explore-fraction of picks with an under-explored target_field. No-op
    # when the flag is OFF (legacy byte-for-byte).
    tagged = await _assign_target_fields(picks, session_factory=factory, rng=rng)
    inserted = await insert_intents(picks, freeze_config_snapshot(), session_factory=factory)
    logger.info(f"[pool.scheduler] inserted {inserted} hyp_intent rows"
                + (f" ({tagged} field-steered)" if tagged else ""))
    return inserted
