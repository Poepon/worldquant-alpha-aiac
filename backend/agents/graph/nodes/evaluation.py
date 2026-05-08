"""
Evaluation nodes for LangGraph workflow.

Enhanced with hypothesis-implementation alignment checking:
- Verifies implementations correctly reflect hypotheses
- Attributes failures to hypothesis vs implementation
- Filters knowledge based on attribution confidence

Contains:
- node_simulate: Batch simulate alphas on BRAIN platform
- node_evaluate: Evaluate alpha quality using multi-objective scoring
"""

import time
import random
from typing import Dict, List, Optional, Tuple
from loguru import logger
from langchain_core.runnables import RunnableConfig

from backend.agents.graph.state import MiningState
from backend.agents.graph.nodes.base import (
    record_trace,
    _debug_log,
    EXPERIMENT_TRACKING_ENABLED,
    get_current_experiment,
)
from backend.adapters.brain_adapter import BrainAdapter
from backend.config import settings
from backend.agents.prompts import (
    quick_alignment_check,
    determine_attribution_heuristic,
)


# =============================================================================
# Helpers
# =============================================================================

def _check_is_os_consistency(metrics: Dict) -> bool:
    """V-12: reject alphas whose IS sharpe far exceeds OS sharpe.

    Spike (2026-05-02 → 03) revealed train_sharpe values up to 16.2 paired
    with test_sharpe=0 — pure IS overfit. PASS gate must require OS
    consistency for elevated IS sharpe.

    Tiered rules (calibrated against Spike T1 healthy ratio 76% / T2 35%):
      - is_sharpe < 2:    no OS check (conservative IS already)
      - 2 <= is_sharpe < 5: require os_sharpe > 0 AND os/is >= 0.3
      - is_sharpe >= 5:   require os_sharpe > 0 AND os/is >= 0.4

    OS sharpe sources, in priority order:
      1. metrics["os_sharpe"]           (BRAIN OS-evaluated sharpe)
      2. metrics["test_sharpe"]         (BRAIN test-period split)
      Both null/zero → reject (no OS evidence).

    Returns True if the alpha is safe (i.e., not over-fit by this rule).
    """
    is_sh = (metrics.get("sharpe") if isinstance(metrics, dict) else None) or 0
    if is_sh < 2:
        return True
    os_sh = 0.0
    if isinstance(metrics, dict):
        os_sh = metrics.get("os_sharpe") or metrics.get("test_sharpe") or 0
    if os_sh is None or os_sh <= 0:
        return False
    ratio = os_sh / is_sh if is_sh > 0 else 0
    threshold = 0.4 if is_sh >= 5 else 0.3
    return ratio >= threshold


# =============================================================================
# V-16: Suspicion mode for sharpe > 3.0 alphas
# =============================================================================
# Triggered when is_sharpe > V16_SUSPICION_THRESHOLD. Six static + dynamic
# checks against well-known quant risks. Hard flags downgrade PASS →
# PASS_PROVISIONAL; soft + info flags only annotate trace_steps for review.
#
# This is NOT a substitute for V-12 (IS/OS consistency). V-12 catches
# train→test sharpe collapse; V-16 catches "too good to be true" patterns
# that survive V-12 because train AND test both look strong (e.g., perfect
# divide-by-something-tiny throughout the test window).

import re as _re_v16

V16_SUSPICION_THRESHOLD: float = 3.0

# Fields that can be 0 (returns on no-trade days, volume on halts, etc.)
_V16_DIVIDE_RISKY_DENOMS: set = {
    "returns", "volume", "amount",
    # Fundamental fields can be 0 / negative for distressed firms
    "net_income", "fnd6_newa2v1300_ni",
    "ebit", "fnd6_newa2v1300_oiadp",
    "total_equity", "fnd6_newa1v1300_ceq",
    # Synthetic-zero risks
    "high", "low",  # rare but high==low on illiquid
}

# Fields that arrive at announcement boundary; need ts_delay wrapping
_V16_LOOKAHEAD_FIELDS: tuple = (
    "actual_eps_value", "actual_sales_value",
    "actual_cashflow_per_share_value",
    "actual_dividend_value",
    # Earnings-event fields
    "fam_earn_date", "fam_earn_announce",
)

# Standard rolling-window sizes — anything outside is suspicious of
# parameter mining
_V16_STANDARD_WINDOWS: set = {1, 2, 3, 5, 10, 15, 20, 30, 60, 90, 120, 240, 480, 1200}

_V16_DIVIDE_RE = _re_v16.compile(r"divide\s*\(\s*[^,()]+,\s*([a-zA-Z_][\w]*)\s*\)")
_V16_TS_WINDOW_RE = _re_v16.compile(r"\bts_\w+\s*\([^,()]+,\s*(\d+)\b")


def _v16_check_divide_by_zero(expression: str) -> str | None:
    """Risk 1: divide() with denominator that may be 0 on some dates."""
    if not expression:
        return None
    for m in _V16_DIVIDE_RE.finditer(expression):
        denom = m.group(1).lower()
        if denom in _V16_DIVIDE_RISKY_DENOMS:
            return f"divide(_, {denom}) — denominator can be 0"
    return None


def _v16_check_lookahead(expression: str) -> str | None:
    """Risk 2: announcement-type fields must be ts_delay-wrapped."""
    if not expression:
        return None
    el = expression.lower()
    for field in _V16_LOOKAHEAD_FIELDS:
        if field not in el:
            continue
        # ts_delay must wrap the field. Heuristic: ts_delay( appears at
        # smaller index than the field's first occurrence in same nesting.
        idx_field = el.find(field)
        idx_delay = el.rfind("ts_delay", 0, idx_field)
        if idx_delay == -1:
            return f"announcement field '{field}' used without ts_delay wrapping"
    return None


def _v16_check_overfit_window(expression: str) -> str | None:
    """Risk 5: ts_op uses non-standard window size suggesting parameter mining."""
    if not expression:
        return None
    weird = []
    for m in _V16_TS_WINDOW_RE.finditer(expression):
        n = int(m.group(1))
        if n > 1 and n not in _V16_STANDARD_WINDOWS:
            weird.append(n)
    if weird:
        return f"ts_op uses non-standard windows {weird} (standard: 5/10/20/60/120/240)"
    return None


def _v16_check_outliers(metrics: Dict) -> list:
    """Risk 6: data-anomaly metrics."""
    flags = []
    if not isinstance(metrics, dict):
        return flags
    returns = metrics.get("returns") or 0
    drawdown = metrics.get("drawdown") or 0
    fitness = metrics.get("fitness") or 0
    sharpe = metrics.get("sharpe") or 0
    if returns > 1.0:  # >100% annual return
        flags.append(f"returns={returns:.2%} unrealistic for diversified portfolio")
    if drawdown == 0 and abs(sharpe) > 0.5:
        flags.append("drawdown=0 with non-trivial sharpe — simulation anomaly likely")
    if fitness > 10 and sharpe < 5:
        flags.append(f"fitness={fitness:.1f} but sharpe={sharpe:.1f} — fitness/sharpe inconsistency")
    return flags


