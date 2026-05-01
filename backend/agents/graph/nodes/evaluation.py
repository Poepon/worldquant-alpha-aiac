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
    
    logger.info(f"[{node_name}] Starting batch simulation | count={len(indices_to_simulate)} region={state.region}")
    
    expressions = [state.pending_alphas[i].expression for i in indices_to_simulate]
    
    _debug_log("E", "nodes.py:simulate:expressions", "Expressions to simulate", {
        "count": len(expressions),
        "expressions": [e[:150] for e in expressions],
        "region": state.region,
        "universe": state.universe
    })
    
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
        updated.metrics = res.get("metrics", {})
        updated.simulation_error = res.get("error")
        
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

        hard_gate_pass = (
            sharpe >= sharpe_min
            and fitness >= fitness_min
            and turnover_min <= turnover <= turnover_max
            and sub_universe_ok
            and concentrated_ok
            and self_corr_ok
            and self_corr_verified
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
            error_type = "QUALITY_FAIL"
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
                            dataset_id=feedback["dataset_id"]
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
            "details": eval_details[:20]
        },
        duration_ms,
        "SUCCESS"
    )
    
    return {
        "pending_alphas": updated_alphas,
        **trace_update
    }
