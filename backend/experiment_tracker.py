"""
Experiment Tracker - Observability, Reproducibility, and A/B Testing Framework.

This module provides:
1. Structured metrics collection for all pipeline stages
2. Experiment versioning and reproducibility controls
3. A/B testing framework for optimization comparison
4. Baseline recording and statistical significance testing

Design Principles:
- Every optimization should be measurable
- Every experiment should be reproducible
- Every change should be comparable to baseline
"""

import json
import hashlib
import random
import numpy as np
from typing import Dict, List, Any, Optional, Tuple, Callable
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from loguru import logger
import asyncio


# =============================================================================
# Metric Types and Definitions
# =============================================================================

class MetricType(Enum):
    """Types of metrics tracked"""
    COUNTER = "counter"      # Cumulative count (e.g., total simulations)
    GAUGE = "gauge"          # Point-in-time value (e.g., current queue size)
    HISTOGRAM = "histogram"  # Distribution (e.g., response times)
    RATE = "rate"            # Rate over time (e.g., PASS rate)


@dataclass
class MetricDefinition:
    """Definition of a trackable metric"""
    name: str
    metric_type: MetricType
    description: str
    unit: str = ""
    tags: List[str] = field(default_factory=list)


# Core KPIs from optimization plan
METRIC_DEFINITIONS = {
    # KPI A: PASS count per simulation
    "pass_per_sim": MetricDefinition(
        name="pass_per_sim",
        metric_type=MetricType.RATE,
        description="PASS alphas per simulation (efficiency)",
        unit="ratio",
        tags=["kpi", "efficiency"]
    ),
    
    # KPI C: Diversity of PASSes
    "diversity_score": MetricDefinition(
        name="diversity_score",
        metric_type=MetricType.GAUGE,
        description="Diversity of generated alphas (1 - avg similarity)",
        unit="ratio",
        tags=["kpi", "diversity"]
    ),
    
    # P0-2: Deduplication effectiveness
    "dedup_skip_rate": MetricDefinition(
        name="dedup_skip_rate",
        metric_type=MetricType.RATE,
        description="Percentage of expressions skipped by deduplication",
        unit="percent",
        tags=["p0", "dedup"]
    ),
    
    # P0-3: Correlation check savings
    "corr_check_skip_rate": MetricDefinition(
        name="corr_check_skip_rate",
        metric_type=MetricType.RATE,
        description="Percentage of correlation checks skipped (two-stage)",
        unit="percent",
        tags=["p0", "correlation"]
    ),
    
    # P1-1: Bandit selection effectiveness
    "bandit_reward_mean": MetricDefinition(
        name="bandit_reward_mean",
        metric_type=MetricType.GAUGE,
        description="Mean reward from bandit-selected datasets",
        unit="score",
        tags=["p1", "bandit"]
    ),
    
    # P1-2: Field selection quality
    "field_selection_hit_rate": MetricDefinition(
        name="field_selection_hit_rate",
        metric_type=MetricType.RATE,
        description="Percentage of selected fields appearing in PASS alphas",
        unit="percent",
        tags=["p1", "field_selection"]
    ),
    
    # P2-2: Multi-fidelity savings
    "mf_simulation_savings": MetricDefinition(
        name="mf_simulation_savings",
        metric_type=MetricType.RATE,
        description="Simulation cost savings from multi-fidelity evaluation",
        unit="percent",
        tags=["p2", "multi_fidelity"]
    ),
    
    # General performance
    "simulation_count": MetricDefinition(
        name="simulation_count",
        metric_type=MetricType.COUNTER,
        description="Total simulations performed",
        unit="count",
        tags=["performance"]
    ),
    "pass_count": MetricDefinition(
        name="pass_count",
        metric_type=MetricType.COUNTER,
        description="Total PASS alphas",
        unit="count",
        tags=["performance"]
    ),
    "api_call_count": MetricDefinition(
        name="api_call_count",
        metric_type=MetricType.COUNTER,
        description="Total BRAIN API calls",
        unit="count",
        tags=["performance", "cost"]
    ),
    "llm_token_count": MetricDefinition(
        name="llm_token_count",
        metric_type=MetricType.COUNTER,
        description="Total LLM tokens used",
        unit="tokens",
        tags=["performance", "cost"]
    ),
    "iteration_duration_ms": MetricDefinition(
        name="iteration_duration_ms",
        metric_type=MetricType.HISTOGRAM,
        description="Duration of mining iterations",
        unit="milliseconds",
        tags=["performance", "latency"]
    ),
}


