"""
Multi-Fidelity Evaluation Module - Tiered simulation for efficiency.

P2-2: Multi-fidelity evaluation
- Fast screening with small testPeriod
- Full validation only for top candidates
- Budget-aware simulation scheduling

This significantly reduces simulation costs while maintaining quality.
"""

from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger


class FidelityLevel(Enum):
    """Simulation fidelity levels"""
    QUICK = "QUICK"      # P0Y3M - 3 months, fast screening
    MEDIUM = "MEDIUM"    # P1Y0M - 1 year, balance
    FULL = "FULL"        # P2Y0M - 2 years, final validation


@dataclass
class FidelityConfig:
    """Configuration for each fidelity level"""
    level: FidelityLevel
    test_period: str
    min_sharpe: float  # Minimum Sharpe to pass to next level
    min_fitness: float
    max_turnover: float
    
    @classmethod
    def quick(cls) -> "FidelityConfig":
        return cls(
            level=FidelityLevel.QUICK,
            test_period="P0Y3M",  # 3 months
            min_sharpe=1.0,       # Lower threshold for quick screen
            min_fitness=0.4,
            max_turnover=0.8
        )
    
    @classmethod
    def medium(cls) -> "FidelityConfig":
        return cls(
            level=FidelityLevel.MEDIUM,
            test_period="P1Y0M",  # 1 year
            min_sharpe=1.3,
            min_fitness=0.5,
            max_turnover=0.75
        )
    
    @classmethod
    def full(cls) -> "FidelityConfig":
        return cls(
            level=FidelityLevel.FULL,
            test_period="P2Y0M",  # 2 years
            min_sharpe=1.5,
            min_fitness=0.6,
            max_turnover=0.7
        )


@dataclass
class EvaluationResult:
    """Result of multi-fidelity evaluation"""
    expression: str
    passed: bool
    final_level: FidelityLevel
    
    # Metrics at each level
    quick_metrics: Optional[Dict] = None
    medium_metrics: Optional[Dict] = None
    full_metrics: Optional[Dict] = None
    
    # Timing
    quick_time_ms: int = 0
    medium_time_ms: int = 0
    full_time_ms: int = 0
    
    # Status
    alpha_id: Optional[str] = None
    error: Optional[str] = None
    
    @property
    def total_time_ms(self) -> int:
        return self.quick_time_ms + self.medium_time_ms + self.full_time_ms
    
    @property
    def best_metrics(self) -> Optional[Dict]:
        """Return metrics from highest completed level"""
        if self.full_metrics:
            return self.full_metrics
        if self.medium_metrics:
            return self.medium_metrics
        return self.quick_metrics