def _v16_check_cost_vacuum(metrics: Dict) -> str | None:
    """Risk 4: high turnover + extreme sharpe = cost-model insensitive alpha."""
    if not isinstance(metrics, dict):
        return None
    turnover = metrics.get("turnover") or 0
    sharpe = metrics.get("sharpe") or 0
    # >50% turnover + sharpe>5 means the alpha trades aggressively yet still
    # claims abnormal returns. BRAIN cost-models, but the alpha may exploit
    # specific cost-model gaps (e.g., unrealistic instant fills).
    if turnover > 0.50 and sharpe > 5:
        return f"turnover={turnover:.2f} + sharpe={sharpe:.2f} — cost-model insensitivity risk"
    return None


def _run_suspicion_checks(metrics: Dict, expression: str) -> list:
    """V-16: full 6-risk audit when is_sharpe > V16_SUSPICION_THRESHOLD.

    Returns list[dict] with shape:
      {"check": str, "severity": "hard" | "soft" | "info", "evidence": str}

    Severity semantics:
      hard — downgrade PASS → PASS_PROVISIONAL (alpha needs review)
      soft — annotate metrics, keep status
      info — manual-only, e.g. survivorship bias

    Returns [] when sharpe ≤ threshold.
    """
    flags: list = []
    if not isinstance(metrics, dict):
        return flags
    sharpe = metrics.get("sharpe") or 0
    if sharpe <= V16_SUSPICION_THRESHOLD:
        return flags

    # Risk 1: divide by zero
    flag = _v16_check_divide_by_zero(expression)
    if flag:
        flags.append({"check": "divide_by_zero", "severity": "soft", "evidence": flag})

    # Risk 2: lookahead bias
    flag = _v16_check_lookahead(expression)
    if flag:
        flags.append({"check": "lookahead_bias", "severity": "hard", "evidence": flag})

    # Risk 3: survivorship bias — system-level, manual review only
    flags.append({
        "check": "survivorship_bias",
        "severity": "info",
        "evidence": "BRAIN universe selection inherits survivorship; review at portfolio construction.",
    })

    # Risk 4: cost vacuum
    flag = _v16_check_cost_vacuum(metrics)
    if flag:
        flags.append({"check": "cost_vacuum", "severity": "hard", "evidence": flag})

    # Risk 5: overfit window
    flag = _v16_check_overfit_window(expression)
    if flag:
        flags.append({"check": "overfit_window", "severity": "soft", "evidence": flag})

    # Risk 6: data-anomaly outliers
    for outlier_msg in _v16_check_outliers(metrics):
        flags.append({"check": "outlier_metric", "severity": "hard", "evidence": outlier_msg})

    return flags


# =============================================================================
# NODE: Simulate
# =============================================================================

