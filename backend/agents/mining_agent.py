"""
Mining Agent - High-level Entry Point for Alpha Mining

This module provides:
1. Backward-compatible interface (run_mining_iteration)
2. Evolution loop with actual strategy application
3. Integration with LangGraph workflow and optimization chain

Design Principles:
1. Strategy flows through the entire pipeline (not just recorded)
2. Clear separation between orchestration and execution
3. Explicit state transitions with full traceability
4. Graceful degradation (rule-based fallback when LLM fails)
"""

from typing import List, Dict, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from loguru import logger
from datetime import datetime, timedelta
import json, time, os  # #region agent log

def _debug_log(hypo_id, location, message, data=None):
    try:
        log_path = r"e:\AIACV2_v1.2\worldquant-alpha-aiac\.cursor\debug.log"
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        entry = {"hypothesisId": hypo_id, "location": location, "message": message, "data": data or {}, "timestamp": int(time.time()*1000), "sessionId": "debug-session"}
        with open(log_path, "a", encoding="utf-8") as f: f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except: pass
# #endregion

from backend.models import MiningTask, Alpha, AlphaFailure
from backend.agents.graph import MiningWorkflow, create_mining_graph
from backend.agents.services import LLMService, get_llm_service
from backend.agents.services.trace_service import TraceService
from backend.agents.strategy_agent import StrategyAgent, create_strategy_agent
from backend.agents.evolution_strategy import (
    EvolutionStrategy, StrategyMode, RoundResult,
    RuleBasedTransition, merge_strategies,
    # Phase 1 R2/Q7 (2026-05-17)
    ContextualDirectionBandit, DirectionArm, build_context as _bandit_build_context,
    compute_arm_reward, segment_id as _bandit_segment_id,
)
from backend.config import settings as _settings  # R2/Q7 ENABLE_DIRECTION_BANDIT flag
from backend.agents.feedback_agent import FeedbackAgent
from backend.adapters.brain_adapter import BrainAdapter