class MultiFidelityEvaluator:
    """
    Multi-fidelity evaluation pipeline.
    
    Strategy:
    1. Quick screen all candidates with short testPeriod
    2. Medium evaluation for promising candidates
    3. Full evaluation only for near-PASS candidates
    
    This can reduce simulation costs by 60-80% while maintaining quality.
    """
    
    def __init__(
        self,
        brain_adapter,
        quick_config: Optional[FidelityConfig] = None,
        medium_config: Optional[FidelityConfig] = None,
        full_config: Optional[FidelityConfig] = None,
        skip_medium: bool = False,  # Go directly from quick to full
        quick_pass_ratio: float = 0.3,  # Top 30% of quick pass to next level
        medium_pass_ratio: float = 0.5,  # Top 50% of medium pass to full
    ):
        self.brain = brain_adapter
        self.quick_config = quick_config or FidelityConfig.quick()
        self.medium_config = medium_config or FidelityConfig.medium()
        self.full_config = full_config or FidelityConfig.full()
        self.skip_medium = skip_medium
        self.quick_pass_ratio = quick_pass_ratio
        self.medium_pass_ratio = medium_pass_ratio
        
    async def evaluate_batch(
        self,
        expressions: List[str],
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        decay: int = 4,
        neutralization: str = "SUBINDUSTRY",
        max_full_evals: int = 10
    ) -> List[EvaluationResult]:
        """
        Evaluate batch of expressions using multi-fidelity approach.
        
        Args:
            expressions: List of alpha expressions
            max_full_evals: Maximum number of full evaluations (budget control)
            
        Returns:
            List of EvaluationResult with metrics at appropriate fidelity
        """
        import time
        
        results = []
        
        # =================================================================
        # STAGE 1: Quick screening (all candidates)
        # =================================================================
        logger.info(f"[MultiFidelity] Stage 1: Quick screening {len(expressions)} candidates")
        
        quick_results = []
        start = time.time()
        
        for expr in expressions:
            result = EvaluationResult(expression=expr, passed=False, final_level=FidelityLevel.QUICK)
            
            try:
                sim_result = await self.brain.simulate_alpha(
                    expression=expr,
                    region=region,
                    universe=universe,
                    delay=delay,
                    decay=decay,
                    neutralization=neutralization,
                    test_period=self.quick_config.test_period
                )
                
                if sim_result.get("success"):
                    result.quick_metrics = sim_result.get("metrics", {})
                    result.alpha_id = sim_result.get("alpha_id")
                    
                    # Check if passes quick thresholds
                    sharpe = result.quick_metrics.get("sharpe", 0) or 0
                    fitness = result.quick_metrics.get("fitness", 0) or 0
                    turnover = result.quick_metrics.get("turnover", 1) or 1
                    
                    if (sharpe >= self.quick_config.min_sharpe and
                        fitness >= self.quick_config.min_fitness and
                        turnover <= self.quick_config.max_turnover):
                        result.passed = True
                else:
                    result.error = sim_result.get("error", "Quick sim failed")
                    
            except Exception as e:
                result.error = str(e)
                
            result.quick_time_ms = int((time.time() - start) * 1000)
            quick_results.append(result)
            start = time.time()
            
        # Sort by quick score for next stage selection
        def quick_score(r: EvaluationResult) -> float:
            if not r.quick_metrics:
                return -999
            return (r.quick_metrics.get("sharpe", 0) or 0) * (r.quick_metrics.get("fitness", 0) or 0)
        
        quick_results.sort(key=quick_score, reverse=True)
        
        # Select top candidates for next stage
        passed_quick = [r for r in quick_results if r.passed]
        n_next = min(
            int(len(passed_quick) * self.quick_pass_ratio),
            max_full_evals * 2  # Allow 2x buffer for medium stage
        )
        candidates_for_next = passed_quick[:max(n_next, 1)] if passed_quick else []
        
        logger.info(f"[MultiFidelity] Quick stage: {len(passed_quick)}/{len(expressions)} passed, "
                   f"{len(candidates_for_next)} advancing")
        
        # =================================================================
        # STAGE 2: Medium evaluation (optional)
        # =================================================================
        if not self.skip_medium and candidates_for_next:
            logger.info(f"[MultiFidelity] Stage 2: Medium eval {len(candidates_for_next)} candidates")
            
            for result in candidates_for_next:
                start = time.time()
                
                try:
                    sim_result = await self.brain.simulate_alpha(
                        expression=result.expression,
                        region=region,
                        universe=universe,
                        delay=delay,
                        decay=decay,
                        neutralization=neutralization,
                        test_period=self.medium_config.test_period
                    )
                    
                    if sim_result.get("success"):
                        result.medium_metrics = sim_result.get("metrics", {})
                        result.final_level = FidelityLevel.MEDIUM
                        
                        sharpe = result.medium_metrics.get("sharpe", 0) or 0
                        fitness = result.medium_metrics.get("fitness", 0) or 0
                        turnover = result.medium_metrics.get("turnover", 1) or 1
                        
                        result.passed = (
                            sharpe >= self.medium_config.min_sharpe and
                            fitness >= self.medium_config.min_fitness and
                            turnover <= self.medium_config.max_turnover
                        )
                    else:
                        result.passed = False
                        result.error = sim_result.get("error")
                        
                except Exception as e:
                    result.passed = False
                    result.error = str(e)
                    
                result.medium_time_ms = int((time.time() - start) * 1000)
                
            # Select for full evaluation
            passed_medium = [r for r in candidates_for_next if r.passed]
            n_full = min(int(len(passed_medium) * self.medium_pass_ratio), max_full_evals)
            candidates_for_full = passed_medium[:max(n_full, 1)] if passed_medium else []
            
            logger.info(f"[MultiFidelity] Medium stage: {len(passed_medium)}/{len(candidates_for_next)} passed, "
                       f"{len(candidates_for_full)} advancing to full")
        else:
            candidates_for_full = candidates_for_next[:max_full_evals]
            
        # =================================================================
        # STAGE 3: Full evaluation (final candidates)
        # =================================================================
        if candidates_for_full:
            logger.info(f"[MultiFidelity] Stage 3: Full eval {len(candidates_for_full)} candidates")
            
            for result in candidates_for_full:
                start = time.time()
                
                try:
                    sim_result = await self.brain.simulate_alpha(
                        expression=result.expression,
                        region=region,
                        universe=universe,
                        delay=delay,
                        decay=decay,
                        neutralization=neutralization,
                        test_period=self.full_config.test_period
                    )
                    
                    if sim_result.get("success"):
                        result.full_metrics = sim_result.get("metrics", {})
                        result.alpha_id = sim_result.get("alpha_id")  # Use full sim's alpha_id
                        result.final_level = FidelityLevel.FULL
                        
                        sharpe = result.full_metrics.get("sharpe", 0) or 0
                        fitness = result.full_metrics.get("fitness", 0) or 0
                        turnover = result.full_metrics.get("turnover", 1) or 1
                        
                        result.passed = (
                            sharpe >= self.full_config.min_sharpe and
                            fitness >= self.full_config.min_fitness and
                            turnover <= self.full_config.max_turnover
                        )
                    else:
                        result.passed = False
                        result.error = sim_result.get("error")
                        
                except Exception as e:
                    result.passed = False
                    result.error = str(e)
                    
                result.full_time_ms = int((time.time() - start) * 1000)
                
        # Combine all results
        # Full evaluated candidates are already in quick_results, just ensure final state
        final_passed = [r for r in quick_results if r.passed and r.final_level == FidelityLevel.FULL]
        
        logger.info(f"[MultiFidelity] Complete: {len(final_passed)}/{len(expressions)} final PASS")
        
        return quick_results
    
    def estimate_savings(self, total_candidates: int, pass_rates: Tuple[float, float, float] = (0.3, 0.5, 0.8)) -> Dict:
        """
        Estimate simulation cost savings vs full evaluation.
        
        Args:
            total_candidates: Number of candidates to evaluate
            pass_rates: (quick_pass_rate, medium_pass_rate, full_pass_rate)
        """
        quick_pass, medium_pass, full_pass = pass_rates
        
        # Traditional: all candidates get full eval
        traditional_sims = total_candidates
        traditional_time_factor = 1.0  # Full testPeriod
        
        # Multi-fidelity
        quick_sims = total_candidates
        quick_time_factor = 0.15  # ~3 months vs 2 years
        
        medium_candidates = int(total_candidates * quick_pass * self.quick_pass_ratio)
        medium_sims = medium_candidates if not self.skip_medium else 0
        medium_time_factor = 0.5  # 1 year vs 2 years
        
        full_candidates = int(medium_candidates * medium_pass * self.medium_pass_ratio)
        full_sims = full_candidates
        full_time_factor = 1.0
        
        # Calculate equivalent full-sim cost
        mf_cost = (
            quick_sims * quick_time_factor +
            medium_sims * medium_time_factor +
            full_sims * full_time_factor
        )
        
        traditional_cost = traditional_sims * traditional_time_factor
        
        savings = 1 - (mf_cost / traditional_cost) if traditional_cost > 0 else 0
        
        return {
            "traditional_equivalent_sims": traditional_sims,
            "multi_fidelity_equivalent_sims": round(mf_cost, 1),
            "savings_percentage": round(savings * 100, 1),
            "breakdown": {
                "quick_sims": quick_sims,
                "medium_sims": medium_sims,
                "full_sims": full_sims,
            }
        }