# =============================================================================
# Metrics Collector
# =============================================================================

@dataclass
class MetricSample:
    """Single metric sample with timestamp and context"""
    metric_name: str
    value: float
    timestamp: datetime
    tags: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "metric": self.metric_name,
            "value": self.value,
            "timestamp": self.timestamp.isoformat(),
            "tags": self.tags
        }


class MetricsCollector:
    """
    Centralized metrics collection for observability.
    
    Usage:
        collector = MetricsCollector(experiment_id="exp_001")
        collector.record("pass_count", 5, tags={"region": "USA"})
        collector.increment("simulation_count", 10)
        summary = collector.get_summary()
    """
    
    def __init__(self, experiment_id: str, persist_path: Optional[Path] = None):
        self.experiment_id = experiment_id
        self.persist_path = persist_path
        self.samples: List[MetricSample] = []
        self.counters: Dict[str, float] = {}
        self.histograms: Dict[str, List[float]] = {}
        self.start_time = datetime.now()
        
    def record(self, metric_name: str, value: float, tags: Optional[Dict[str, str]] = None):
        """Record a metric value"""
        sample = MetricSample(
            metric_name=metric_name,
            value=value,
            timestamp=datetime.now(),
            tags=tags or {}
        )
        self.samples.append(sample)
        
        # Update aggregates
        defn = METRIC_DEFINITIONS.get(metric_name)
        if defn:
            if defn.metric_type == MetricType.HISTOGRAM:
                if metric_name not in self.histograms:
                    self.histograms[metric_name] = []
                self.histograms[metric_name].append(value)
                
        logger.debug(f"[Metrics] {metric_name}={value} tags={tags}")
        
    def increment(self, metric_name: str, delta: float = 1, tags: Optional[Dict[str, str]] = None):
        """Increment a counter metric"""
        key = f"{metric_name}:{json.dumps(tags or {}, sort_keys=True)}"
        self.counters[key] = self.counters.get(key, 0) + delta
        self.record(metric_name, self.counters[key], tags)
        
    def get_counter(self, metric_name: str, tags: Optional[Dict[str, str]] = None) -> float:
        """Get current counter value"""
        key = f"{metric_name}:{json.dumps(tags or {}, sort_keys=True)}"
        return self.counters.get(key, 0)
        
    def get_histogram_stats(self, metric_name: str) -> Dict[str, float]:
        """Get histogram statistics"""
        values = self.histograms.get(metric_name, [])
        if not values:
            return {"count": 0, "mean": 0, "std": 0, "min": 0, "max": 0, "p50": 0, "p95": 0}
            
        arr = np.array(values)
        return {
            "count": len(values),
            "mean": float(np.mean(arr)),
            "std": float(np.std(arr)),
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
            "p50": float(np.percentile(arr, 50)),
            "p95": float(np.percentile(arr, 95)),
        }
        
    def get_summary(self) -> Dict[str, Any]:
        """Get comprehensive metrics summary"""
        duration = (datetime.now() - self.start_time).total_seconds()
        
        # Calculate KPIs
        sim_count = self.get_counter("simulation_count")
        pass_count = self.get_counter("pass_count")
        pass_per_sim = pass_count / sim_count if sim_count > 0 else 0
        
        return {
            "experiment_id": self.experiment_id,
            "duration_seconds": duration,
            "start_time": self.start_time.isoformat(),
            "end_time": datetime.now().isoformat(),
            "kpis": {
                "pass_per_sim": round(pass_per_sim, 4),
                "total_simulations": sim_count,
                "total_passes": pass_count,
            },
            "counters": dict(self.counters),
            "histograms": {k: self.get_histogram_stats(k) for k in self.histograms},
            "sample_count": len(self.samples),
        }
        
    def export_samples(self) -> List[Dict]:
        """Export all samples for analysis"""
        return [s.to_dict() for s in self.samples]
        
    async def persist(self):
        """Persist metrics to file"""
        if not self.persist_path:
            return
            
        self.persist_path.mkdir(parents=True, exist_ok=True)
        
        summary_file = self.persist_path / f"{self.experiment_id}_summary.json"
        samples_file = self.persist_path / f"{self.experiment_id}_samples.jsonl"
        
        # Write summary
        with open(summary_file, 'w') as f:
            json.dump(self.get_summary(), f, indent=2)
            
        # Write samples (append mode for streaming)
        with open(samples_file, 'a') as f:
            for sample in self.samples[-100:]:  # Last 100 samples
                f.write(json.dumps(sample.to_dict()) + "\n")
                
        logger.info(f"[Metrics] Persisted to {self.persist_path}")


