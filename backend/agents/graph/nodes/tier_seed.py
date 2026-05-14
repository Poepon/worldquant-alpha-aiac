"""T2 / T3 LLM-guided wrapping nodes (PR2).

Three nodes total:

1. node_tier_seed_load: runs ONCE at task start. Loads K=max(daily_goal*3, 10)
   PASS alphas at the predecessor tier, refreshes their metrics via BRAIN
   GET /alphas/{id}, re-evaluates against current tier thresholds, drops any
   that demoted, persists the surviving set into state.tier_seeds.

2. node_tier_strategy_select: per round, LLM picks T2Strategy (or T3Strategy)
   for the current seed. Writes to state.current_strategy.

3. node_tier_wrap_one: per round, expand_t{2,3}_strategy materializes 8-12
   (or 3-5 for T3) variant expressions. Writes to state.pending_alphas with
   parent_alpha_id propagated from the seed.

Routing (handled in workflow.py): after SAVE_RESULTS, advance current_seed_index
and loop back to strategy_select if more seeds remain, else END.
"""
from __future__ import annotations

import random
import time
from typing import Dict, List

from langchain_core.runnables import RunnableConfig
from loguru import logger
from sqlalchemy import select

from backend.agents.graph.nodes.base import record_trace, resolve_db
from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.graph.tier_thresholds import get_min_seed_count, get_tier_thresholds
from backend.agents.services.llm_service import get_llm_service
from backend.config import settings
from backend.factor_wrapping import (
    DEFAULT_T2_STRATEGY,
    DEFAULT_T3_STRATEGY,
    T2Strategy,
    T3Strategy,
    expand_t2_strategy,
    expand_t3_strategy,
    select_t2_strategy_via_llm,
    select_t3_strategy_via_llm,
)
from backend.models import Alpha


def _seed_count_target(daily_goal: int) -> int:
    """K = max(daily_goal × 3, MIN_TIER_SEED_COUNT × 2). Plan §6.1."""
    return max(daily_goal * 3, get_min_seed_count() * 2)