# =============================================================================
# P1-D: What-if window-perturbation robustness gate.
# Source: docs/alphagbm_skills_research_2026-05-15.md skill `pnl-simulator`.
#
# 单 alpha 的 window 参数扰动鲁棒性检验 — 单元独立,由 evaluation 节点内联调用。
# 不绑定 MultiFidelityEvaluator 主流程(后者尚未集成);只复用文件作为对齐研究文档
# P1-D 落地容器的约定。
#
# - quota 守卫与 round-cap 由 evaluation.py 内联块管理(M-1/M-7/M-8)。
# - 本类只数 Redis robustness counter(M-1)且使用 return_exceptions(M-3)。
# =============================================================================


@dataclass
class RobustnessResult:
    """Outcome of a single-alpha window-perturbation robustness check.

    Fields are JSON-safe so the caller can stamp subsets into ``alpha.metrics``.
    """
    baseline_sharpe: float
    perturbation_count: int  # 成功 simulate 的变体数
    perturbation_sharpes: List[float] = field(default_factory=list)
    perturbation_can_submits: List[bool] = field(default_factory=list)  # S-7
    worst_sharpe: float = 0.0
    median_sharpe: float = 0.0
    worst_ratio: float = 0.0
    can_submit_consistency: float = 1.0  # S-7: 与 baseline can_submit 一致的比例
    passed: bool = False
    perturbations_used: List[str] = field(default_factory=list)
    perturbation_expressions: List[str] = field(default_factory=list)
    sim_failed_count: int = 0
    elapsed_ms: int = 0
    skip_reason: Optional[str] = None
    # skip_reason ∈ {None, 'no_window', 'baseline_sharpe_zero',
    #               'baseline_metrics_missing', 'all_perturbations_failed',
    #               'per_alpha_timeout', 'exception'}