# =============================================================================
# Reproducibility Controls
# =============================================================================

@dataclass
class ExperimentConfig:
    """
    Experiment configuration snapshot for reproducibility.
    
    Captures all settings that affect experiment outcome.
    """
    experiment_id: str
    created_at: datetime
    
    # Random seeds
    random_seed: int
    numpy_seed: int
    
    # Mining settings
    region: str
    universe: str
    dataset_ids: List[str]
    
    # Model settings
    llm_model: str
    temperature: float
    
    # Optimization flags (P0-P2)
    semantic_validation_enabled: bool = True
    batch_dedup_enabled: bool = True
    db_dedup_enabled: bool = True
    two_stage_corr_enabled: bool = True
    bandit_selection_enabled: bool = False
    field_scoring_enabled: bool = False
    diversity_filter_enabled: bool = False
    multi_fidelity_enabled: bool = False
    
    # Thresholds
    sharpe_min: float = 1.5
    fitness_min: float = 1.0
    turnover_max: float = 0.7
    corr_check_threshold: float = 0.5
    dedup_similarity_threshold: float = 0.9
    
    # Version info
    code_version: str = ""
    config_hash: str = ""
    
    def __post_init__(self):
        # Compute config hash for quick comparison
        config_str = json.dumps(asdict(self), sort_keys=True, default=str)
        self.config_hash = hashlib.md5(config_str.encode()).hexdigest()[:12]
        
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_settings(cls, experiment_id: str, settings, **overrides) -> "ExperimentConfig":
        """Create config from settings object"""
        return cls(
            experiment_id=experiment_id,
            created_at=datetime.now(),
            random_seed=overrides.get("random_seed", 42),
            numpy_seed=overrides.get("numpy_seed", 42),
            region=overrides.get("region", settings.DEFAULT_REGION),
            universe=overrides.get("universe", settings.DEFAULT_UNIVERSE),
            dataset_ids=overrides.get("dataset_ids", []),
            llm_model=settings.OPENAI_MODEL,
            temperature=overrides.get("temperature", settings.DEFAULT_TEMPERATURE),
            semantic_validation_enabled=overrides.get("semantic_validation_enabled", True),
            batch_dedup_enabled=overrides.get("batch_dedup_enabled", True),
            db_dedup_enabled=overrides.get("db_dedup_enabled", True),
            two_stage_corr_enabled=overrides.get("two_stage_corr_enabled", True),
            bandit_selection_enabled=overrides.get("bandit_selection_enabled", False),
            field_scoring_enabled=overrides.get("field_scoring_enabled", False),
            diversity_filter_enabled=overrides.get("diversity_filter_enabled", False),
            multi_fidelity_enabled=settings.MULTI_FIDELITY_ENABLED,
            sharpe_min=settings.SHARPE_MIN,
            fitness_min=settings.FITNESS_MIN,
            turnover_max=settings.TURNOVER_MAX,
            corr_check_threshold=settings.CORR_CHECK_THRESHOLD,
            dedup_similarity_threshold=settings.BATCH_DEDUP_THRESHOLD,
        )