async def node_tier_seed_load(
    state: MiningState, config: RunnableConfig = None
) -> Dict:
    """Load + refresh predecessor-tier PASS alphas as seeds for this T2/T3 task.

    Plan §6.1 — runs once after START:
      1. SELECT * FROM alphas WHERE quality_status='PASS' AND factor_tier=N-1
         AND region=state.region ORDER BY is_sharpe DESC LIMIT K
      2. For each candidate, GET /alphas/{id} from BRAIN to refresh metrics
      3. Re-evaluate quality_status against tier-specific thresholds
      4. Drop demoted ones; sort survivors by sharpe; write state.tier_seeds
      5. If survivors < MIN_TIER_SEED_COUNT, signal early stop

    Note on transactions: this node calls db.commit() implicitly via
    apply_quality_status_change. Demotions during refresh are persisted before
    the wrap loop starts.
    """
    node_name = "TIER_SEED_LOAD"
    trace_service = config.get("configurable", {}).get("trace_service") if config else None

    start = time.time()

    # Resolve dependencies from config (avoid circular imports)
    configurable = (config or {}).get("configurable", {}) or {}
    db = configurable.get("db_session")
    brain_adapter = configurable.get("brain_adapter")
    alpha_service = configurable.get("alpha_service")  # provides apply_quality_status_change

    if db is None:
        logger.error(f"[{node_name}] db_session not in config; cannot load seeds")
        return {
            "tier_seeds": [],
            "should_stop": True,
            "early_stop_reason": "tier_seed_load: missing db_session",
        }

    target_tier = state.factor_tier
    prior_tier = target_tier - 1
    daily_goal = state.num_alphas_target
    k = _seed_count_target(daily_goal)

    # 1. Candidate query — restrict to current region; dataset_id constraint
    # is intentionally relaxed here to maximize seed pool (T2/T3 wrappers are
    # dataset-agnostic in practice).
    stmt = (
        select(Alpha)
        .where(Alpha.factor_tier == prior_tier)
        .where(Alpha.quality_status == "PASS")
        .where(Alpha.region == state.region)
        .order_by(Alpha.is_sharpe.desc().nullslast())
        .limit(k)
    )
    rows = (await db.execute(stmt)).scalars().all()

    if not rows:
        logger.warning(
            f"[{node_name}] no PASS T{prior_tier} alphas in region={state.region}"
        )
        return {
            "tier_seeds": [],
            "should_stop": True,
            "early_stop_reason": (
                f"insufficient_fresh_seeds: 0 T{prior_tier} PASS alphas in region {state.region}"
            ),
        }

    # 2. Refresh metrics via BRAIN (best-effort — single failures don't kill batch).
    # PR4 — P0 experiment found BRAIN GET /alphas/{id} returns frozen sim-time
    # snapshots not rolling metrics, so the refresh is a no-op for IS metrics.
    # Gated by settings.TIER_SEED_LOAD_REFRESH_VIA_BRAIN (default False) to save
    # BRAIN budget. Re-enable if you want to detect deleted-alpha edge cases.
    from backend.config import settings as _settings
    refresh_failed = 0
    if brain_adapter is not None and getattr(_settings, "TIER_SEED_LOAD_REFRESH_VIA_BRAIN", False):
        for alpha in rows:
            if not alpha.alpha_id:
                continue
            try:
                fresh = await brain_adapter.get_alpha(alpha.alpha_id)
                if fresh:
                    is_block = fresh.get("is") or {}
                    alpha.is_sharpe = is_block.get("sharpe", alpha.is_sharpe)
                    alpha.is_fitness = is_block.get("fitness", alpha.is_fitness)
                    alpha.is_turnover = is_block.get("turnover", alpha.is_turnover)
                    if "checks" in fresh:
                        merged = dict(alpha.metrics or {})
                        merged["checks"] = fresh["checks"]
                        alpha.metrics = merged
            except Exception as e:
                refresh_failed += 1
                logger.warning(
                    f"[{node_name}] refresh alpha_id={alpha.alpha_id} failed: {e}"
                )

    # 3. Re-evaluate quality_status against tier-specific thresholds.
    # We use predecessor tier's thresholds — the seed's PASS bar is judged by
    # what tier it BELONGS to, not the tier we're wrapping into.
    prior_thresholds = get_tier_thresholds(prior_tier)
    survivors: List[Alpha] = []
    demoted = 0
    for alpha in rows:
        if not _meets_pass(alpha, prior_thresholds):
            demoted += 1
            new_status = "PASS_PROVISIONAL"
            if alpha_service is not None:
                try:
                    await alpha_service.apply_quality_status_change(
                        alpha_id=alpha.id,
                        new_status=new_status,
                        reason=f"tier_seed_refresh: drifted below T{prior_tier} threshold",
                        source="tier_seed_refresh",
                    )
                except Exception as e:
                    logger.warning(
                        f"[{node_name}] apply_quality_status_change failed: {e}"
                    )
            continue
        survivors.append(alpha)

    # 4. Materialize seeds for state
    survivors.sort(key=lambda a: a.is_sharpe or 0, reverse=True)
    from datetime import datetime as _dt
    snapshot_at = _dt.utcnow().isoformat()
    tier_seeds = [
        {
            "alpha_id": a.id,                      # DB id (used as parent_alpha_id)
            "brain_alpha_id": a.alpha_id,
            "expression": a.expression,
            "region": a.region,
            "dataset_id": a.dataset_id,
            "metrics": {
                "sharpe": a.is_sharpe,
                "fitness": a.is_fitness,
                "turnover": a.is_turnover,
                "returns": a.is_returns,
            },
            "snapshot_at": snapshot_at,
        }
        for a in survivors
    ]

    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        f"[{node_name}] tier={target_tier} seeds_loaded={len(rows)} "
        f"refresh_failed={refresh_failed} demoted={demoted} survived={len(tier_seeds)} "
        f"duration={duration_ms}ms"
    )

    # 5. Early stop if too few survivors
    min_seeds = get_min_seed_count()
    early_stop = len(tier_seeds) < min_seeds
    early_stop_reason = (
        f"insufficient_fresh_seeds: {len(tier_seeds)} < {min_seeds} after refresh"
        if early_stop else None
    )

    if trace_service:
        await record_trace(
            state, trace_service, node_name,
            input_data={"tier": target_tier, "K": k, "region": state.region},
            output_data={
                "tier": target_tier,
                "seeds_loaded": len(rows),
                "fresh_after_refresh": len(tier_seeds),
                "demoted": demoted,
                "refresh_failed": refresh_failed,
                "early_stop": early_stop,
            },
            duration_ms=duration_ms,
            status="SUCCESS" if not early_stop else "WARNING",
        )

    result: Dict = {
        "tier_seeds": tier_seeds,
        "current_seed_index": 0,
    }
    if early_stop:
        result["should_stop"] = True
        result["early_stop_reason"] = early_stop_reason
    return result