class MiningAgent:
    """
    Mining Agent - Orchestrates the alpha mining process.
    
    Key Responsibilities:
    1. Manage evolution loop across multiple rounds
    2. Ensure strategy is propagated to all pipeline stages
    3. Coordinate feedback learning and knowledge accumulation
    4. Handle failures gracefully with automatic recovery
    
    Usage:
        agent = MiningAgent(db, brain)
        result = await agent.run_evolution_loop(task, dataset_id, fields, operators)
    """
    
    def __init__(
        self,
        db: AsyncSession,
        brain_adapter: BrainAdapter = None,
        llm_service: LLMService = None
    ):
        """
        Initialize MiningAgent with dependencies.
        
        Args:
            db: Async SQLAlchemy session for persistence
            brain_adapter: BRAIN platform adapter for simulation
            llm_service: LLM service for generation and analysis
        """
        self.db = db
        self.brain = brain_adapter or BrainAdapter()
        self.llm_service = llm_service or get_llm_service()
        
        # Create LangGraph workflow
        self._workflow = create_mining_graph(
            db=db,
            brain=self.brain,
            llm_service=self.llm_service
        )
        
        # Create Strategy Agent for intelligent planning
        self._strategy_agent = create_strategy_agent(llm_service=self.llm_service)
        
        # Rule-based transition for fallback
        self._rule_transition = RuleBasedTransition()
        
        # Feedback Agent for knowledge accumulation
        self._feedback_agent = FeedbackAgent(db)
        
        logger.info("[MiningAgent] Initialized with strategy-aware pipeline")
    
    async def run_mining_iteration(
        self,
        task: MiningTask,
        dataset_id: str,
        fields: List[Dict],
        operators: List[Dict],
        num_alphas: int = 3,
        iteration: int = 1,
        strategy: Optional[EvolutionStrategy] = None,
        run_id: Optional[int] = None,
        available_dataset_pool: Optional[List[str]] = None,
        hypothesis_centric_level: int = 0,
        experiment_variant: Optional[str] = None,
    ) -> List[Alpha]:
        """
        Run a single mining iteration with strategy application.
        
        Args:
            task: Mining task instance
            dataset_id: Dataset to mine
            fields: Available data fields
            operators: Available operators
            num_alphas: Target number of alphas
            iteration: Current iteration number
            strategy: Evolution strategy to apply (uses default if None)
            
        Returns:
            List of generated Alpha models (both passed and failed)
        """
        # Use default strategy if none provided
        if strategy is None:
            strategy = EvolutionStrategy.default()

        # P2-C (2026-05-16): inject the current market regime + style preset
        # into ``strategy`` so downstream nodes (node_evaluate / node_hypothesis)
        # can pick them up via config["configurable"]["strategy"]. Two effect
        # flags can drive injection:
        #   ENABLE_REGIME_AWARE_THRESHOLDS — node_evaluate scales tier_cfg
        #   ENABLE_STYLE_PRESET_GUIDANCE   — node_hypothesis renders the
        #                                     Investment Philosophy block
        # Both default OFF (S1). With both OFF the block is fully skipped so
        # strategy.regime / strategy.style_preset stay None → byte-for-byte
        # legacy (no Redis call, no log line, no observable behavioural
        # change anywhere in the workflow).
        #
        # MF2: we use ``self.db`` (the mining session) rather than spinning
        # up a fresh AsyncSessionLocal — RegimeInferenceService.get_cached_
        # regime is Redis-only so the DB session is just there to satisfy
        # BaseService's ctor contract, and reusing self.db avoids the
        # V-26.79 transactional pollution risk.
        from backend.config import settings as _p2c_settings
        if (
            getattr(_p2c_settings, "ENABLE_REGIME_AWARE_THRESHOLDS", False)
            or getattr(_p2c_settings, "ENABLE_STYLE_PRESET_GUIDANCE", False)
        ):
            try:
                # Lazy imports to avoid any circular-import surface area
                # at module load time.
                from backend.services.regime_inference_service import (
                    RegimeInferenceService,
                )
                from backend.regime_classifier import REGIME_PRESETS
                from dataclasses import replace as _dc_replace
                _svc = RegimeInferenceService(self.db)
                _cached = await _svc.get_cached_regime(
                    region=getattr(task, "region", "USA"),
                )
                if _cached and _cached in REGIME_PRESETS:
                    _preset = REGIME_PRESETS[_cached]
                    _style_dict = {
                        "regime": _preset.regime,
                        "style_label": _preset.style_label,
                        "style_philosophy": _preset.style_philosophy,
                        "pillar_bias": list(_preset.pillar_bias),
                    }
                    strategy = _dc_replace(
                        strategy,
                        regime=_cached,
                        style_preset=_style_dict,
                    )
                    logger.info(
                        f"[MiningAgent] P2-C regime injected | "
                        f"regime={_cached} task.region="
                        f"{getattr(task, 'region', 'USA')}"
                    )
            except Exception as _p2c_ex:
                logger.warning(
                    f"[MiningAgent] P2-C regime fetch failed (non-fatal): "
                    f"{_p2c_ex}"
                )

        logger.info(
            f"[MiningAgent] Starting iteration {iteration} | "
            f"mode={strategy.mode.value} temp={strategy.temperature:.2f} "
            f"explore={strategy.exploration_weight:.2f}"
        )
        
        # Initialize TraceService
        trace_service = TraceService(self.db, task.id, iteration=iteration, run_id=run_id)

        # alpha_service is injected into the workflow configurable so node
        # implementations that need to persist quality transitions (e.g.
        # apply_quality_status_change) can resolve it without re-importing.
        from backend.services.alpha_service import AlphaService
        alpha_service = AlphaService(self.db)

        try:
            # Run workflow with strategy context
            result = await self._workflow.run_with_persistence(
                task=task,
                dataset_id=dataset_id,
                fields=self._apply_field_filters(fields, strategy),
                operators=operators,
                num_alphas=num_alphas,
                config={
                    "configurable": {
                        "trace_service": trace_service,
                        "strategy": strategy.to_dict(),  # Pass strategy to all nodes
                        "run_id": run_id,
                        "db_session": self.db,
                        "brain_adapter": self.brain,
                        "alpha_service": alpha_service,
                        # Phase 1 (A2): cross-dataset hypothesis pool. Empty
                        # list = legacy single-anchor; populated = LLM may
                        # pick selected_datasets from this pool.
                        "available_dataset_pool": available_dataset_pool or [],
                        # Phase 2 (B3): typed Hypothesis persistence triggers
                        # at level >= 2. experiment_variant tags persisted rows
                        # for the F-5 variant isolation invariant.
                        "hypothesis_centric_level": int(hypothesis_centric_level or 0),
                        "experiment_variant": str(experiment_variant) if experiment_variant is not None else None,
                        # B5 v2 (2026-05-06): inject llm_service for the
                        # round-end LLM-based attribution classifier in
                        # _process_hypothesis_feedback. Workflow already has
                        # self.llm_service; we re-expose it here so node_save_results
                        # can pass it through without a partial binding.
                        "llm_service": self._workflow.llm_service if hasattr(self, "_workflow") else None,
                    }
                },
            )
            
            # Collect generated alphas from database
            generated_alphas = await self._collect_iteration_alphas(
                task.id, result.get("generated_alphas", [])
            )
            
            logger.info(
                f"[MiningAgent] Iteration {iteration} complete | "
                f"alphas={len(generated_alphas)} "
                f"failures={len(result.get('failures', []))}"
            )
            
            return generated_alphas
            
        except Exception as e:
            logger.error(f"[MiningAgent] Iteration {iteration} failed: {e}")
            raise
    
    def _apply_field_filters(
        self,
        fields: List[Dict],
        strategy: EvolutionStrategy
    ) -> List[Dict]:
        """
        Apply strategy-based field filtering.

        Prioritizes preferred fields, demotes avoided fields.

        V-22.6.6 (2026-05-13): universal PV anchors (close/cap/vwap/high/low/
        open/volume/returns/vwap/adv*/sharesout/amount) are PROTECTED from
        avoid_set + screened_set filtering. They're required by V-22.6
        composite synthesis (PE = close/eps, intraday range = (high-low)/close
        etc.) and the validator's allowed_fields list rejects composite
        candidates whose ingredients are absent. Spike on task 534 round 2
        showed strategy.avoid_fields included PV after a 0-PASS round, which
        stripped close/cap/vwap from state.fields, causing 5 V-22.6 composite
        candidates to fail VALIDATE with "Field 'close' not found in dataset".

        V-22.10 (2026-05-13): bump neutral-bucket cap 30 → 60. fundamental6
        has 886 fields; with old cap=30 LLM only saw 9 PV + 21 fund — far
        too narrow given DISTILL_CONTEXT then picks "General" category that
        further trims. Bumping to 60 lets LLM see 9 PV + 51 fund without
        ballooning prompt token cost meaningfully (~ +1.5KB).
        """
        # V-22.6.6 protected anchors — mirror _UNIVERSAL_PV_FIELDS in
        # mining_tasks.py. Kept inline rather than imported to avoid the
        # circular import (mining_agent ← mining_tasks chain).
        _PROTECTED_PV = {
            "close", "open", "high", "low", "volume", "vwap", "returns",
            "cap", "sharesout", "adv5", "adv20", "adv60", "adv120", "amount",
        }

        avoid_set = set(strategy.avoid_fields) - _PROTECTED_PV
        preferred_set = set(strategy.preferred_fields)
        screened_set = set(strategy.screened_fields) - _PROTECTED_PV

        # Split out the PV anchors first so they always survive.
        pv_anchors = [
            f for f in fields
            if (f.get("id") or f.get("name") or "") in _PROTECTED_PV
        ]
        pv_anchor_ids = {(f.get("id") or f.get("name")) for f in pv_anchors}
        other_fields = [
            f for f in fields
            if (f.get("id") or f.get("name") or "") not in pv_anchor_ids
        ]

        # If we have screened fields, prioritize them
        if screened_set:
            screened = [
                f for f in other_fields
                if (f.get("id") or f.get("name")) in screened_set
            ]
            others = [
                f for f in other_fields
                if (f.get("id") or f.get("name")) not in screened_set
                and (f.get("id") or f.get("name")) not in avoid_set
            ]
            # PV anchors always at front, then screened, then capped others.
            # V-22.10: bumped cap 20→40 for screened path
            return pv_anchors + screened + others[:40]

        # Otherwise, use preferred/avoid logic
        preferred = []
        neutral = []
        for f in other_fields:
            field_id = f.get("id") or f.get("name")
            if field_id in avoid_set:
                continue  # excluded
            if field_id in preferred_set:
                preferred.append(f)
            else:
                neutral.append(f)

        # PV anchors first (always), then preferred, then capped neutral.
        # V-22.10: bumped neutral cap 30→60
        return pv_anchors + preferred + neutral[:60]
    
    async def _collect_iteration_alphas(
        self, 
        task_id: str, 
        alpha_results: List[Any]
    ) -> List[Alpha]:
        """Collect persisted Alpha models for this iteration."""
        alphas = []
        
        for alpha_result in alpha_results:
            query = select(Alpha).where(
                Alpha.task_id == task_id,
                Alpha.expression == alpha_result.expression
            ).order_by(Alpha.id.desc()).limit(1)
            
            db_result = await self.db.execute(query)
            alpha = db_result.scalar_one_or_none()
            
            if alpha:
                alphas.append(alpha)
        
        return alphas
    
    async def run_evolution_loop(
        self,
        task: MiningTask,
        dataset_id: str,
        fields: List[Dict],
        operators: List[Dict],
        max_iterations: int = 10,
        target_alphas: int = 4,
        num_alphas_per_round: int = 4,
        initial_strategy: Optional[EvolutionStrategy] = None,
        run_id: Optional[int] = None,
        available_dataset_pool: Optional[List[str]] = None,
        hypothesis_centric_level: int = 0,
        experiment_variant: Optional[str] = None,
        iteration_offset: int = 0,
    ) -> Dict[str, Any]:
        """
        Run multi-round evolution loop for alpha mining.
        
        This is the main entry point for production mining. It:
        1. Iterates through mining rounds until goal or max iterations
        2. Applies and evolves strategy based on results
        3. Triggers optimization chain for promising weak alphas
        4. Accumulates knowledge through feedback agent
        
        Args:
            task: Mining task instance
            dataset_id: Dataset to mine
            fields: Available data fields
            operators: Available operators
            max_iterations: Maximum mining rounds
            target_alphas: Target number of successful alphas
            num_alphas_per_round: Alphas to generate per round
            initial_strategy: Optional starting strategy
            
        Returns:
            Dict with complete evolution results
        """
        logger.info(
            f"[MiningAgent] Starting Evolution Loop | "
            f"task={task.id} dataset={dataset_id} "
            f"max_iter={max_iterations} target={target_alphas}"
        )
        # #region agent log
        _debug_log("B", "mining_agent.py:run_evolution_loop:start", "Evolution loop start", {"dataset_id": dataset_id, "fields_count": len(fields), "operators_count": len(operators), "target": target_alphas})
        loop_start_time = time.time()
        # #endregion
        
        # Initialize state
        # iteration_offset (2026-05-19): for flat sessions the outer dispatcher
        # loops dataset cycles, calling this with max_iterations=1 each time.
        # Without an offset, every call would re-emit trace_steps.iteration=1,
        # causing the UI to fold all rounds into "第 1 轮". The dispatcher
        # passes the cumulative prior round count so the inner counter
        # advances monotonically across dataset cycles + pause-resume.
        iteration = iteration_offset
        total_success = 0
        all_alphas: List[Alpha] = []
        all_failures: List[Dict] = []
        strategy_history: List[EvolutionStrategy] = []
        # W1: round-level history for early-stop policy
        round_history: List[Dict] = []

        # Start with provided or default strategy
        current_strategy = initial_strategy or EvolutionStrategy.default()
        
        # Ensure Brain session is active and authenticated
        async with self.brain:
            # NB: when iteration_offset>0, terminate after max_iterations
            # ROUNDS done in THIS call (not after a fixed absolute index).
            iteration_terminal = iteration_offset + max_iterations
            while iteration < iteration_terminal:
                iteration += 1
            
                logger.info(
                    f"[MiningAgent] === Round {iteration}/{iteration_terminal} === "
                    f"Strategy: {current_strategy.action_summary}"
                )
                # #region agent log
                round_start = time.time()
                _debug_log("A", f"mining_agent.py:round_{iteration}:start", f"Round {iteration} start", {"strategy_mode": current_strategy.mode.value, "temperature": current_strategy.temperature})
                # #endregion
                
                try:
                    # Execute mining iteration with current strategy
                    alphas = await self.run_mining_iteration(
                        task=task,
                        dataset_id=dataset_id,
                        fields=fields,
                        operators=operators,
                        num_alphas=num_alphas_per_round,
                        iteration=iteration,
                        strategy=current_strategy,
                        run_id=run_id,
                        available_dataset_pool=available_dataset_pool,
                        hypothesis_centric_level=hypothesis_centric_level,
                        experiment_variant=experiment_variant,
                    )
                    
                    # Analyze round results
                    round_result = await self._analyze_round_results(
                        task_id=task.id,
                        alphas=alphas,
                        iteration=iteration
                    )
                    
                    # Update counters
                    total_success += round_result.passed_count
                    all_alphas.extend(alphas)
                    strategy_history.append(current_strategy)
                    # #region agent log
                    round_elapsed = time.time() - round_start
                    _debug_log("A", f"mining_agent.py:round_{iteration}:end", f"Round {iteration} complete", {
                        "elapsed_sec": round(round_elapsed, 2),
                        "generated": round_result.total_generated,
                        "simulated": round_result.total_simulated,
                        "passed": round_result.passed_count,
                        "failed": round_result.failed_count,
                        "syntax_errors": round_result.syntax_errors,
                        "simulation_errors": round_result.simulation_errors,
                        "quality_failures": round_result.quality_failures,
                        "best_sharpe": round_result.best_sharpe,
                        "cumulative_success": total_success
                    })
                    # #endregion
                    
                    logger.info(
                        f"[MiningAgent] Round {iteration} | "
                        f"passed={round_result.passed_count} "
                        f"total={total_success}/{target_alphas}"
                    )
                    
                    # W1: append round summary for early-stop policy
                    total_alphas_round = max(1, round_result.total_simulated or 1)
                    round_history.append({
                        "round_index": iteration,
                        "alphas_count": round_result.total_simulated,
                        "pass_count": round_result.passed_count,
                        "fail_count": round_result.failed_count,
                        "pass_rate": round_result.passed_count / total_alphas_round,
                        "best_sharpe": round_result.best_sharpe or 0.0,
                        "mean_score": 0.0,
                    })

                    # Check termination: goal reached
                    if total_success >= target_alphas:
                        logger.info(
                            f"[MiningAgent] Goal reached! "
                            f"{total_success}/{target_alphas} in {iteration} rounds"
                        )
                        break

                    # Check termination: task stopped externally
                    await self.db.refresh(task)
                    if task.status in ["STOPPED", "PAUSED"]:
                        logger.info(f"[MiningAgent] Task {task.status}, stopping")
                        break

                    # W1: round-level early-stop policy
                    from backend.agents.graph.early_stop import should_stop_early
                    early_stop, early_reason = should_stop_early(round_history, max_iterations)
                    if early_stop:
                        logger.warning(
                            f"[MiningAgent] Early stop after round {iteration}: "
                            f"{early_reason}"
                        )
                        # Mark task as EARLY_STOPPED rather than COMPLETED so the
                        # frontend can show "exploration halted, manual review"
                        try:
                            from backend.models import MiningStatus
                            task.status = MiningStatus.EARLY_STOPPED.value
                            await self.db.commit()
                        except Exception as e:
                            logger.warning(f"[MiningAgent] failed to mark task EARLY_STOPPED: {e}")
                        break
                    
                    # === STRATEGY EVOLUTION ===
                    current_strategy = await self._evolve_strategy(
                        task_id=task.id,
                        current_strategy=current_strategy,
                        round_result=round_result,
                        cumulative_success=total_success,
                        target_goal=target_alphas,
                        max_iterations=max_iterations,
                        dataset_id=dataset_id,
                        region=task.region,
                        # R2/Q7 (2026-05-17): pass full task + in-memory alphas
                        # so DirectionBandit can build context + compute reward
                        # without DB queries (R1a v1.6 lesson — most alphas don't
                        # INSERT). When ENABLE_DIRECTION_BANDIT=False these args
                        # are ignored (legacy invariant).
                        task=task,
                        round_alphas=alphas,
                    )
                    
                    # === RECORD ROUND SUMMARY ===
                    await self._record_round_summary(
                        task=task,
                        iteration=iteration,
                        round_result=round_result,
                        strategy=current_strategy,
                        cumulative_success=total_success,
                        target_alphas=target_alphas,
                        run_id=run_id,
                    )
                    
                    # === FEEDBACK LEARNING ===
                    await self._run_feedback_learning(
                        task=task,
                        alphas=alphas,
                        round_result=round_result,
                        iteration=iteration,
                        dataset_id=dataset_id,
                        cumulative_success=total_success,
                        target_alphas=target_alphas,
                        max_iterations=max_iterations,
                    )
                    
                    # === OPTIMIZATION CHAIN (if applicable) ===
                    if round_result.optimization_candidates:
                        await self._run_optimization_chain(
                            task=task,
                            candidates=round_result.optimization_candidates,
                            strategy=current_strategy,
                            iteration=iteration
                        )
                    
                except Exception as e:
                    logger.error(f"[MiningAgent] Round {iteration} error: {e}")
                    # Rollback any failed transaction
                    try:
                        await self.db.rollback()
                    except Exception:
                        pass
                    # Create rescue strategy and continue
                    current_strategy = EvolutionStrategy.rescue_mode(
                        problematic_fields=list(current_strategy.avoid_fields),
                        iteration=iteration
                    )
                    continue
        
        # Final summary
        logger.info(
            f"[MiningAgent] Evolution Complete | "
            f"iterations={iteration} success={total_success}"
        )
        
        return {
            "iterations_completed": iteration,
            "total_success": total_success,
            "target_reached": total_success >= target_alphas,
            "all_alphas": all_alphas,
            "all_failures": all_failures,
            "strategy_history": [s.to_dict() for s in strategy_history],
            "final_strategy": current_strategy.to_dict(),
        }
    
    async def _analyze_round_results(
        self,
        task_id: str,
        alphas: List[Alpha],
        iteration: int
    ) -> RoundResult:
        """
        Analyze results from a mining round to inform next strategy.
        
        Extracts metrics, identifies patterns, and flags optimization candidates.
        """
        result = RoundResult(iteration=iteration)
        result.total_generated = len(alphas)
        
        # Separate passed and failed
        passed = [a for a in alphas if getattr(a, "quality_status", None) == "PASS"]
        failed = [a for a in alphas if getattr(a, "quality_status", None) != "PASS"]
        
        result.passed_count = len(passed)
        result.failed_count = len(failed)
        
        # Count simulated (passed + quality failures)
        result.total_simulated = len(passed) + len([
            a for a in failed 
            if getattr(a, "is_simulated", False)
        ])
        
        # Extract metrics from passed alphas
        if passed:
            sharpes = []
            fitnesses = []
            turnovers = []
            
            for a in passed:
                metrics = getattr(a, "metrics", {}) or {}
                if isinstance(metrics, dict):
                    if metrics.get("sharpe") is not None:
                        sharpes.append(metrics["sharpe"])
                    if metrics.get("fitness") is not None:
                        fitnesses.append(metrics["fitness"])
                    if metrics.get("turnover") is not None:
                        turnovers.append(metrics["turnover"])
            
            if sharpes:
                result.best_sharpe = max(sharpes)
                result.avg_sharpe = sum(sharpes) / len(sharpes)
            if fitnesses:
                result.best_fitness = max(fitnesses)
                result.avg_fitness = sum(fitnesses) / len(fitnesses)
            if turnovers:
                result.avg_turnover = sum(turnovers) / len(turnovers)
        
        # Query recent failures for analysis
        failures = await self._query_recent_failures(task_id)
        
        # Analyze failure patterns
        problematic_fields = {}
        for f in failures:
            err_msg = f.get("error_message", "") or ""
            err_type = f.get("error_type", "")
            
            # Count error types
            if "syntax" in err_msg.lower() or err_type == "SYNTAX_ERROR":
                result.syntax_errors += 1
            elif "simulation" in err_msg.lower() or err_type == "SIMULATION_ERROR":
                result.simulation_errors += 1
            elif err_type == "QUALITY_CHECK_FAILED":
                result.quality_failures += 1
            
            # Extract problematic fields
            import re
            field_match = re.search(r"field[:\s]+['\"]?(\w+)['\"]?", err_msg.lower())
            if field_match:
                fname = field_match.group(1)
                problematic_fields[fname] = problematic_fields.get(fname, 0) + 1
        
        result.problematic_fields = sorted(
            problematic_fields.keys(),
            key=lambda x: problematic_fields[x],
            reverse=True
        )[:5]
        
        # Identify optimization candidates (weak but promising)
        result.optimization_candidates = await self._identify_optimization_candidates(
            alphas=failed,
            task_id=task_id
        )
        
        return result
    
    async def _query_recent_failures(self, task_id: str) -> List[Dict]:
        """Query recent failure records for analysis."""
        query = select(AlphaFailure).where(
            AlphaFailure.task_id == task_id,
            AlphaFailure.created_at >= datetime.utcnow() - timedelta(minutes=10),
            AlphaFailure.is_analyzed == False
        )
        res = await self.db.execute(query)
        failures = res.scalars().all()
        
        return [
            {
                "expression": f.expression,
                "error_message": f.error_message,
                "error_type": f.error_type
            }
            for f in failures
        ]
    
    async def _identify_optimization_candidates(
        self,
        alphas: List[Alpha],
        task_id: str
    ) -> List[Dict]:
        """
        Identify weak alphas that are worth optimizing.
        
        Criteria (from alpha_scoring.should_optimize):
        - Positive but below threshold
        - Risk-neutralized significantly better than raw
        - IS/OS gap suggests overfitting (fixable with decay/window)
        """
        from backend.alpha_scoring import should_optimize
        
        candidates = []
        
        for a in alphas:
            # Consider alphas that were optimized or simulated but failed quality
            status = getattr(a, "quality_status", None)
            is_sim = getattr(a, "is_simulated", False)

            if not is_sim:
                continue

            metrics = getattr(a, "metrics", {}) or {}

            # P1-D (M-8): alphas downgraded by the window-perturbation
            # robustness gate carry ``_skip_optimize_pool=True``. They have
            # already burned config-fragility budget upstream — re-running GA
            # on them would double-burn BRAIN quota for a candidate the gate
            # already said is not robust. Skip them here.
            if metrics.get("_skip_optimize_pool"):
                continue

            # If explicit optimize status, always include
            if status == "OPTIMIZE":
                candidates.append({
                    "expression": a.expression,
                    "hypothesis": getattr(a, "hypothesis", ""),
                    "metrics": metrics,
                    "reason": metrics.get("_optimize_reason", "Marked for optimization")
                })
                continue
            
            # Wrap metrics in structure alpha_scoring expects if needed
            sim_result = {
                "train": metrics,
                "is_stats": [metrics],
                "riskNeutralized": metrics.get("riskNeutralized", {}),
                "investabilityConstrained": metrics.get("investabilityConstrained", {})
            }
            
            should_opt, reason = should_optimize(sim_result)
            
            if should_opt:
                candidates.append({
                    "expression": a.expression,
                    "hypothesis": getattr(a, "hypothesis", ""),
                    "metrics": metrics,
                    "reason": reason
                })

        # P0 baseline screening: spend the optimization budget first on alphas
        # that genuinely beat their (hypothesis-family × dataset) cell baseline.
        # baseline_residual_sigma is a soft signal — it only reorders the top-5
        # cut here, it never gates PASS/FAIL. Missing annotation sorts as 0.0.
        candidates.sort(
            key=lambda c: (c.get("metrics") or {}).get("baseline_residual_sigma") or 0.0,
            reverse=True,
        )

        return candidates[:5]  # Limit to top 5
    
    async def _evolve_strategy(
        self,
        task_id: str,
        current_strategy: EvolutionStrategy,
        round_result: RoundResult,
        cumulative_success: int,
        target_goal: int,
        max_iterations: int,
        dataset_id: str,
        region: str,
        *,
        task: Optional[MiningTask] = None,
        round_alphas: Optional[List[Any]] = None,
    ) -> EvolutionStrategy:
        """
        Evolve strategy based on round results.

        Uses LLM analysis when available, falls back to rules.

        R2/Q7 (2026-05-17): when ``settings.ENABLE_DIRECTION_BANDIT=True`` AND
        ``task`` provided, runs the ContextualDirectionBandit's
        update-then-select cycle before consulting the LLM. The selected arm
        is passed as ``bandit_arm`` hint to ``strategy_agent.generate_strategy``.
        Flag-off path is byte-for-byte legacy (no DB writes, no bandit state
        touched).
        """
        # Compute rule-based strategy (always available)
        rule_strategy = self._rule_transition.compute_next_strategy(
            current_strategy=current_strategy,
            round_result=round_result,
            cumulative_success=cumulative_success,
            target_goal=target_goal,
            max_iterations=max_iterations
        )

        # R2/Q7 bandit decision — happens BEFORE optimization short-circuit so
        # the previous round's arm gets its reward recorded even if we're
        # about to forcibly switch into EXPLOIT this round (bandit signal must
        # not lose data just because the local rule overrides).
        bandit_arm: Optional[str] = None
        if getattr(_settings, "ENABLE_DIRECTION_BANDIT", False) and task is not None:
            try:
                bandit_arm = await self._bandit_update_and_select(
                    task=task,
                    round_result=round_result,
                    round_alphas=round_alphas,
                )
            except Exception as e:
                # Bandit failure must never block strategy evolution
                logger.warning(f"[Bandit] non-fatal bandit decision failure: {e}")
                # M12 (2026-05-18): clear persisted last_select so next round
                # does NOT attribute its reward to a stale arm. The local
                # ``bandit`` object inside _bandit_update_and_select may have
                # already advanced (or partially advanced) past update_last_round
                # before raising — in either case the safe sentinel is None
                # (update_last_round treats None as "skip update + warn").
                await self._clear_bandit_last_select(task)

        # CRITICAL FIX: If we have optimization candidates, FORCE exploit/optimize mode
        # to ensure we don't skip the opportunity to refine them.
        if round_result.optimization_candidates:
            logger.info(f"[Strategy] Found {len(round_result.optimization_candidates)} optimization candidates. Forcing EXPLOIT mode.")
            rule_strategy.mode = StrategyMode.EXPLOIT
            rule_strategy.focus_hypotheses = [
                f"Optimize: {c['reason']}" for c in round_result.optimization_candidates
            ]
            rule_strategy.reasoning = "Focusing on optimizing identified promising alphas."
            return rule_strategy


        # Try LLM-based strategy enhancement
        try:
            # Get recent alphas for this task (for LLM analysis)
            query = select(Alpha).where(
                Alpha.task_id == task_id
            ).order_by(Alpha.created_at.desc()).limit(10)

            res = await self.db.execute(query)
            recent_alphas = res.scalars().all()

            llm_response = await self._strategy_agent.generate_strategy(
                iteration=round_result.iteration,
                max_iterations=max_iterations,
                alphas=recent_alphas,
                failures=await self._query_recent_failures(task_id),
                dataset_id=dataset_id,
                region=region,
                cumulative_success=cumulative_success,
                target_goal=target_goal,
                previous_strategy=current_strategy,
                bandit_arm=bandit_arm,  # R2/Q7: None when flag OFF (legacy)
            )
            
            # Convert to dict for merging
            llm_dict = {
                "strategy": {
                    "temperature": llm_response.temperature,
                    "exploration_weight": llm_response.exploration_weight,
                    "focus_hypotheses": llm_response.focus_hypotheses,
                    "avoid_patterns": llm_response.avoid_patterns,
                    "preferred_fields": llm_response.preferred_fields,
                    "avoid_fields": llm_response.avoid_fields,
                    "action_summary": llm_response.action_summary,
                    "reasoning": llm_response.reasoning,
                },
                "optimization_targets": llm_response.optimization_suggestions
            }
            
            # Merge LLM suggestions with rule guardrails
            return merge_strategies(current_strategy, llm_dict, rule_strategy)
            
        except Exception as e:
            logger.warning(f"[MiningAgent] LLM strategy failed, using rules: {e}")
            return rule_strategy

    # ------------------------------------------------------------------
    # Phase 1 R2/Q7 (2026-05-17) ContextualDirectionBandit integration
    # ------------------------------------------------------------------

    _BANDIT_CONFIG_KEY = "contextual_bandit_v1"

    async def _bandit_update_and_select(
        self,
        task: MiningTask,
        round_result: RoundResult,
        round_alphas: Optional[List[Any]],
    ) -> Optional[str]:
        """One bandit cycle: deserialize → apply reward for previous round →
        compute new context → select arm → persist state → log to
        direction_bandit_log. Returns the newly-selected arm name, or None
        on any soft failure (caller catches the broader except).
        """
        # 1. Load bandit state from task.config (initialize fresh on first round)
        config = task.config if isinstance(task.config, dict) else {}
        state = config.get(self._BANDIT_CONFIG_KEY)
        if state:
            bandit = ContextualDirectionBandit.from_dict(state)
        else:
            arm_names = list(
                getattr(_settings, "DIRECTION_BANDIT_ARMS", None)
                or ("rag_template", "knowledge_pattern",
                    "llm_generation", "genetic_mutation")
            )
            cold = int(getattr(_settings, "DIRECTION_BANDIT_COLD_THRESHOLD", 5))
            bandit = ContextualDirectionBandit(
                arm_names=arm_names, cold_threshold=cold
            )

        # 2. Apply previous round's reward to last_select (no-op if first round)
        reward = compute_arm_reward(round_alphas or [])
        prior_apply = bandit.update_last_round(reward)

        # 3. Build new context for this round's select
        ctx = await _bandit_build_context(task)
        cold_at_ctx = bandit.is_cold_at(ctx)

        # 4. Select new arm (caches to bandit.last_select for next round)
        arm = bandit.select_arm(ctx)

        # 5. Persist bandit state back into task.config (mark JSONB dirty)
        await self._persist_bandit_state(task, bandit)

        # 6. INSERT a direction_bandit_log row (independent session, R1a v1.6
        #    pattern). Soft-fail: log failure doesn't break strategy evolution.
        await self._write_bandit_log_row(
            task_id=task.id,
            round_idx=getattr(round_result, "iteration", None),
            ctx=ctx,
            selected_arm=arm,
            observed_reward=reward if prior_apply is not None else None,
            cold_start=cold_at_ctx,
        )

        logger.info(
            f"[Bandit] task={task.id} round={round_result.iteration} "
            f"ctx={_bandit_segment_id(ctx)} arm={arm} "
            f"reward_for_prev={reward:.3f} cold={cold_at_ctx}"
        )
        return arm

    async def _clear_bandit_last_select(self, task: MiningTask) -> None:
        """M12 swallow-path helper: null out ``last_select`` inside the
        persisted bandit blob without touching arm posteriors. Safe no-op
        when the bandit was never initialized.
        """
        from sqlalchemy.orm.attributes import flag_modified
        try:
            if not isinstance(task.config, dict):
                return
            state = task.config.get(self._BANDIT_CONFIG_KEY)
            if not isinstance(state, dict):
                return
            if state.get("last_select") is None:
                return  # already clean — nothing to flush
            state["last_select"] = None
            task.config[self._BANDIT_CONFIG_KEY] = state
            flag_modified(task, "config")
            await self.db.flush()
        except Exception as e:
            logger.warning(
                f"[Bandit] clear last_select on exception path failed (non-fatal): {e}"
            )

    async def _persist_bandit_state(
        self,
        task: MiningTask,
        bandit: "ContextualDirectionBandit",
    ) -> None:
        """Write bandit.to_dict() into task.config and flag JSONB dirty."""
        from sqlalchemy.orm.attributes import flag_modified
        if not isinstance(task.config, dict):
            task.config = {}
        task.config[self._BANDIT_CONFIG_KEY] = bandit.to_dict()
        # SQLAlchemy doesn't auto-detect mutation inside JSONB; mark dirty so
        # the next flush picks it up. (task is a SQLAlchemy mapped instance —
        # this is NOT the AlphaCandidate Pydantic trap from R1a v1.2.)
        flag_modified(task, "config")
        try:
            await self.db.flush()
        except Exception as e:
            logger.warning(f"[Bandit] persist flush failed (non-fatal): {e}")

    async def _write_bandit_log_row(
        self,
        task_id: int,
        round_idx: Optional[int],
        ctx: tuple,
        selected_arm: str,
        observed_reward: Optional[float],
        cold_start: bool,
    ) -> None:
        """INSERT one direction_bandit_log row via fresh AsyncSessionLocal."""
        # Lazy import to avoid module-load cycle
        from backend.database import AsyncSessionLocal
        from backend.models.direction_bandit_log import DirectionBanditLog
        try:
            async with AsyncSessionLocal() as s:
                row = DirectionBanditLog(
                    task_id=task_id,
                    round_idx=round_idx,
                    segment_id=_bandit_segment_id(ctx),
                    region=ctx[0],
                    dataset_category=ctx[1],
                    failure_pattern=ctx[2],
                    selected_arm=selected_arm,
                    observed_reward=observed_reward,
                    cold_start="true" if cold_start else "false",
                    bandit_version="v1",
                )
                s.add(row)
                await s.commit()
        except Exception as e:
            logger.warning(f"[Bandit] log INSERT failed (non-fatal): {e}")

    async def _record_round_summary(
        self,
        task: MiningTask,
        iteration: int,
        round_result: RoundResult,
        strategy: EvolutionStrategy,
        cumulative_success: int,
        target_alphas: int,
        run_id: Optional[int] = None,
    ):
        """Record comprehensive round summary for tracing."""
        try:
            trace_service = TraceService(
                self.db, task.id, 
                initial_step_order=99, 
                iteration=iteration,
                run_id=run_id,
            )
            
            # 2026-05-19: rewrite output_data to a FLAT schema the frontend
            # ROUND_SUMMARY card actually reads from (the old shape nested
            # everything under `round_metrics` + omitted next_strategy
            # fields entirely → the card displayed mostly N/A). Backwards
            # compat: keep `round_metrics` and `next_action` / `next_reasoning`
            # alongside the new top-level keys so any older reader survives.
            pass_count = round_result.passed_count
            alpha_count = round_result.total_generated or round_result.total_simulated
            avg_turnover = getattr(round_result, "avg_turnover", None)
            avg_fitness = getattr(round_result, "avg_fitness", None)
            problematic_fields = list(getattr(round_result, "problematic_fields", []) or [])
            record = trace_service.create_record(
                step_type="ROUND_SUMMARY",
                status="SUCCESS",
                input_data={
                    "round": iteration,
                    "target_alphas": target_alphas,
                    "strategy_mode": strategy.mode.value,
                    "strategy_params": {
                        "temperature": strategy.temperature,
                        "exploration": strategy.exploration_weight,
                        "focus_hypos": len(strategy.focus_hypotheses),
                        "avoid_patterns": len(strategy.avoid_patterns),
                    },
                },
                output_data={
                    # Top-level performance keys (flat, as frontend expects)
                    "mining_success": pass_count > 0,
                    "success_rate": round_result.success_rate,
                    "simulated_alphas": round_result.total_simulated,
                    "succeeded_alphas": pass_count,
                    "alphas_count": alpha_count,
                    "best_sharpe": round_result.best_sharpe,
                    "avg_sharpe": round_result.avg_sharpe,
                    "best_fitness": round_result.best_fitness,
                    "avg_fitness": avg_fitness,
                    "avg_turnover": avg_turnover,
                    # No avg_returns source — leave omitted (frontend → N/A).
                    "error_breakdown": {
                        "syntax_errors": round_result.syntax_errors,
                        "simulation_errors": round_result.simulation_errors,
                        "quality_failures": round_result.quality_failures,
                    },
                    "problematic_fields": problematic_fields[:5],
                    "cumulative_success": cumulative_success,
                    # Next-round strategy (computed from this round via
                    # _evolve_strategy before this writer is called) —
                    # frontend reads `next_strategy.<field>`.
                    "next_strategy": {
                        "mode": strategy.mode.value,
                        "temperature": strategy.temperature,
                        "exploration_weight": strategy.exploration_weight,
                        "action": strategy.action_summary,
                        "reasoning": strategy.reasoning,
                        "focus_hypotheses": list(strategy.focus_hypotheses),
                        "amplify_patterns": list(strategy.amplify_patterns),
                        "avoid_patterns": list(strategy.avoid_patterns),
                        "avoid_fields": list(strategy.avoid_fields),
                        # `optimization_suggestions` not produced by current
                        # EvolutionStrategy — omitted (frontend tolerates).
                    },
                    # Back-compat for any code path still reading the old
                    # nested shape:
                    "round_metrics": round_result.to_dict(),
                    "next_action": strategy.action_summary,
                    "next_reasoning": strategy.reasoning,
                    "optimization_candidates": len(round_result.optimization_candidates),
                },
            )
            
            await trace_service.persist_record(record)
            
        except Exception as e:
            logger.error(f"Failed to record round summary: {e}")
    
    async def _run_feedback_learning(
        self,
        task: MiningTask,
        alphas: List[Alpha],
        round_result: RoundResult,
        iteration: int,
        dataset_id: str,
        cumulative_success: int = 0,
        target_alphas: int = 4,
        max_iterations: int = 10,
    ):
        """Run feedback learning to accumulate knowledge."""
        try:
            failures = await self._query_recent_failures(task.id)

            # Plan v5+ §B8: thread typed Hypothesis lineage + variant tag
            # into KB writes. hypothesis_ids come from the alphas themselves
            # (B4 populates Alpha.hypothesis_id when level>=2). variant comes
            # from task config (set at task launch in mining_tasks.py).
            hypothesis_ids = sorted({
                a.hypothesis_id for a in alphas
                if getattr(a, "hypothesis_id", None) is not None
            })
            experiment_variant = (task.config or {}).get(
                "hypothesis_centric_variant"
            )
            if experiment_variant is not None:
                experiment_variant = str(experiment_variant)

            await self._feedback_agent.learn_from_round(
                successes=alphas,
                failures=failures,
                iteration=iteration,
                dataset_id=dataset_id,
                region=task.region,
                cumulative_success=cumulative_success,
                target_goal=target_alphas,
                max_iterations=max_iterations,
                hypothesis_ids=hypothesis_ids,
                experiment_variant=experiment_variant,
            )
            
            # Mark failures as analyzed
            query = select(AlphaFailure).where(
                AlphaFailure.task_id == task.id,
                AlphaFailure.is_analyzed == False
            )
            res = await self.db.execute(query)
            for f in res.scalars().all():
                f.is_analyzed = True
            
            await self.db.commit()
            
        except Exception as e:
            logger.warning(f"[MiningAgent] Feedback learning failed: {e}")
            try:
                await self.db.rollback()
            except Exception:
                pass
    
    async def _run_optimization_chain(
        self,
        task: MiningTask,
        candidates: List[Dict],
        strategy: EvolutionStrategy,
        iteration: int
    ):
        """
        Run optimization chain on promising weak alphas.
        
        This is the Chain-of-Alpha style optimization loop.
        """
        from backend.optimization_chain import generate_local_rewrites, generate_settings_variants
        
        logger.info(f"[MiningAgent] Running optimization chain on {len(candidates)} candidates")
        
        for candidate in candidates[:3]:  # Limit to top 3
            expression = candidate.get("expression", "")
            metrics = candidate.get("metrics", {})
            reason = candidate.get("reason", "")
            
            if not expression:
                continue
            
            try:
                # Generate expression variants
                expr_variants = generate_local_rewrites(
                    expression=expression,
                    sim_result=metrics,
                    feedback=reason,
                    max_variants=10
                )
                
                # Generate settings variants
                settings_variants = generate_settings_variants({
                    "neutralization": "INDUSTRY",
                    "decay": 4,
                    "truncation": 0.02
                })
                
                # Simulate top variants (budget-limited)
                await self._simulate_optimization_variants(
                    task=task,
                    original_expression=expression,
                    expr_variants=expr_variants[:5],
                    settings_variants=settings_variants[:3],
                    iteration=iteration
                )
                
            except Exception as e:
                logger.warning(f"Optimization failed for {expression[:50]}: {e}")
    
    async def _simulate_optimization_variants(
        self,
        task: MiningTask,
        original_expression: str,
        expr_variants: List[Dict],
        settings_variants: List[Dict],
        iteration: int
    ):
        """Simulate optimization variants and save improvements."""
        logger.info(
            f"[MiningAgent] Simulating {len(expr_variants)} variants for optimization"
        )
        
        # Process Expression Variants
        for variant in expr_variants:
            try:
                expression = variant.get("expression")
                if not expression:
                    continue
                    
                # Simulate
                result = await self.brain.simulate_alpha(
                    expression=expression,
                    region=task.region,
                    universe=task.universe,
                    delay=1,
                    decay=4,
                    neutralization="INDUSTRY" 
                )
                
                if result.get("success"):
                    # Check if improved (using simplified check here, fuller one in chain)
                    metrics = result.get("metrics", {})
                    sharpe = metrics.get("sharpe", 0)
                    
                    if sharpe > 1.2: # Simple threshold for now
                        # PASS only when all three BRAIN red-lines clear; otherwise OPTIMIZE.
                        fitness = metrics.get("fitness", 0) or 0
                        turnover = metrics.get("turnover", 0) or 0
                        hard_gate_pass = (
                            sharpe >= 1.5
                            and fitness >= 1.0
                            and 0.01 <= turnover <= 0.7
                        )
                        # Save successful optimization
                        alpha = Alpha(
                            task_id=task.id,
                            alpha_id=result.get("alpha_id"),
                            expression=expression,
                            hypothesis=f"Optimization of {original_expression[:20]}...",
                            logic_explanation=f"Variant: {variant.get('description')}",
                            region=task.region,
                            universe=task.universe,
                            dataset_id=task.dataset_id if hasattr(task, 'dataset_id') else "unknown",
                            simulation_status="SUCCESS",
                            quality_status="PASS" if hard_gate_pass else "OPTIMIZE",
                            metrics=metrics
                        )
                        self.db.add(alpha)
                        logger.info(f"[MiningAgent] Optimization success: {expression[:30]} (Sharpe: {sharpe})")
                        
            except Exception as e:
                logger.warning(f"Optimization simulation failed: {e}")
                
        await self.db.commit()
    
    @property
    def workflow(self) -> MiningWorkflow:
        """Access the underlying LangGraph workflow."""
        return self._workflow


# =============================================================================
# Factory Function
# =============================================================================

def create_mining_agent(
    db: AsyncSession,
    brain: BrainAdapter = None
) -> MiningAgent:
    """
    Factory function to create MiningAgent.
    
    Usage:
        agent = create_mining_agent(db)
        result = await agent.run_evolution_loop(task, ...)
    """
    return MiningAgent(db=db, brain_adapter=brain)