def set_reproducibility_seeds(seed: int):
    """Set all random seeds for reproducibility"""
    random.seed(seed)
    np.random.seed(seed)
    # Note: For torch, add: torch.manual_seed(seed)
    logger.info(f"[Reproducibility] Seeds set to {seed}")


# =============================================================================
# A/B Testing Framework
# =============================================================================

class ExperimentVariant(Enum):
    """Experiment variants for A/B testing"""
    BASELINE = "baseline"      # No optimizations
    TREATMENT_A = "treatment_a"  # P0 only
    TREATMENT_B = "treatment_b"  # P0 + P1
    TREATMENT_C = "treatment_c"  # P0 + P1 + P2


@dataclass
class ABTestResult:
    """Result of an A/B test comparison"""
    baseline_metrics: Dict[str, float]
    treatment_metrics: Dict[str, float]
    metric_name: str
    
    # Statistical analysis
    absolute_diff: float = 0.0
    relative_diff_pct: float = 0.0
    is_significant: bool = False
    p_value: float = 1.0
    confidence_interval: Tuple[float, float] = (0.0, 0.0)
    
    def __post_init__(self):
        baseline_val = self.baseline_metrics.get(self.metric_name, 0)
        treatment_val = self.treatment_metrics.get(self.metric_name, 0)
        
        self.absolute_diff = treatment_val - baseline_val
        if baseline_val > 0:
            self.relative_diff_pct = (self.absolute_diff / baseline_val) * 100
            
    def summary(self) -> str:
        direction = "↑" if self.absolute_diff > 0 else "↓"
        sig = "**" if self.is_significant else ""
        return f"{self.metric_name}: {direction}{abs(self.relative_diff_pct):.1f}%{sig}"


class ABTestFramework:
    """
    A/B Testing framework for comparing optimization variants.
    
    Usage:
        ab = ABTestFramework()
        ab.record_baseline("pass_per_sim", [0.05, 0.04, 0.06, ...])
        ab.record_treatment("pass_per_sim", [0.08, 0.07, 0.09, ...])
        result = ab.compare("pass_per_sim")
    """
    
    def __init__(self, min_samples: int = 30, confidence_level: float = 0.95):
        self.min_samples = min_samples
        self.confidence_level = confidence_level
        self.baseline_samples: Dict[str, List[float]] = {}
        self.treatment_samples: Dict[str, List[float]] = {}
        
    def record_baseline(self, metric_name: str, values: List[float]):
        """Record baseline metric values"""
        if metric_name not in self.baseline_samples:
            self.baseline_samples[metric_name] = []
        self.baseline_samples[metric_name].extend(values)
        
    def record_treatment(self, metric_name: str, values: List[float]):
        """Record treatment metric values"""
        if metric_name not in self.treatment_samples:
            self.treatment_samples[metric_name] = []
        self.treatment_samples[metric_name].extend(values)
        
    def compare(self, metric_name: str) -> ABTestResult:
        """Compare baseline vs treatment for a metric"""
        baseline = self.baseline_samples.get(metric_name, [])
        treatment = self.treatment_samples.get(metric_name, [])
        
        if len(baseline) < self.min_samples or len(treatment) < self.min_samples:
            logger.warning(f"[ABTest] Insufficient samples for {metric_name}: "
                          f"baseline={len(baseline)}, treatment={len(treatment)}")
                          
        baseline_mean = np.mean(baseline) if baseline else 0
        treatment_mean = np.mean(treatment) if treatment else 0
        
        # Simple t-test for significance
        is_sig, p_val, ci = self._ttest(baseline, treatment)
        
        return ABTestResult(
            baseline_metrics={metric_name: baseline_mean},
            treatment_metrics={metric_name: treatment_mean},
            metric_name=metric_name,
            is_significant=is_sig,
            p_value=p_val,
            confidence_interval=ci
        )
        
    def _ttest(self, a: List[float], b: List[float]) -> Tuple[bool, float, Tuple[float, float]]:
        """Perform two-sample t-test"""
        if len(a) < 2 or len(b) < 2:
            return False, 1.0, (0.0, 0.0)
            
        from scipy import stats
        try:
            stat, p_value = stats.ttest_ind(a, b)
            is_significant = p_value < (1 - self.confidence_level)
            
            # Confidence interval for difference
            diff = np.mean(b) - np.mean(a)
            se = np.sqrt(np.var(a)/len(a) + np.var(b)/len(b))
            z = stats.norm.ppf((1 + self.confidence_level) / 2)
            ci = (diff - z * se, diff + z * se)
            
            return is_significant, p_value, ci
        except Exception:
            return False, 1.0, (0.0, 0.0)
            
    def get_all_comparisons(self) -> List[ABTestResult]:
        """Compare all metrics"""
        metrics = set(self.baseline_samples.keys()) | set(self.treatment_samples.keys())
        return [self.compare(m) for m in metrics]
        
    def generate_report(self) -> str:
        """Generate human-readable A/B test report"""
        lines = [
            "=" * 60,
            "A/B TEST REPORT",
            "=" * 60,
            f"Confidence Level: {self.confidence_level * 100}%",
            f"Min Samples Required: {self.min_samples}",
            "",
            "RESULTS:",
            "-" * 40,
        ]
        
        for result in self.get_all_comparisons():
            baseline_n = len(self.baseline_samples.get(result.metric_name, []))
            treatment_n = len(self.treatment_samples.get(result.metric_name, []))
            
            lines.append(f"\n{result.metric_name}:")
            lines.append(f"  Baseline:  {result.baseline_metrics.get(result.metric_name, 0):.4f} (n={baseline_n})")
            lines.append(f"  Treatment: {result.treatment_metrics.get(result.metric_name, 0):.4f} (n={treatment_n})")
            lines.append(f"  Change:    {result.relative_diff_pct:+.1f}%")
            lines.append(f"  P-value:   {result.p_value:.4f}")
            lines.append(f"  Significant: {'YES ✓' if result.is_significant else 'NO'}")
            
        lines.append("")
        lines.append("=" * 60)
        
        return "\n".join(lines)