def _meets_pass(alpha: Alpha, t: Dict) -> bool:
    """Inline tier-PASS check using the tier_thresholds dict."""
    s = alpha.is_sharpe or 0
    f = alpha.is_fitness or 0
    to = alpha.is_turnover or 0
    return (
        s >= t["sharpe_min"]
        and f >= t["fitness_min"]
        and t["turnover_min"] <= to <= t["turnover_max"]
    )


async def node_tier_strategy_select(
    state: MiningState, config: RunnableConfig = None
) -> Dict:
    """LLM picks T2 or T3 strategy for the current seed (one per round)."""
    node_name = "STRATEGY_SELECT"
    trace_service = config.get("configurable", {}).get("trace_service") if config else None

    start = time.time()

    # Resolve current seed
    seeds = state.tier_seeds or []
    idx = state.current_seed_index
    if idx >= len(seeds):
        logger.warning(f"[{node_name}] seed_index out of range ({idx} >= {len(seeds)})")
        return {"current_strategy": None, "current_seed": None}

    seed = seeds[idx]
    llm_service = get_llm_service()

    # L1 Anti-collapse: forward dedup blacklist accumulated this run so
    # T2/T3 wrapper LLM stops re-emitting the same group/template combos.
    dedup_skels = list(state.recent_dedup_skeletons or [])

    # L1 ε-greedy explore: per-round coin flip — escape narrow wrapper
    # combinatorial space (T2 ≈ 5 wrapper × 4 group ≈ 20 combos so collapse
    # is sharp; explore directs LLM toward less-common slots).
    explore_mode = (
        random.random() < float(getattr(settings, "EXPLORE_BUDGET_PCT", 0.3) or 0.3)
    )
    if explore_mode:
        logger.info(
            f"[STRATEGY_SELECT] L1 EXPLORE round T{state.factor_tier} "
            f"seed_idx={idx} (ε-greedy fired)"
        )

    if state.factor_tier == 2:
        strategy = await select_t2_strategy_via_llm(
            seed_expression=seed["expression"],
            seed_metrics=seed.get("metrics") or {},
            region=state.region,
            dataset_id=state.dataset_id or seed.get("dataset_id") or "",
            llm_service=llm_service,
            dedup_skeletons=dedup_skels,
            explore_mode=explore_mode,
        )
    elif state.factor_tier == 3:
        strategy = await select_t3_strategy_via_llm(
            seed_t2_expression=seed["expression"],
            seed_metrics=seed.get("metrics") or {},
            region=state.region,
            dataset_id=state.dataset_id or seed.get("dataset_id") or "",
            llm_service=llm_service,
            dedup_skeletons=dedup_skels,
            explore_mode=explore_mode,
        )
    else:
        logger.error(f"[{node_name}] tier {state.factor_tier} not supported by tier_seed nodes")
        return {"current_strategy": None, "current_seed": seed}

    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        f"[{node_name}] T{state.factor_tier} seed_idx={idx} "
        f"alpha_id={seed.get('alpha_id')} duration={duration_ms}ms"
    )

    if trace_service:
        await record_trace(
            state, trace_service, node_name,
            input_data={
                "tier": state.factor_tier,
                "seed_index": idx,
                "seed_alpha_id": seed.get("alpha_id"),
                "seed_expression": seed["expression"][:200],
                # L1 ε-greedy observability for T2/T3 wrappers.
                "explore_mode": explore_mode,
                "dedup_blacklist_size": len(dedup_skels),
            },
            output_data={
                "rationale": getattr(strategy, "rationale", ""),
                "skip_reasons": getattr(strategy, "skip_reasons", {}) or {},
            },
            duration_ms=duration_ms,
            status="SUCCESS",
        )

    return {
        "current_strategy": strategy.model_dump(),
        "current_seed": seed,
    }