class RobustnessGate:
    """Single-alpha window-perturbation robustness gate.

    Pure I/O class — quota 守卫与 round-cap 由 evaluation.py 内联块管理;
    本类只做:
      1. 枚举 ``enumerate_window_perturbations`` 变体(deterministic, M-5)。
      2. 用 baseline ``_sim_settings`` 并发 simulate 变体(``return_exceptions=True``,M-3)。
      3. 每完成一次 simulate 递增 Redis counter ``aiac:robustness_today_used``
         (TTL 86400s,M-1) — 失败静默不阻塞。
      4. 计算 worst / median / ratio / can_submit consistency 并返回。
    """

    REDIS_COUNTER_KEY = "aiac:robustness_today_used"
    REDIS_COUNTER_TTL = 86400  # seconds — auto-reset across day

    def __init__(
        self,
        brain_adapter,
        *,
        n_perturbations: int = 4,
        min_ratio: float = 0.7,
        selection_strategy: str = "first",
        redis_client=None,
    ):
        self.brain = brain_adapter
        self.n = max(1, int(n_perturbations))
        self.min_ratio = float(min_ratio)
        self.selection_strategy = selection_strategy
        self.redis = redis_client  # async redis client; None disables counter

    async def check(self, alpha) -> RobustnessResult:
        """Run robustness check for a single alpha candidate.

        Args:
            alpha: object with ``expression`` (str) and ``metrics`` (dict)
                attributes. ``metrics`` should carry ``sharpe`` (baseline) and
                ``_sim_settings`` (region/universe/delay/decay/neutralization)
                stamped by node_simulate.

        Returns:
            RobustnessResult — never raises (per-variant failures collapse
            into sim_failed_count + skip_reason).  Caller still SHOULD wrap
            this call in ``asyncio.wait_for`` to enforce a hard per-alpha
            timeout (S-5).
        """
        import time
        import asyncio
        from backend.genetic_optimizer import enumerate_window_perturbations

        t0 = time.time()
        m = alpha.metrics if isinstance(alpha.metrics, dict) else {}
        base_sharpe_raw = m.get("sharpe")
        if base_sharpe_raw is None:
            return RobustnessResult(
                baseline_sharpe=0.0,
                perturbation_count=0,
                skip_reason="baseline_metrics_missing",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        try:
            base_sharpe = float(base_sharpe_raw)
        except (TypeError, ValueError):
            return RobustnessResult(
                baseline_sharpe=0.0,
                perturbation_count=0,
                skip_reason="baseline_metrics_missing",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        if abs(base_sharpe) < 1e-9:
            return RobustnessResult(
                baseline_sharpe=base_sharpe,
                perturbation_count=0,
                skip_reason="baseline_sharpe_zero",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        base_can_submit = bool(m.get("can_submit"))

        variants = enumerate_window_perturbations(
            getattr(alpha, "expression", "") or "",
            n=self.n,
            selection_strategy=self.selection_strategy,
        )
        if not variants:
            return RobustnessResult(
                baseline_sharpe=base_sharpe,
                perturbation_count=0,
                skip_reason="no_window",
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        # Reuse baseline sim-settings so Δsharpe isolates the window change.
        sim_kwargs = dict(m.get("_sim_settings") or {})
        sim_kwargs.pop("expression", None)  # set per-variant
        # Drop keys not in BrainAdapter.simulate_alpha signature (defensive —
        # _sim_settings may carry extras like _sim_settings_reason mirror).
        _allowed_kwargs = {
            "region", "universe", "delay", "decay",
            "neutralization", "truncation", "test_period",
        }
        sim_kwargs = {k: v for k, v in sim_kwargs.items() if k in _allowed_kwargs}

        async def _one(expr: str):
            try:
                r = await self.brain.simulate_alpha(expression=expr, **sim_kwargs)
                # M-1: counter每次 simulate 都计入(无论 success / failure)— BRAIN
                # 服务端配额按 sim 调用记账,失败也算。
                if self.redis is not None:
                    try:
                        await self.redis.incr(self.REDIS_COUNTER_KEY)
                        await self.redis.expire(
                            self.REDIS_COUNTER_KEY, self.REDIS_COUNTER_TTL
                        )
                    except Exception:
                        pass  # counter 失败不阻塞主流程
                return r
            except Exception as e:
                # 不再泄露异常;返回 failure shape 让上层逐变体记账。
                return {"success": False, "error": str(e)}

        # M-3:return_exceptions=True 防 CancelledError 撕裂整 gather
        sim_results = await asyncio.gather(
            *[_one(v[0]) for v in variants],
            return_exceptions=True,
        )

        sharpes: List[float] = []
        descs: List[str] = []
        exprs: List[str] = []
        can_submits: List[bool] = []
        sim_failed = 0
        for (expr, desc), res in zip(variants, sim_results):
            if isinstance(res, BaseException):
                # CancelledError / 其他未捕获;_one 已经 except Exception,但 BaseException
                # 子类(如 CancelledError)落到 gather 的 return_exceptions 路径。
                sim_failed += 1
                continue
            if not isinstance(res, dict) or not res.get("success"):
                sim_failed += 1
                continue
            mres = res.get("metrics") or {}
            s = mres.get("sharpe")
            if s is None:
                sim_failed += 1
                continue
            try:
                s_f = float(s)
            except (TypeError, ValueError):
                sim_failed += 1
                continue
            sharpes.append(s_f)
            descs.append(desc)
            exprs.append(expr)
            # can_submit lives at top-level on real BRAIN responses, sometimes
            # also mirrored inside metrics — accept either.
            cs = res.get("can_submit")
            if cs is None:
                cs = mres.get("can_submit")
            can_submits.append(bool(cs))

        elapsed_ms = int((time.time() - t0) * 1000)
        if not sharpes:
            return RobustnessResult(
                baseline_sharpe=base_sharpe,
                perturbation_count=0,
                sim_failed_count=sim_failed,
                elapsed_ms=elapsed_ms,
                skip_reason="all_perturbations_failed",
            )

        # baseline > 0 → worst = min;baseline < 0 → worst = max (代表"最差"取决于符号)
        worst = min(sharpes) if base_sharpe > 0 else max(sharpes)
        worst_ratio = worst / abs(base_sharpe)
        sorted_sharpes = sorted(sharpes)
        median = sorted_sharpes[len(sorted_sharpes) // 2]
        can_submit_consistency = (
            sum(1 for c in can_submits if c == base_can_submit) / len(can_submits)
        )

        return RobustnessResult(
            baseline_sharpe=base_sharpe,
            perturbation_count=len(sharpes),
            perturbation_sharpes=[round(s, 4) for s in sharpes],
            perturbation_can_submits=can_submits,
            worst_sharpe=round(worst, 4),
            median_sharpe=round(median, 4),
            worst_ratio=round(worst_ratio, 4),
            can_submit_consistency=round(can_submit_consistency, 3),
            passed=worst_ratio >= self.min_ratio,
            perturbations_used=descs,
            perturbation_expressions=exprs,
            sim_failed_count=sim_failed,
            elapsed_ms=elapsed_ms,
        )