# =============================================================================
# Experiment Runner
# =============================================================================

class ExperimentRunner:
    """
    Unified experiment runner with full observability.
    
    Coordinates metrics collection, reproducibility, and A/B testing.
    """
    
    def __init__(
        self,
        experiment_id: str,
        config: ExperimentConfig,
        persist_path: Optional[Path] = None
    ):
        self.experiment_id = experiment_id
        self.config = config
        self.persist_path = persist_path or Path("experiments") / experiment_id
        
        # Initialize components
        self.metrics = MetricsCollector(experiment_id, self.persist_path)
        self.ab_test = ABTestFramework()
        
        # Set seeds
        set_reproducibility_seeds(config.random_seed)
        
        # Track state
        self.is_baseline = True
        self.iteration_count = 0
        
    def start_iteration(self, iteration_id: Optional[str] = None) -> "IterationContext":
        """Start a new iteration with context manager"""
        self.iteration_count += 1
        return IterationContext(
            runner=self,
            iteration_id=iteration_id or f"iter_{self.iteration_count}",
            is_baseline=self.is_baseline
        )
        
    def set_mode(self, is_baseline: bool):
        """Set baseline or treatment mode"""
        self.is_baseline = is_baseline
        mode = "BASELINE" if is_baseline else "TREATMENT"
        logger.info(f"[Experiment] Mode set to {mode}")
        
    def record_iteration_result(
        self,
        simulations: int,
        passes: int,
        duration_ms: int,
        dedup_skipped: int = 0,
        corr_skipped: int = 0,
        diversity_score: float = 0.0
    ):
        """Record results from a mining iteration"""
        # Update counters
        self.metrics.increment("simulation_count", simulations)
        self.metrics.increment("pass_count", passes)
        self.metrics.record("iteration_duration_ms", duration_ms)
        
        # Calculate rates
        pass_rate = passes / simulations if simulations > 0 else 0
        dedup_rate = dedup_skipped / (simulations + dedup_skipped) if (simulations + dedup_skipped) > 0 else 0
        
        self.metrics.record("pass_per_sim", pass_rate)
        self.metrics.record("dedup_skip_rate", dedup_rate * 100)
        self.metrics.record("diversity_score", diversity_score)
        
        if corr_skipped > 0:
            self.metrics.record("corr_check_skip_rate", corr_skipped)
            
        # Record for A/B testing
        if self.is_baseline:
            self.ab_test.record_baseline("pass_per_sim", [pass_rate])
            self.ab_test.record_baseline("diversity_score", [diversity_score])
        else:
            self.ab_test.record_treatment("pass_per_sim", [pass_rate])
            self.ab_test.record_treatment("diversity_score", [diversity_score])
            
    async def finalize(self) -> Dict[str, Any]:
        """Finalize experiment and generate reports"""
        # Persist metrics
        await self.metrics.persist()
        
        # Save config
        config_file = self.persist_path / f"{self.experiment_id}_config.json"
        self.persist_path.mkdir(parents=True, exist_ok=True)
        with open(config_file, 'w') as f:
            json.dump(self.config.to_dict(), f, indent=2, default=str)
            
        # Generate A/B report if we have both modes
        ab_report = self.ab_test.generate_report()
        report_file = self.persist_path / f"{self.experiment_id}_ab_report.txt"
        with open(report_file, 'w') as f:
            f.write(ab_report)
            
        logger.info(f"[Experiment] Finalized {self.experiment_id}")
        logger.info(f"\n{ab_report}")
        
        return {
            "summary": self.metrics.get_summary(),
            "config": self.config.to_dict(),
            "ab_report": ab_report,
        }