async def node_simulate(
    state: MiningState,
    brain: BrainAdapter,
    config: RunnableConfig = None
) -> Dict:
    """
    Batch simulate ALL valid alphas on BRAIN platform.
    
    Enhanced with DB-level deduplication:
    - Check expression hash against existing alphas before simulation
    - Skip already-simulated expressions to save API calls
    
    Input State:
        - pending_alphas, region, universe
    
    Output Updates:
        - pending_alphas (with simulation result)
        - trace_steps
    """
    start_time = time.time()
    node_name = "SIMULATE"
    
    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    
    # Filter valid alphas that haven't been simulated
    valid_indices = [
        i for i, a in enumerate(state.pending_alphas)
        if a.is_valid and not a.simulation_success
    ]
    
    if not valid_indices:
        logger.warning(f"[{node_name}] No valid alphas to simulate")
        return {}
    
    # DB-level deduplication check
    db_duplicates = 0
    indices_to_simulate = []
    
    try:
        from backend.database import AsyncSessionLocal
        from backend.selection_strategy import filter_unsimulated_expressions
        
        expressions_to_check = [state.pending_alphas[i].expression for i in valid_indices]
        
        async with AsyncSessionLocal() as db:
            new_exprs, dup_exprs = await filter_unsimulated_expressions(
                db, expressions_to_check, state.region, state.universe
            )
        
        new_expr_set = set(new_exprs)
        for idx in valid_indices:
            expr = state.pending_alphas[idx].expression
            if expr in new_expr_set:
                indices_to_simulate.append(idx)
            else:
                db_duplicates += 1
                state.pending_alphas[idx].simulation_error = "DB duplicate: already simulated"
                state.pending_alphas[idx].is_simulated = True
                state.pending_alphas[idx].simulation_success = False
        
        logger.info(
            f"[{node_name}] DB dedup: {db_duplicates} duplicates skipped, "
            f"{len(indices_to_simulate)} to simulate"
        )
        
    except Exception as e:
        logger.warning(f"[{node_name}] DB dedup check failed, proceeding with all: {e}")
        indices_to_simulate = valid_indices
    
    if not indices_to_simulate:
        logger.warning(f"[{node_name}] All expressions already in DB")
        return {"pending_alphas": state.pending_alphas}

    # Pre-simulate self-corr check (2026-05-09): drop candidates whose
    # skeleton matches an already-submitted alpha — they would fail BRAIN's
    # server-side self-correlation gate at submission, so simulating wastes
    # BRAIN config quota. Cheap O(1) hashset lookup, runs before the
    # ML-based filter below. Cache loaded from
    # backend/data/correlation_cache/submitted_portfolio_{region}.json.
    try:
        from backend.agents.seed_pool.portfolio_skeletons import (
            get_portfolio_skeleton_set,
        )
        from backend.knowledge_extraction import expression_to_skeleton
        portfolio_skels = get_portfolio_skeleton_set(state.region)
        if portfolio_skels:
            keep_after_skel: list[int] = []
            skel_dups = 0
            for idx in indices_to_simulate:
                expr = state.pending_alphas[idx].expression or ""
                try:
                    sk = expression_to_skeleton(expr, max_depth=3)
                except Exception:
                    sk = None
                if sk and sk in portfolio_skels:
                    skel_dups += 1
                    state.pending_alphas[idx].simulation_error = (
                        f"portfolio skeleton duplicate (self-corr risk): {sk[:60]}"
                    )
                    state.pending_alphas[idx].is_simulated = True
                    state.pending_alphas[idx].simulation_success = False
                else:
                    keep_after_skel.append(idx)
            if skel_dups:
                logger.info(
                    f"[{node_name}] portfolio-skel dedup: {skel_dups} candidates "
                    f"matched submitted skeletons (saved BRAIN sims), "
                    f"{len(keep_after_skel)} remain"
                )
            indices_to_simulate = keep_after_skel
            if not indices_to_simulate:
                logger.warning(f"[{node_name}] All candidates dropped by portfolio-skel dedup")
                return {"pending_alphas": state.pending_alphas}
    except Exception as e:
        logger.warning(f"[{node_name}] portfolio-skel dedup failed, proceeding: {e}")

    # Plan v5+ #3 (2026-05-07): pre-simulate skeleton classifier filter.
    # When ENABLE_PRE_SIMULATE_FILTER=True, predict P(PASS) per candidate
    # and skip very-likely-fails BEFORE sending to BRAIN simulate. Default
    # OFF; opt-in via .env. Conservative threshold 0.05 keeps 99% PASS
    # recall on the training-set CV (AUC=0.813).
    if getattr(settings, "ENABLE_PRE_SIMULATE_FILTER", False):
        try:
            from backend.agents.services.pre_simulate_filter import filter_candidates
            threshold = float(getattr(settings, "PRE_SIMULATE_FILTER_THRESHOLD", 0.05))
            cand_exprs = [state.pending_alphas[i].expression for i in indices_to_simulate]
            keep_local, skip_local, probas = filter_candidates(
                cand_exprs, threshold=threshold,
            )
            if skip_local:
                # Translate skip_local positions back to original indices_to_simulate
                pre_sim_skipped: list = []
                for local_idx in skip_local:
                    orig_idx = indices_to_simulate[local_idx]
                    p_pass = probas[local_idx]
                    pre_sim_skipped.append(orig_idx)
                    a = state.pending_alphas[orig_idx]
                    a.simulation_error = (
                        f"pre-simulate filter skip: P(PASS)={p_pass:.3f} < {threshold}"
                    )
                    a.is_simulated = True
                    a.simulation_success = False
                # Reduce indices_to_simulate to keepers only
                indices_to_simulate = [
                    indices_to_simulate[i] for i in keep_local
                ]
                logger.info(
                    f"[{node_name}] pre-simulate filter: skipped={len(pre_sim_skipped)} "
                    f"keep={len(indices_to_simulate)} threshold={threshold}"
                )
        except Exception as _filter_e:
            logger.warning(
                f"[{node_name}] pre-simulate filter failed (proceed with all): {_filter_e}"
            )

    if not indices_to_simulate:
        logger.warning(
            f"[{node_name}] All expressions filtered by pre-simulate classifier"
        )
        return {"pending_alphas": state.pending_alphas}

    logger.info(f"[{node_name}] Starting batch simulation | count={len(indices_to_simulate)} region={state.region}")
    
    expressions = [state.pending_alphas[i].expression for i in indices_to_simulate]
    
    _debug_log("E", "nodes.py:simulate:expressions", "Expressions to simulate", {
        "count": len(expressions),
        "expressions": [e[:150] for e in expressions],
        "region": state.region,
        "universe": state.universe
    })
    
    # A1: smart simulation settings — per-expression settings choice based on
    # structural form (group_neutralize → neut=NONE, trade_when → decay=0,
    # etc.) and field category. When enabled, bucket expressions by their
    # chosen settings tuple and call simulate_batch per bucket; results are
    # merged back to original index order.
    smart_enabled = getattr(settings, "ENABLE_SMART_SIM_SETTINGS", False)
    smart_settings_per_idx: Dict[int, Dict] = {}  # local_index → settings dict
    smart_reasons_per_idx: Dict[int, str] = {}

    if smart_enabled:
        from backend.sim_settings import settings_reason, smart_simulation_settings

        SETTINGS_KEYS = ("region", "universe", "delay", "decay", "neutralization", "truncation", "test_period")
        buckets: Dict[Tuple, List[int]] = {}
        for local_i, idx in enumerate(indices_to_simulate):
            expr = state.pending_alphas[idx].expression
            smart = smart_simulation_settings(
                expr,
                tier=getattr(state, "factor_tier", None),
                region=state.region,
                universe=state.universe,
            )
            smart_settings_per_idx[local_i] = smart
            smart_reasons_per_idx[local_i] = settings_reason(
                expr, tier=getattr(state, "factor_tier", None)
            )
            key = tuple(smart.get(k) for k in SETTINGS_KEYS)
            buckets.setdefault(key, []).append(local_i)

        logger.info(
            f"[{node_name}] smart-settings: {len(buckets)} bucket(s) for "
            f"{len(indices_to_simulate)} expressions"
        )

        results = [None] * len(indices_to_simulate)
        for settings_key, local_indices in buckets.items():
            bucket_kwargs = dict(zip(SETTINGS_KEYS, settings_key))
            bucket_exprs = [expressions[li] for li in local_indices]
            try:
                bucket_results = await brain.simulate_batch(
                    expressions=bucket_exprs,
                    **bucket_kwargs,
                )
            except Exception as e:
                logger.error(f"[{node_name}] bucket sim error ({settings_key}): {e}")
                bucket_results = [{"success": False, "error": str(e)} for _ in bucket_exprs]
            for j, li in enumerate(local_indices):
                results[li] = bucket_results[j] if j < len(bucket_results) else {"success": False, "error": "Missing"}
    else:
        try:
            results = await brain.simulate_batch(
                expressions=expressions,
                region=state.region,
                universe=state.universe,
                delay=1,
                decay=4,
                neutralization="SUBINDUSTRY"
            )
        except Exception as e:
            logger.error(f"[{node_name}] Batch Simulate Loop Error: {e}")
            results = [{"success": False, "error": str(e)} for _ in expressions]
    
    duration_ms = int((time.time() - start_time) * 1000)
    
    # Update alphas
    updated_alphas = state.pending_alphas.copy()
    success_count = 0
    
    for i, idx in enumerate(indices_to_simulate):
        res = results[i] if i < len(results) else {"success": False, "error": "Missing result"}
        
        current = updated_alphas[idx]
        updated = current.model_copy()
        
        updated.is_simulated = True
        updated.simulation_success = res.get("success", False)
        updated.alpha_id = res.get("alpha_id")
        updated.metrics = res.get("metrics", {}) or {}
        updated.simulation_error = res.get("error")

        # A1: stamp smart-settings metadata into metrics for audit / KB insight
        if smart_enabled and i in smart_settings_per_idx:
            updated.metrics = {
                **updated.metrics,
                "_sim_settings": smart_settings_per_idx[i],
                "_sim_settings_reason": smart_reasons_per_idx.get(i, ""),
            }

        if updated.simulation_success:
            success_count += 1

        updated_alphas[idx] = updated
    
    failed_errors = [
        {"expr": expressions[i][:80], "error": results[i].get("error", "unknown")[:200]}
        for i in range(len(results)) if not results[i].get("success")
    ]
    
    _debug_log("E", "nodes.py:simulate:result", "Simulation complete", {
        "total_to_simulate": len(indices_to_simulate),
        "success": success_count,
        "failed": len(indices_to_simulate) - success_count,
        "db_duplicates_skipped": db_duplicates,
        "duration_ms": duration_ms,
        "success_rate": round(success_count / max(1, len(indices_to_simulate)) * 100, 1),
        "failed_errors": failed_errors[:5]
    })
    
    logger.info(f"[{node_name}] Complete | success={success_count}/{len(indices_to_simulate)} db_skipped={db_duplicates}")
    
    # Experiment tracking
    if EXPERIMENT_TRACKING_ENABLED:
        exp = get_current_experiment()
        if exp:
            exp.metrics.increment("simulation_count", len(indices_to_simulate))
            exp.metrics.record("dedup_skip_rate",
                (db_duplicates / (len(indices_to_simulate) + db_duplicates) * 100)
                if (len(indices_to_simulate) + db_duplicates) > 0 else 0,
                tags={"node": node_name, "region": state.region}
            )
            exp.metrics.record("simulation_success_rate",
                (success_count / len(indices_to_simulate) * 100) if len(indices_to_simulate) > 0 else 0,
                tags={"node": node_name}
            )
    
    trace_update = await record_trace(
        state, trace_service, node_name,
        {
            "batch_size": len(indices_to_simulate),
            "db_duplicates_skipped": db_duplicates,
            "expressions": [e[:50] for e in expressions[:10]]
        },
        {
            "success_count": success_count,
            "simulated_count": len(indices_to_simulate),
            "db_duplicates": db_duplicates,
            "results": [{"id": r.get("alpha_id"), "err": r.get("error")} for r in results[:20]]
        },
        duration_ms,
        "SUCCESS" if success_count > 0 else "PARTIAL_FAILURE"
    )
    
    return {
        "pending_alphas": updated_alphas,
        **trace_update
    }