async def node_tier_wrap_one(
    state: MiningState, config: RunnableConfig = None
) -> Dict:
    """Materialize wrapper variants from current_strategy + current_seed."""
    node_name = "TIER_WRAP"
    trace_service = config.get("configurable", {}).get("trace_service") if config else None

    start = time.time()

    seed = state.current_seed
    strat_dict = state.current_strategy
    if seed is None or strat_dict is None:
        logger.warning(f"[{node_name}] no seed/strategy on state — skipping")
        return {"pending_alphas": [], "current_alpha_index": 0}

    if state.factor_tier == 2:
        try:
            strategy = T2Strategy(**strat_dict)
        except Exception as e:
            logger.warning(f"[{node_name}] T2Strategy deser failed: {e}; using DEFAULT")
            strategy = DEFAULT_T2_STRATEGY
        variants = expand_t2_strategy(seed["expression"], strategy, region=state.region)
    elif state.factor_tier == 3:
        try:
            strategy = T3Strategy(**strat_dict)
        except Exception as e:
            logger.warning(f"[{node_name}] T3Strategy deser failed: {e}; using DEFAULT")
            strategy = DEFAULT_T3_STRATEGY
        # Plan v5+ §决策 3 (2026-05-06): pull hypothesis_signal from seed's
        # ancestor hypothesis (B4 link). When present, expand_t3_strategy
        # uses theme-matched trade_when conditions instead of generic
        # 6-template fallback. seed dict carries alpha row metadata
        # including hypothesis_id (set by node_tier_seed_load); we look up
        # expected_signal one extra hop.
        hypothesis_signal = None
        seed_hid = seed.get("hypothesis_id")
        if seed_hid is not None:
            try:
                from backend.models import Hypothesis as _H
                # V-27.D: pure read — reuse the workflow-injected db_session.
                async with resolve_db(config) as _hdb:
                    _h = await _hdb.get(_H, seed_hid)
                    if _h is not None:
                        hypothesis_signal = _h.expected_signal
            except Exception as _e:
                logger.debug(f"[{node_name}] hypothesis lookup failed (ok): {_e}")
        variants = expand_t3_strategy(
            seed["expression"], strategy,
            region=state.region,
            hypothesis_signal=hypothesis_signal,
        )
    else:
        logger.error(f"[{node_name}] tier {state.factor_tier} not supported")
        return {"pending_alphas": [], "current_alpha_index": 0}

    pending = [
        AlphaCandidate(
            expression=v["expression"],
            hypothesis=getattr(strategy, "rationale", "") or "",
            explanation=f"T{state.factor_tier} wrap of seed alpha_id={seed.get('alpha_id')}",
            parent_alpha_id=seed.get("alpha_id"),
            wrapper_kind=v.get("wrapper_kind"),
            # PR6 fix — variants are already validated by _dedup_and_validate
            # (semantic + tier roundtrip checks). Mark them is_valid=True so
            # node_simulate doesn't skip them. Without this the T2/T3 path
            # short-circuits (simulate sees no valid alphas) and every variant
            # ends up FAILed with empty metrics → evaluate's sharpe gate trips.
            is_valid=True,
            metadata={
                "tier": state.factor_tier,
                "seed_brain_alpha_id": seed.get("brain_alpha_id"),
                "seed_index": state.current_seed_index,
            },
        )
        for v in variants
    ]

    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        f"[{node_name}] T{state.factor_tier} seed_idx={state.current_seed_index} "
        f"variants={len(pending)} duration={duration_ms}ms"
    )

    if trace_service:
        await record_trace(
            state, trace_service, node_name,
            input_data={
                "tier": state.factor_tier,
                "seed_index": state.current_seed_index,
                "seed_alpha_id": seed.get("alpha_id"),
            },
            output_data={
                "n_variants": len(pending),
                "wrapper_kinds": [v.get("wrapper_kind") for v in variants],
            },
            duration_ms=duration_ms,
            status="SUCCESS",
        )

    return {
        "pending_alphas": pending,
        "current_alpha_index": 0,
    }