@dataclass
class IterationContext:
    """Context manager for a single iteration"""
    runner: ExperimentRunner
    iteration_id: str
    is_baseline: bool
    
    _start_time: datetime = field(default_factory=datetime.now)
    _simulations: int = 0
    _passes: int = 0
    _dedup_skipped: int = 0
    _corr_skipped: int = 0
    
    def __enter__(self):
        self._start_time = datetime.now()
        logger.info(f"[Iteration] Started {self.iteration_id} (baseline={self.is_baseline})")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = int((datetime.now() - self._start_time).total_seconds() * 1000)
        self.runner.record_iteration_result(
            simulations=self._simulations,
            passes=self._passes,
            duration_ms=duration_ms,
            dedup_skipped=self._dedup_skipped,
            corr_skipped=self._corr_skipped
        )
        logger.info(f"[Iteration] Completed {self.iteration_id} in {duration_ms}ms | "
                   f"sims={self._simulations} pass={self._passes}")
        return False
        
    def record_simulation(self, success: bool):
        """Record a simulation result"""
        self._simulations += 1
        if success:
            self._passes += 1
            
    def record_dedup_skip(self, count: int = 1):
        """Record deduplication skips"""
        self._dedup_skipped += count
        
    def record_corr_skip(self, count: int = 1):
        """Record correlation check skips"""
        self._corr_skipped += count


# =============================================================================
# Singleton Instance for Global Access
# =============================================================================

_current_experiment: Optional[ExperimentRunner] = None


def get_current_experiment() -> Optional[ExperimentRunner]:
    """Get current active experiment"""
    return _current_experiment


def set_current_experiment(experiment: ExperimentRunner):
    """Set current active experiment"""
    global _current_experiment
    _current_experiment = experiment
    

def create_experiment(
    experiment_id: str,
    settings,
    is_baseline: bool = True,
    **config_overrides
) -> ExperimentRunner:
    """
    Create and register a new experiment.
    
    Usage:
        from backend.experiment_tracker import create_experiment
        
        exp = create_experiment(
            "exp_p0_optimization_001",
            settings,
            is_baseline=False,
            two_stage_corr_enabled=True
        )
        
        with exp.start_iteration() as ctx:
            # ... run mining iteration
            ctx.record_simulation(success=True)
            
        await exp.finalize()
    """
    config = ExperimentConfig.from_settings(experiment_id, settings, **config_overrides)
    runner = ExperimentRunner(experiment_id, config)
    runner.set_mode(is_baseline)
    set_current_experiment(runner)
    
    logger.info(f"[Experiment] Created {experiment_id} | config_hash={config.config_hash}")
    
    return runner