# =============================================================================
# NODE: Evaluate Quality
# =============================================================================

async def node_evaluate(
    state: MiningState,
    brain: BrainAdapter = None,
    config: RunnableConfig = None
) -> Dict:
    """
    Evaluate alpha quality using multi-objective scoring.
    
    Enhanced with:
    - Two-stage correlation checking
    - BRAIN platform official checks integration (checks 数组)
    - Pyramid multiplier consideration for prioritization
    
    Input State:
        - pending_alphas (with simulation results)
    
    Output Updates:
        - pending_alphas (with quality_status and score)
        - trace_steps
    """
    from backend.alpha_scoring import (
        calculate_alpha_score,
        should_optimize,
        get_failed_tests,
        evaluate_with_brain_checks,  # 新增：BRAIN官方检查
    )
    from backend.services.correlation_service import CorrelationService

    start_time = time.time()
    node_name = "EVALUATE"

    trace_service = config.get("configurable", {}).get("trace_service") if config else None

    updated_alphas = state.pending_alphas.copy()
    pass_count = 0
    fail_count = 0
    optimize_count = 0
    corr_checks_performed = 0
    corr_checks_skipped = 0

    # W0.5: local PnL-matrix self-correlation with BRAIN-API fallback.
    # Shared across all alphas in this round to amortise the cache load.
    correlation_service = CorrelationService(brain) if brain is not None else None
    
    logger.info(f"[{node_name}] Starting two-stage evaluation | count={len(state.pending_alphas)}")

    # PR2: tier-aware thresholds + gate config. state.factor_tier is set by the
    # router from agent_mode (AUTONOMOUS_TIER1 → 1, AUTONOMOUS_TIER2 → 2,
    # AUTONOMOUS_TIER3 → 3). For legacy AUTONOMOUS, factor_tier defaults to 1
    # via MiningState; setting ENABLE_FACTOR_TIERING=False keeps it on legacy
    # globals via the tier=None fallback inside tier_thresholds.
    from backend.agents.graph.tier_thresholds import get_tier_thresholds

    tier_cfg = get_tier_thresholds(getattr(state, "factor_tier", None))
    sharpe_min = tier_cfg["sharpe_min"]
    fitness_min = tier_cfg["fitness_min"]
    turnover_min = tier_cfg["turnover_min"]
    turnover_max = tier_cfg["turnover_max"]
    max_correlation = tier_cfg.get("self_corr_max") or getattr(settings, "MAX_CORRELATION", 0.7)
    check_self_corr = tier_cfg["check_self_corr"]
    check_concentrated = tier_cfg["check_concentrated"]
    # PROVISIONAL config: tier-specific looser bar for near-PASS pool (KB / island GA seeds).
    prov_cfg = tier_cfg.get("provisional") or {}
    prov_sharpe_min = prov_cfg.get("sharpe_min", sharpe_min)
    prov_fitness_min = prov_cfg.get("fitness_min", 0.6)
    prov_turnover_max = prov_cfg.get("turnover_max", 0.85)

    score_pass_threshold = getattr(settings, 'SCORE_PASS_THRESHOLD', 0.8)
    score_optimize_threshold = getattr(settings, 'SCORE_OPTIMIZE_THRESHOLD', 0.3)
    corr_check_threshold = getattr(settings, 'CORR_CHECK_THRESHOLD', 0.5)
    logger.info(
        f"[{node_name}] tier={tier_cfg['tier']} sharpe>={sharpe_min} fitness>={fitness_min} "
        f"turnover [{turnover_min}, {turnover_max}] check_self_corr={check_self_corr} "
        f"check_concentrated={check_concentrated}"
    )
    
    eval_details = []
    failure_feedback_queue = []
    
    for i, alpha in enumerate(updated_alphas):
        # Hard rule: anything that didn't simulate successfully cannot be PASS,
        # regardless of any earlier transient status. Metrics are missing, so
        # gates are unverifiable.
        if not alpha.is_simulated or not alpha.simulation_success:
            alpha.quality_status = "FAIL"
            fail_count += 1
            continue
        
        metrics = alpha.metrics or {}
        
        train_sharpe_val = metrics.get("train_sharpe")
        train_fitness_val = metrics.get("train_fitness")
        test_sharpe_val = metrics.get("test_sharpe")
        test_fitness_val = metrics.get("test_fitness")
        
        # 构建完整的 sim_result，包含 BRAIN 返回的 checks
        sim_result = {
            "train": {
                "sharpe": train_sharpe_val if train_sharpe_val is not None else metrics.get("sharpe", 0),
                "fitness": train_fitness_val if train_fitness_val is not None else metrics.get("fitness", 0),
                "turnover": metrics.get("turnover", 0),
                "returns": metrics.get("returns", 0),
            },
            "test": {
                "sharpe": test_sharpe_val if test_sharpe_val is not None else metrics.get("sharpe", 0) * 0.8,
                "fitness": test_fitness_val if test_fitness_val is not None else metrics.get("fitness", 0),
            },
            "is": {
                "sharpe": metrics.get("sharpe", 0),
                "fitness": metrics.get("fitness", 0),
                "turnover": metrics.get("turnover", 0),
                "drawdown": metrics.get("drawdown", 0),
                "longCount": metrics.get("longCount"),
                "shortCount": metrics.get("shortCount"),
                "checks": metrics.get("checks", []),  # BRAIN 官方检查结果
            },
            "riskNeutralized": metrics.get("riskNeutralized", {}),
            "investabilityConstrained": metrics.get("investabilityConstrained", {}),
            "checks": metrics.get("checks", []),  # 顶层也放一份
            "can_submit": metrics.get("can_submit", False),
        }
        
        # 新增：使用 BRAIN 官方检查结果进行快速判断
        brain_eval = evaluate_with_brain_checks(sim_result)
        brain_can_submit = brain_eval.get('can_submit', False)
        brain_failed_checks = brain_eval.get('failed_checks', [])
        pyramid_info = brain_eval.get('pyramid_info', {})
        pyramid_multiplier = pyramid_info.get('multiplier', 1.0)
        
        # Stage 1: Preliminary score WITHOUT correlation
        preliminary_score = calculate_alpha_score(
            sim_result=sim_result,
            prod_corr=0.0,
            self_corr=0.0
        )
        
        sharpe = metrics.get("sharpe", 0) or 0
        turnover = metrics.get("turnover", 0) or 0
        fitness = metrics.get("fitness", 0) or 0
        
        # 使用 BRAIN 官方检查或本地阈值
        if brain_eval['check_details']:
            # 有官方检查结果，以官方为准
            meets_thresholds = brain_can_submit or (not brain_failed_checks)
        else:
            # Fallback: 使用本地阈值
            meets_thresholds = (
                sharpe >= sharpe_min and
                turnover <= turnover_max and
                fitness >= fitness_min
            )
        
        # Stage 2: Correlation check for promising candidates
        # PR2: T1/T2 tier skips self_corr entirely (check_self_corr=False).
        # The 8-12 LLM-guided wrapper variants per seed are necessarily
        # PnL-correlated; gating them on self_corr would FAIL the whole T2
        # batch. self_corr is only meaningful at T3 (vs already-submitted OS
        # pool), which is where we keep the strict gate.
        prod_corr = 0.0
        self_corr = 0.0
        needs_corr_check = check_self_corr and (
            preliminary_score >= corr_check_threshold or
            meets_thresholds
        )

        if needs_corr_check and brain and alpha.alpha_id:
            corr_checks_performed += 1
            try:
                prod_corr_result = await brain.check_correlation(alpha.alpha_id, check_type="PROD")
                if isinstance(prod_corr_result, dict):
                    prod_corr = float(prod_corr_result.get("max", 0.0) or 0.0)
            except Exception as e:
                logger.warning(f"[{node_name}] PROD correlation check failed for {alpha.alpha_id}: {e}")

            # W0.5: prefer local PnL-matrix; fall back to BRAIN API; finally
            # mark unknown so hard_gate downgrades to PASS_PROVISIONAL.
            self_corr_source = "unknown"
            if correlation_service is not None:
                try:
                    self_corr, self_corr_source = await correlation_service.get_with_fallback(
                        alpha.alpha_id, region=state.region
                    )
                except Exception as e:
                    logger.warning(f"[{node_name}] correlation_service failed for {alpha.alpha_id}: {e}")
                    self_corr_source = "unknown"
            else:
                try:
                    self_corr_result = await brain.check_correlation(alpha.alpha_id, check_type="SELF")
                    if isinstance(self_corr_result, dict):
                        self_corr = float(self_corr_result.get("max", 0.0) or 0.0)
                        self_corr_source = "brain"
                except Exception as e:
                    logger.warning(f"[{node_name}] SELF correlation check failed for {alpha.alpha_id}: {e}")
        else:
            corr_checks_skipped += 1
            # tier_skipped means "by tier policy, not because we couldn't measure" —
            # downstream gate should treat as ok+verified, NOT downgrade to PROVISIONAL
            self_corr_source = "tier_skipped" if not check_self_corr else "skipped"
        
        # Final score with correlation penalty
        score = calculate_alpha_score(
            sim_result=sim_result,
            prod_corr=prod_corr,
            self_corr=self_corr
        )
        
        should_opt, opt_reason = should_optimize(sim_result)
        failed_tests = get_failed_tests(sim_result)

        # Hard skill gate (BRAIN red-line on IS metrics) — see plan §
        # "BRAIN Gate 真实值校准". PASS requires ALL of:
        #   sharpe >= SHARPE_MIN AND fitness >= FITNESS_MIN
        #   AND 0.01 <= turnover <= TURNOVER_MAX
        #   AND sub-universe check not FAIL
        #   AND self_corr < MAX_CORRELATION
        sub_universe_check = next(
            (c for c in metrics.get("checks", [])
             if c.get("name") == "LOW_SUB_UNIVERSE_SHARPE"),
            None,
        )
        sub_universe_ok = (
            sub_universe_check is None
            or sub_universe_check.get("result") != "FAIL"
        )
        # Post-Step1 (2026-04-30): BRAIN /check rejects ~25% of project's PASS
        # alphas on CONCENTRATED_WEIGHT (single position > 10% on some date).
        # Local hard_gate now mirrors that rule using sim_result's checks block.
        concentrated_check = next(
            (c for c in metrics.get("checks", [])
             if c.get("name") == "CONCENTRATED_WEIGHT"),
            None,
        )
        # PR2: T1 skips concentrated_weight check (raw signal evaluation only).
        # T2/T3 keep BRAIN's CONCENTRATED_WEIGHT rule.
        if check_concentrated:
            concentrated_ok = (
                concentrated_check is None
                or concentrated_check.get("result") != "FAIL"
            )
        else:
            concentrated_ok = True

        # PR2: tier-aware self_corr gate. T1/T2 force ok+verified so PASS path
        # is reachable for wrapper variants; T3 uses real self_corr value.
        self_corr_source = locals().get("self_corr_source", "skipped")
        if check_self_corr:
            self_corr_ok = self_corr < max_correlation
            self_corr_verified = self_corr_source not in ("unknown",)
        else:
            self_corr_ok = True
            self_corr_verified = True  # tier_skipped, not unknown

        # V-12 (2026-05-03): IS-only PASS bar lets train_sharpe >> test_sharpe
        # alphas through. Spike data showed T2 train_avg=3.94 / test_avg=0.40
        # (90% decay), with top alphas like sharpe=16.2/test=0.0 — pure IS
        # overfit. Require OS consistency for high-IS-sharpe alphas: above
        # sharpe=2 we need a positive os_sharpe with retention >= 0.3 (or 0.4
        # if sharpe>5). Lower-IS alphas pass without OS check (their own bar
        # is conservative enough).
        is_overfit_safe = _check_is_os_consistency(metrics)

        hard_gate_pass = (
            sharpe >= sharpe_min
            and fitness >= fitness_min
            and turnover_min <= turnover <= turnover_max
            and sub_universe_ok
            and concentrated_ok
            and self_corr_ok
            and self_corr_verified
            and is_overfit_safe
        )

        # PASS_PROVISIONAL: 近成功池 (sharpe + fitness>=0.6 + turnover [0.01,0.85])
        # 用于 KB 学习/island 优化种子，但不视为可提交
        #
        # 议题 B (PnL 硬闸门，规模无关替代 expression injection):
        #   self_corr 由 correlation_service 用 OS PnL 矩阵实测得出 (W0.5)。
        #   - 已验证且 >= 0.7 (self_corr_ok=False, verified=True) → 直接 FAIL，
        #     不污染 PROVISIONAL 池 (重复 alpha 没必要进 KB 学习)
        #   - 未验证 (verified=False, cache miss + API fail) → 仍允许 PROVISIONAL
        #     (defensive：宁可保留候选也不丢真信号)
        #   - skipped (前置门没过自然没算 corr) → ok=True, verified=True (skipped
        #     != unknown), 不影响其他门的判定
        # 这套机制对 OS 池规模无关 — 不论 5 还是 10000 条提交 alpha,
        # 单条 alpha 的 corrwith 都是 O(N列) ~50ms 量级。
        self_corr_acceptable = self_corr_ok or not self_corr_verified
        # PR2: PROVISIONAL bar uses tier-specific looser thresholds (plan §"PASS_PROVISIONAL 阈值").
        # T1: sharpe>0.5, fitness>0.3, turnover<0.85
        # T2: sharpe>0.8, fitness>0.6, turnover<0.65
        # T3: sharpe>=1.3, fitness>=0.8, turnover<0.70
        near_pass = (
            sharpe >= prov_sharpe_min
            and fitness >= prov_fitness_min
            and turnover_min <= turnover <= prov_turnover_max
            and sub_universe_ok
            and concentrated_ok
            and self_corr_acceptable
        )

        # Determine quality status
        if hard_gate_pass and (meets_thresholds or score >= score_pass_threshold):
            # V-16 (2026-05-03): suspicion mode for sharpe > 3.0. Hard flags
            # downgrade PASS → PASS_PROVISIONAL so the alpha is held for
            # review rather than entering KB / submission queue. Soft / info
            # flags only annotate metrics for trace_steps.
            v16_flags = _run_suspicion_checks(metrics, alpha.expression or "")
            hard_flags = [f for f in v16_flags if f.get("severity") == "hard"]
            if v16_flags:
                # Persist annotations on the alpha's metrics (preserved through
                # _incremental_save_alphas / workflow.run_with_persistence)
                if isinstance(alpha.metrics, dict):
                    alpha.metrics["_v16_suspicion_flags"] = v16_flags
                logger.warning(
                    f"[{node_name}] V-16 suspicion mode (sharpe={sharpe:.2f}) | "
                    f"flags={[f['check'] for f in v16_flags]}"
                )
            # Fix C (2026-05-07): BRAIN-aware PASS downgrade.
            # Internal hard_gate uses SHARPE_MIN=1.0 / FITNESS_MIN=0.5 (探索阈值).
            # BRAIN submission gate uses higher bar — top-level fitness ≥ ~1.0,
            # CONCENTRATED_WEIGHT ≤ 10%. Without this check, alpha that pass our
            # gate but BRAIN already FAIL'd on submittable fields are labelled
            # PASS and skip the optimization chain forever (see should_optimize
            # "已接近/达到门槛..." skip branch). Downgrading to PASS_PROVISIONAL
            # routes them into _collect_optimization_candidates so wrapper /
            # window optimizations get a chance to push fitness over BRAIN's bar.
            brain_actionable_fails = [
                c.get("name") for c in brain_failed_checks or []
                if c.get("name") in ("LOW_FITNESS", "LOW_SHARPE", "CONCENTRATED_WEIGHT")
            ]
            if hard_flags:
                alpha.quality_status = "PASS_PROVISIONAL"
                optimize_count += 1
            elif brain_actionable_fails and not brain_can_submit:
                alpha.quality_status = "PASS_PROVISIONAL"
                optimize_count += 1
                if isinstance(alpha.metrics, dict):
                    alpha.metrics["_brain_pass_downgrade"] = brain_actionable_fails
                logger.info(
                    f"[{node_name}] PASS→PROVISIONAL: BRAIN rejected on {brain_actionable_fails} | "
                    f"sharpe={sharpe:.2f} fitness={fitness:.2f} expr={(alpha.expression or '')[:80]}"
                )
            else:
                alpha.quality_status = "PASS"
                pass_count += 1
        elif near_pass:
            alpha.quality_status = "PASS_PROVISIONAL"
            optimize_count += 1
        elif should_opt and score >= score_optimize_threshold:
            alpha.quality_status = "OPTIMIZE"
            optimize_count += 1
        else:
            alpha.quality_status = "FAIL"
            fail_count += 1
            
            # Enhanced: Alignment check and attribution for failures
            # This helps distinguish hypothesis failure from implementation failure
            alignment_issues = []
            attribution = "unknown"
            
            # Get hypothesis from alpha if available
            hypothesis_dict = {}
            if hasattr(alpha, 'hypothesis') and alpha.hypothesis:
                if isinstance(alpha.hypothesis, dict):
                    hypothesis_dict = alpha.hypothesis
                else:
                    hypothesis_dict = {"statement": alpha.hypothesis}
            
            # Quick alignment check
            if hypothesis_dict and alpha.expression:
                is_aligned, alignment_issues = quick_alignment_check(
                    hypothesis_dict, alpha.expression, state.fields
                )
                
                # Determine attribution
                result_dict = {
                    "success": False,
                    "sharpe": sharpe,
                    "fitness": fitness,
                    "turnover": turnover,
                }
                attribution = determine_attribution_heuristic(
                    result_dict, alignment_issues, alpha.validation_error
                )
                
                if not is_aligned:
                    logger.debug(
                        f"[{node_name}] Alignment issues for {alpha.alpha_id}: {alignment_issues[:2]}"
                    )
            
            # Determine error type
            # P0: BRAIN check FAIL 优先匹配（来自 metrics.checks），它们指向具体
            # settings/结构修法（truncation / neutralization / sub-universe），
            # 比 sharpe/fitness/turnover 通用归因更可操作。
            error_type = "QUALITY_FAIL"
            brain_fail_priority = (
                "CONCENTRATED_WEIGHT",
                "LOW_SUB_UNIVERSE_SHARPE",
                "HIGH_PROD_CORRELATION",
                "HIGH_SELF_CORRELATION",
            )
            brain_fails = {
                c.get("name"): c
                for c in metrics.get("checks", []) or []
                if c.get("result") == "FAIL"
            }
            for name in brain_fail_priority:
                if name in brain_fails:
                    error_type = name
                    break
            if error_type == "QUALITY_FAIL":
                # Fallback: 通用 metric-band 归因
                if sharpe < sharpe_min:
                    error_type = "LOW_SHARPE"
                elif fitness < fitness_min:
                    error_type = "LOW_FITNESS"
                elif turnover > turnover_max:
                    error_type = "HIGH_TURNOVER"
                elif sharpe < 0:
                    error_type = "NEGATIVE_SIGNAL"
            
            if alpha.expression:
                failure_feedback_queue.append({
                    "expression": alpha.expression,
                    "error_type": error_type,
                    "metrics": metrics,
                    "region": state.region,
                    "dataset_id": state.dataset_id,
                    # New: attribution info for knowledge filtering
                    "hypothesis": hypothesis_dict.get("statement", ""),
                    "alignment_issues": alignment_issues,
                    "attribution": attribution,
                })
        
        # Store detailed metrics with BRAIN checks info
        alpha.metrics = {
            **metrics,
            "_score": round(score, 4),
            "_preliminary_score": round(preliminary_score, 4),
            "_prod_corr": round(prod_corr, 4) if prod_corr else None,
            "_self_corr": round(self_corr, 4) if self_corr else None,
            "_corr_checked": needs_corr_check,
            "_should_optimize": should_opt,
            "_optimize_reason": opt_reason,
            "_failed_tests": failed_tests,
            # BRAIN 官方检查信息
            "_brain_can_submit": brain_can_submit,
            "_brain_failed_checks": brain_failed_checks,
            "_brain_pending_checks": brain_eval.get('pending_checks', []),
            "_pyramid_multiplier": pyramid_multiplier,
        }
        
        _debug_log("F", "nodes.py:evaluate:alpha_detail", f"Alpha evaluated: {alpha.quality_status}", {
            "alpha_id": alpha.alpha_id,
            "expression": alpha.expression[:80] if alpha.expression else None,
            "sharpe": round(sharpe, 3),
            "fitness": round(fitness, 3),
            "turnover": round(turnover, 3),
            "score": round(score, 3),
            "status": alpha.quality_status
        })
        
        eval_details.append({
            "id": alpha.alpha_id,
            "status": alpha.quality_status,
            "score": round(score, 4),
            "sharpe": sharpe,
            "fitness": fitness,
            "turnover": turnover,
            "corr_checked": needs_corr_check,
            "optimize_reason": opt_reason if should_opt else None,
        })
        
        updated_alphas[i] = alpha

    # PR5 — T1 sign-flip retry. For each FAIL alpha whose |sharpe| ≥
    # T1_FLIP_RETRY_SHARPE (i.e. a real signal pointing the wrong direction,
    # not just statistical noise), simulate the negated expression and
    # re-evaluate. Bounded by T1_FLIP_RETRY_CAP. Only enabled at T1 because
    # T2/T3 already operate on direction-stable seeds.
    flip_retry_count = 0
    flip_retry_pass = 0
    flip_retry_prov = 0
    if (
        tier_cfg["tier"] == 1
        and brain is not None
        and getattr(settings, "ENABLE_T1_SIGN_FLIP_RETRY", True)
    ):
        flip_threshold = getattr(settings, "T1_FLIP_RETRY_SHARPE", 0.5)
        flip_cap = getattr(settings, "T1_FLIP_RETRY_CAP", 5)

        flip_candidates = sorted(
            [
                a for a in updated_alphas
                if a.quality_status == "FAIL"
                and a.is_simulated and a.simulation_success
                and isinstance(a.metrics, dict)
                and a.metrics.get("sharpe") is not None
                and a.metrics["sharpe"] <= -flip_threshold
                and not (a.metadata or {}).get("flipped")
            ],
            key=lambda a: a.metrics["sharpe"],  # most-negative first
        )[:flip_cap]

        if flip_candidates:
            logger.info(
                f"[{node_name}] T1 flip-retry: {len(flip_candidates)} candidates "
                f"with sharpe ≤ -{flip_threshold}"
            )

        from backend.agents.graph.state import AlphaCandidate

        # A2: flip-retry single-alpha sim → smart settings (zero bucketing cost)
        flip_use_smart = getattr(settings, "ENABLE_SMART_SIM_SETTINGS", False)

        # V-19.3 (2026-05-06): pre-dedup flipped expressions across the WHOLE
        # alphas table. Sign-flip historically bypassed node_simulate's
        # filter_unsimulated_expressions, so BRAIN was repeatedly handed
        # already-known expressions and returned existing alpha_ids that
        # collided on uq_alpha_id at INSERT (spike → task=115 dup ZY2K0nwn /
        # GrMeLOg3 with task 81/83). Now we pre-filter to save BRAIN quota
        # AND avoid the doomed INSERT.
        flip_dedup_skipped = 0
        try:
            from backend.database import AsyncSessionLocal as _ASL
            from backend.selection_strategy import filter_unsimulated_expressions as _flt
            flipped_exprs = [f"multiply(-1, {o.expression})" for o in flip_candidates]
            async with _ASL() as _ds:
                _new_flipped, _dup_flipped = await _flt(
                    _ds, flipped_exprs, state.region, state.universe,
                )
            _new_flipped_set = set(_new_flipped)
            kept_candidates = []
            for o in flip_candidates:
                fexpr = f"multiply(-1, {o.expression})"
                if fexpr in _new_flipped_set:
                    kept_candidates.append(o)
                else:
                    flip_dedup_skipped += 1
                    logger.info(
                        f"[{node_name}] V-19.3 flip-retry skip — flipped expr already "
                        f"in DB: {fexpr[:100]!r}"
                    )
            flip_candidates = kept_candidates
            if flip_dedup_skipped:
                logger.info(
                    f"[{node_name}] V-19.3 flip-retry: {flip_dedup_skipped} candidates "
                    f"pre-deduped, {len(flip_candidates)} will simulate"
                )
        except Exception as _e:
            logger.warning(
                f"[{node_name}] V-19.3 flip-retry dedup query failed, proceeding: {_e}"
            )

        for orig in flip_candidates:
            flipped_expr = f"multiply(-1, {orig.expression})"
            try:
                if flip_use_smart:
                    from backend.sim_settings import smart_simulation_settings
                    smart = smart_simulation_settings(
                        flipped_expr,
                        tier=tier_cfg["tier"],
                        region=state.region,
                        universe=state.universe,
                    )
                    sim_result = await brain.simulate_alpha(
                        expression=flipped_expr,
                        **smart,
                    )
                else:
                    sim_result = await brain.simulate_alpha(
                        expression=flipped_expr,
                        region=state.region,
                        universe=state.universe,
                    )
            except Exception as e:
                logger.warning(f"[{node_name}] flip-retry sim failed: {e}")
                continue

            if not sim_result.get("success"):
                logger.debug(f"[{node_name}] flip sim returned failure for {flipped_expr[:80]}")
                continue

            flip_metrics = sim_result.get("metrics") or {}
            new_sharpe = flip_metrics.get("sharpe") or 0
            new_fitness = flip_metrics.get("fitness") or 0
            new_turnover = flip_metrics.get("turnover") or 0

            new_alpha = AlphaCandidate(
                expression=flipped_expr,
                hypothesis=(orig.hypothesis or "") + " (sign-flipped)",
                explanation=(
                    f"sign-flip retry — original {orig.expression[:60]} "
                    f"had sharpe={orig.metrics.get('sharpe'):.3f}"
                ),
                is_valid=True,
                is_simulated=True,
                simulation_success=True,
                alpha_id=sim_result.get("alpha_id"),
                metrics=flip_metrics,
                metadata={
                    "flipped": True,
                    "original_expression": orig.expression,
                    "original_sharpe": orig.metrics.get("sharpe"),
                    "round": getattr(orig.metadata, "get", lambda k: None)("round")
                            if not isinstance(orig.metadata, dict)
                            else (orig.metadata or {}).get("round"),
                },
            )

            # Re-eval with the same tier-aware gate. Reuse the per-tier thresholds
            # (sharpe_min / fitness_min / turnover_min/max) computed at top of
            # this function. T1 doesn't gate on concentrated / self_corr so the
            # flip-retry path mirrors that.
            sub_universe_check = next(
                (c for c in flip_metrics.get("checks", [])
                 if c.get("name") == "LOW_SUB_UNIVERSE_SHARPE"),
                None,
            )
            sub_universe_ok = (
                sub_universe_check is None
                or sub_universe_check.get("result") != "FAIL"
            )

            pass_sharpe = new_sharpe >= sharpe_min
            pass_fitness = new_fitness >= fitness_min
            pass_turnover = turnover_min <= new_turnover <= turnover_max
            # V-12 (2026-05-03 spike-discovered gap): the main hard_gate path
            # already includes is_overfit_safe, but the sign-flip retry path
            # bypassed it. Spike 2.0 alpha YP2QnnVW (multiply(-1, ts_zscore(
            # analyst_revision_rank_derivative, 5))) hit train=8.37 / test=0
            # via flip-retry and was wrongly tagged PASS. Apply the same OS-
            # consistency gate here so flip-retry can't smuggle IS-overfit
            # PASSes through.
            is_overfit_safe = _check_is_os_consistency(flip_metrics)

            if pass_sharpe and pass_fitness and pass_turnover and sub_universe_ok and is_overfit_safe:
                # V-16 (2026-05-03): apply same suspicion-mode checks on the
                # flipped expression — flip-retry inherits the same overfit
                # surface as the main hard_gate path.
                v16_flags = _run_suspicion_checks(flip_metrics, flipped_expr or "")
                hard_flags = [f for f in v16_flags if f.get("severity") == "hard"]
                if v16_flags and isinstance(new_alpha.metrics, dict):
                    new_alpha.metrics["_v16_suspicion_flags"] = v16_flags
                if hard_flags:
                    new_alpha.quality_status = "PASS_PROVISIONAL"
                    optimize_count += 1
                    flip_retry_prov += 1
                    logger.warning(
                        f"[{node_name}] V-16 downgrades flip-retry PASS → PROV | "
                        f"sharpe={new_sharpe:.2f} flags={[f['check'] for f in hard_flags]}"
                    )
                else:
                    new_alpha.quality_status = "PASS"
                    pass_count += 1
                    flip_retry_pass += 1
            elif (
                new_sharpe >= prov_sharpe_min
                and new_fitness >= prov_fitness_min
                and turnover_min <= new_turnover <= prov_turnover_max
                and sub_universe_ok
            ):
                new_alpha.quality_status = "PASS_PROVISIONAL"
                optimize_count += 1
                flip_retry_prov += 1
            else:
                new_alpha.quality_status = "FAIL"
                fail_count += 1

            updated_alphas.append(new_alpha)
            flip_retry_count += 1
            logger.info(
                f"[{node_name}] flip-retry result: orig_sharpe={orig.metrics.get('sharpe'):.2f} "
                f"→ flipped_sharpe={new_sharpe:.2f} status={new_alpha.quality_status}"
            )

    duration_ms = int((time.time() - start_time) * 1000)
    
    _debug_log("E", "nodes.py:evaluate:result", "Evaluation complete", {
        "pass": pass_count,
        "optimize": optimize_count,
        "fail": fail_count,
        "corr_checked": corr_checks_performed,
        "corr_skipped": corr_checks_skipped,
        "duration_ms": duration_ms,
        "pass_rate": round(pass_count / max(1, pass_count + optimize_count + fail_count) * 100, 1)
    })
    
    logger.info(
        f"[{node_name}] Complete | pass={pass_count} optimize={optimize_count} fail={fail_count} "
        f"corr_checked={corr_checks_performed} corr_skipped={corr_checks_skipped}"
    )
    
    # Experiment tracking
    if EXPERIMENT_TRACKING_ENABLED:
        exp = get_current_experiment()
        if exp:
            exp.metrics.increment("pass_count", pass_count)
            exp.metrics.record("iteration_duration_ms", duration_ms, tags={"node": node_name})
            
            total_evaluated = pass_count + optimize_count + fail_count
            if total_evaluated > 0:
                exp.metrics.record("pass_rate", pass_count / total_evaluated * 100, tags={"region": state.region})
            
            total_corr = corr_checks_performed + corr_checks_skipped
            if total_corr > 0:
                exp.metrics.record("corr_check_skip_rate",
                    corr_checks_skipped / total_corr * 100,
                    tags={"node": node_name}
                )
    
    # Record failure feedback with attribution-aware filtering
    if failure_feedback_queue:
        rag_service = config.get("configurable", {}).get("rag_service") if config else None
        if rag_service:
            feedback_recorded = 0
            hypothesis_failures = 0
            implementation_failures = 0

            sample_size = min(3, len(failure_feedback_queue))
            sampled_failures = random.sample(failure_feedback_queue, sample_size)

            # Plan v5+ §B8: tag every recorded pitfall with the active typed
            # Hypothesis + experiment_variant so the KB learning unit becomes
            # (alpha, hypothesis_id, dataset_pool) instead of (alpha, dataset).
            #
            # Smoke-test (2026-05-06) revealed that LangGraph scalar field
            # propagation is unreliable across nodes — state.current_hypothesis_id
            # was None at evaluation time even though the list field
            # state.current_hypothesis_ids was populated correctly. Fallback
            # to list[0] keeps the KB tagging working under that regime.
            cfg = config.get("configurable", {}) if config else {}
            current_hypothesis_id = getattr(state, "current_hypothesis_id", None)
            if current_hypothesis_id is None:
                _hids = getattr(state, "current_hypothesis_ids", None) or []
                if _hids:
                    current_hypothesis_id = _hids[0]
            experiment_variant = cfg.get("experiment_variant")

            for feedback in sampled_failures:
                attribution = feedback.get("attribution", "unknown")

                # Track attribution stats
                if attribution == "hypothesis":
                    hypothesis_failures += 1
                elif attribution == "implementation":
                    implementation_failures += 1

                try:
                    # Only record to knowledge base if attribution is confident
                    # Implementation failures shouldn't teach us about hypotheses
                    should_record = attribution != "implementation"

                    if should_record:
                        await rag_service.record_failure_pattern(
                            expression=feedback["expression"],
                            error_type=feedback["error_type"],
                            metrics=feedback["metrics"],
                            region=feedback["region"],
                            dataset_id=feedback["dataset_id"],
                            hypothesis_id=current_hypothesis_id,
                            experiment_variant=experiment_variant,
                        )
                        feedback_recorded += 1
                    else:
                        logger.debug(
                            f"[{node_name}] Skipping knowledge record for implementation failure: "
                            f"{feedback['alignment_issues'][:2] if feedback.get('alignment_issues') else 'N/A'}"
                        )
                except Exception as e:
                    logger.warning(f"[{node_name}] Failed to record feedback: {e}")
            
            logger.info(
                f"[{node_name}] Knowledge feedback | recorded={feedback_recorded}/{len(failure_feedback_queue)} "
                f"(hypothesis_fail={hypothesis_failures} impl_fail={implementation_failures})"
            )
    
    trace_update = await record_trace(
        state, trace_service, node_name,
        {
            "evaluation_mode": "two_stage_correlation",
            "thresholds": {
                "sharpe_min": sharpe_min,
                "turnover_max": turnover_max,
                "fitness_min": fitness_min,
                "score_pass": score_pass_threshold,
                "corr_check_threshold": corr_check_threshold,
            }
        },
        {
            "pass_count": pass_count,
            "optimize_count": optimize_count,
            "fail_count": fail_count,
            "corr_checks_performed": corr_checks_performed,
            "corr_checks_skipped": corr_checks_skipped,
            "flip_retry_count": flip_retry_count,
            "flip_retry_pass": flip_retry_pass,
            "flip_retry_prov": flip_retry_prov,
            "details": eval_details[:20]
        },
        duration_ms,
        "SUCCESS"
    )
    
    return {
        "pending_alphas": updated_alphas,
        **trace_update
    }
