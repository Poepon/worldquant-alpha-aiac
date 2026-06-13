"""
Metrics Tracker - Enhanced Logging and Performance Monitoring

Features:
1. Per-round metrics tracking (pass_rate, avg_sharpe, diversity)
2. Session-level aggregations
3. Knowledge base evolution tracking
4. Quality reports generation
5. Debug log integration

This module provides comprehensive observability for the mining system.
"""

import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
from loguru import logger

# Try to import SQLAlchemy components (may not be available in all contexts)
try:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import select, func
    from backend.models import KnowledgeEntry, Alpha, MiningTask
    HAS_DB = True
except ImportError:
    HAS_DB = False


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class RoundMetrics:
    """Metrics for a single mining round."""
    round_id: int
    task_id: int
    timestamp: datetime = field(default_factory=datetime.now)
    
    # Core metrics
    alphas_generated: int = 0
    alphas_passed: int = 0
    alphas_failed: int = 0
    alphas_optimized: int = 0
    
    # Performance metrics
    avg_sharpe: float = 0.0
    max_sharpe: float = 0.0
    avg_fitness: float = 0.0
    avg_turnover: float = 0.0
    
    # Diversity metrics
    unique_datasets: int = 0
    unique_operators: int = 0
    unique_fields: int = 0
    diversity_score: float = 0.0
    
    # Efficiency metrics
    simulation_count: int = 0
    duration_seconds: float = 0.0
    
    # Strategy info
    strategy_mode: str = ""
    dataset_id: str = ""
    region: str = ""
    
    @property
    def pass_rate(self) -> float:
        """Calculate pass rate."""
        total = self.alphas_passed + self.alphas_failed
        return self.alphas_passed / total if total > 0 else 0.0
    
    @property
    def efficiency(self) -> float:
        """Passes per simulation."""
        return self.alphas_passed / self.simulation_count if self.simulation_count > 0 else 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["pass_rate"] = round(self.pass_rate, 4)
        result["efficiency"] = round(self.efficiency, 4)
        result["timestamp"] = self.timestamp.isoformat()
        return result


@dataclass
class SessionMetrics:
    """Aggregated metrics for an entire mining session."""
    session_id: str
    task_id: int
    started_at: datetime = field(default_factory=datetime.now)
    ended_at: datetime = None
    
    # Round summaries
    rounds_completed: int = 0
    total_alphas_generated: int = 0
    total_alphas_passed: int = 0
    total_simulations: int = 0
    
    # Performance summaries
    best_sharpe: float = 0.0
    avg_pass_rate: float = 0.0
    
    # Cumulative diversity
    datasets_explored: set = field(default_factory=set)
    operators_used: set = field(default_factory=set)
    
    # Knowledge evolution
    patterns_added: int = 0
    pitfalls_added: int = 0
    
    # Round history
    round_metrics: List[RoundMetrics] = field(default_factory=list)
    
    def add_round(self, round_metrics: RoundMetrics):
        """Add round metrics to session."""
        self.round_metrics.append(round_metrics)
        self.rounds_completed += 1
        self.total_alphas_generated += round_metrics.alphas_generated
        self.total_alphas_passed += round_metrics.alphas_passed
        self.total_simulations += round_metrics.simulation_count
        
        if round_metrics.max_sharpe > self.best_sharpe:
            self.best_sharpe = round_metrics.max_sharpe
        
        # Recalculate average pass rate
        pass_rates = [r.pass_rate for r in self.round_metrics if r.alphas_generated > 0]
        self.avg_pass_rate = sum(pass_rates) / len(pass_rates) if pass_rates else 0.0
    
    @property
    def overall_pass_rate(self) -> float:
        """Overall pass rate across all rounds."""
        total = self.total_alphas_passed + (self.total_alphas_generated - self.total_alphas_passed)
        return self.total_alphas_passed / total if total > 0 else 0.0
    
    @property
    def duration_minutes(self) -> float:
        """Session duration in minutes."""
        end = self.ended_at or datetime.now()
        return (end - self.started_at).total_seconds() / 60
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_minutes": round(self.duration_minutes, 2),
            "rounds_completed": self.rounds_completed,
            "total_alphas_generated": self.total_alphas_generated,
            "total_alphas_passed": self.total_alphas_passed,
            "overall_pass_rate": round(self.overall_pass_rate, 4),
            "avg_pass_rate": round(self.avg_pass_rate, 4),
            "best_sharpe": round(self.best_sharpe, 4),
            "total_simulations": self.total_simulations,
            "datasets_explored": len(self.datasets_explored),
            "patterns_added": self.patterns_added,
            "pitfalls_added": self.pitfalls_added,
        }


@dataclass
class NodeMetrics:
    """Per-LLM-call-site (node) aggregates.

    Used to track tokens / latency / success rate broken down by graph
    node (hypothesis / code_gen / self_correct / ...) and effort tier.
    LLMService emits one sample per call via `record_llm_call`; the
    An offline A/B comparison reads snapshots from `get_node_metrics_snapshot`.
    """
    node_key: str
    # effort_breakdown[effort_tier] → call count at that tier (covers the
    # case where a single node temporarily switched tier during a session)
    effort_breakdown: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    call_count: int = 0
    tokens_total: int = 0
    latency_ms_total: int = 0
    success_count: int = 0
    failure_count: int = 0

    def record(self, effort: str, tokens: int, latency_ms: int, success: bool) -> None:
        self.call_count += 1
        self.effort_breakdown[effort] += 1
        self.tokens_total += max(0, int(tokens or 0))
        self.latency_ms_total += max(0, int(latency_ms or 0))
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_key": self.node_key,
            "effort_breakdown": dict(self.effort_breakdown),
            "call_count": self.call_count,
            "tokens_total": self.tokens_total,
            "latency_ms_total": self.latency_ms_total,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "tokens_avg": round(self.tokens_total / self.call_count, 1) if self.call_count else 0.0,
            "latency_ms_avg": round(self.latency_ms_total / self.call_count, 1) if self.call_count else 0.0,
            "success_rate": round(self.success_count / self.call_count, 4) if self.call_count else 0.0,
        }


@dataclass
class KnowledgeMetrics:
    """Metrics about knowledge base evolution."""
    timestamp: datetime = field(default_factory=datetime.now)
    
    # Counts by type
    success_patterns: int = 0
    failure_pitfalls: int = 0
    field_insights: int = 0
    total_entries: int = 0
    
    # Quality metrics
    avg_pattern_score: float = 0.0
    avg_usage_count: float = 0.0
    patterns_with_high_score: int = 0  # score > 0.7
    
    # Source distribution
    by_source: Dict[str, int] = field(default_factory=dict)
    
    # Recent changes
    added_last_24h: int = 0
    deactivated_last_24h: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "success_patterns": self.success_patterns,
            "failure_pitfalls": self.failure_pitfalls,
            "total_entries": self.total_entries,
            "avg_pattern_score": round(self.avg_pattern_score, 4),
            "avg_usage_count": round(self.avg_usage_count, 2),
            "patterns_with_high_score": self.patterns_with_high_score,
            "by_source": self.by_source,
            "added_last_24h": self.added_last_24h,
        }


# =============================================================================
# Metrics Tracker
# =============================================================================

class MetricsTracker:
    """
    Central metrics tracking and logging service.
    
    Usage:
        tracker = MetricsTracker(task_id=123, debug_log_path="debug.log")
        
        # Start session
        tracker.start_session()
        
        # Track each round
        round_metrics = tracker.create_round_metrics(round_id=1)
        round_metrics.alphas_generated = 10
        round_metrics.alphas_passed = 2
        tracker.complete_round(round_metrics)
        
        # Get reports
        report = tracker.generate_report()
    """
    
    def __init__(
        self,
        task_id: int = 0,
        debug_log_path: str = None,
        db: Optional[Any] = None
    ):
        self.task_id = task_id
        self.db = db
        
        # Debug log file
        if debug_log_path:
            self.debug_log_path = Path(debug_log_path)
        else:
            # Default to .cursor/debug.log
            self.debug_log_path = Path(".cursor/debug.log")
        
        # Session tracking
        self.current_session: Optional[SessionMetrics] = None
        self.sessions: List[SessionMetrics] = []
        
        # Knowledge tracking (periodic snapshots)
        self.knowledge_snapshots: List[KnowledgeMetrics] = []
    
    def start_session(self, session_id: str = None) -> SessionMetrics:
        """Start a new mining session."""
        session_id = session_id or f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        self.current_session = SessionMetrics(
            session_id=session_id,
            task_id=self.task_id
        )
        
        self._log_debug(f"[Session Start] id={session_id} task={self.task_id}")
        
        return self.current_session
    
    def end_session(self) -> Optional[SessionMetrics]:
        """End current session and return metrics."""
        if not self.current_session:
            return None
        
        self.current_session.ended_at = datetime.now()
        self.sessions.append(self.current_session)
        
        # Log summary
        summary = self.current_session.to_dict()
        self._log_debug(f"[Session End] {json.dumps(summary, indent=2)}")
        
        session = self.current_session
        self.current_session = None
        
        return session
    
    def create_round_metrics(
        self,
        round_id: int,
        dataset_id: str = "",
        region: str = "",
        strategy_mode: str = ""
    ) -> RoundMetrics:
        """Create metrics object for a new round."""
        return RoundMetrics(
            round_id=round_id,
            task_id=self.task_id,
            dataset_id=dataset_id,
            region=region,
            strategy_mode=strategy_mode
        )
    
    def complete_round(self, metrics: RoundMetrics):
        """Complete a round and add to session."""
        if self.current_session:
            self.current_session.add_round(metrics)
        
        # Log round metrics
        self._log_debug(f"[Round {metrics.round_id} Complete] {json.dumps(metrics.to_dict(), indent=2)}")
        
        # Log key metrics at info level
        logger.info(
            f"[Metrics] Round {metrics.round_id} | "
            f"pass_rate={metrics.pass_rate:.1%} "
            f"generated={metrics.alphas_generated} "
            f"passed={metrics.alphas_passed} "
            f"avg_sharpe={metrics.avg_sharpe:.2f} "
            f"diversity={metrics.diversity_score:.2f}"
        )
    
    def track_alpha_result(
        self,
        round_metrics: RoundMetrics,
        expression: str,
        passed: bool,
        sharpe: float = 0.0,
        fitness: float = 0.0,
        turnover: float = 0.0,
        dataset_id: str = None,
        operators: List[str] = None,
        fields: List[str] = None
    ):
        """Track individual alpha result."""
        round_metrics.alphas_generated += 1
        
        if passed:
            round_metrics.alphas_passed += 1
        else:
            round_metrics.alphas_failed += 1
        
        # Update averages
        n = round_metrics.alphas_generated
        round_metrics.avg_sharpe = (round_metrics.avg_sharpe * (n-1) + sharpe) / n
        round_metrics.avg_fitness = (round_metrics.avg_fitness * (n-1) + fitness) / n
        round_metrics.avg_turnover = (round_metrics.avg_turnover * (n-1) + turnover) / n
        
        if sharpe > round_metrics.max_sharpe:
            round_metrics.max_sharpe = sharpe
        
        # Track diversity
        if dataset_id:
            if self.current_session:
                self.current_session.datasets_explored.add(dataset_id)
            round_metrics.unique_datasets = len(self.current_session.datasets_explored) if self.current_session else 1
        
        if operators:
            if self.current_session:
                self.current_session.operators_used.update(operators)
            round_metrics.unique_operators = len(self.current_session.operators_used) if self.current_session else len(operators)
        
        # Log individual alpha (debug level)
        status = "PASS" if passed else "FAIL"
        self._log_debug(
            f"[Alpha {status}] sharpe={sharpe:.3f} fitness={fitness:.3f} "
            f"turnover={turnover:.3f} expr={expression[:100]}..."
        )
    
    def track_knowledge_change(
        self,
        change_type: str,  # "pattern_added", "pitfall_added", "pattern_used", "pattern_deactivated"
        pattern: str = "",
        metadata: Dict = None
    ):
        """Track knowledge base changes."""
        if self.current_session:
            if change_type == "pattern_added":
                self.current_session.patterns_added += 1
            elif change_type == "pitfall_added":
                self.current_session.pitfalls_added += 1
        
        self._log_debug(f"[Knowledge] {change_type} | pattern={pattern[:50]} metadata={metadata}")
    
    async def snapshot_knowledge_metrics(self) -> Optional[KnowledgeMetrics]:
        """Take snapshot of knowledge base metrics."""
        if not HAS_DB or not self.db:
            return None
        
        try:
            metrics = KnowledgeMetrics()
            
            # Count by type
            type_query = select(
                KnowledgeEntry.entry_type,
                func.count(KnowledgeEntry.id).label('count')
            ).where(
                KnowledgeEntry.is_active == True
            ).group_by(KnowledgeEntry.entry_type)
            
            result = await self.db.execute(type_query)
            type_counts = dict(result.fetchall())
            
            metrics.success_patterns = type_counts.get('SUCCESS_PATTERN', 0)
            metrics.failure_pitfalls = type_counts.get('FAILURE_PITFALL', 0)
            metrics.field_insights = type_counts.get('FIELD_INSIGHT', 0)
            metrics.total_entries = sum(type_counts.values())
            
            # Average metrics
            avg_query = select(
                func.avg(KnowledgeEntry.usage_count).label('avg_usage'),
            ).where(
                KnowledgeEntry.is_active == True
            )
            
            result = await self.db.execute(avg_query)
            row = result.fetchone()
            if row:
                metrics.avg_usage_count = float(row.avg_usage or 0)
            
            # Recent changes
            cutoff = datetime.now() - timedelta(hours=24)
            recent_query = select(func.count(KnowledgeEntry.id)).where(
                KnowledgeEntry.created_at >= cutoff
            )
            result = await self.db.execute(recent_query)
            metrics.added_last_24h = result.scalar() or 0
            
            self.knowledge_snapshots.append(metrics)
            self._log_debug(f"[Knowledge Snapshot] {json.dumps(metrics.to_dict(), indent=2)}")
            
            return metrics
            
        except Exception as e:
            logger.warning(f"[MetricsTracker] Knowledge snapshot failed: {e}")
            return None
    
    def calculate_diversity_score(
        self,
        round_metrics: RoundMetrics,
        total_datasets: int = 100,
        total_operators: int = 50
    ) -> float:
        """Calculate diversity score for a round."""
        # Normalize components
        dataset_diversity = min(1.0, round_metrics.unique_datasets / 5)  # 5 datasets = max
        operator_diversity = min(1.0, round_metrics.unique_operators / 15)  # 15 operators = max
        field_diversity = min(1.0, round_metrics.unique_fields / 20)  # 20 fields = max
        
        # Weighted combination
        score = 0.4 * dataset_diversity + 0.35 * operator_diversity + 0.25 * field_diversity
        
        round_metrics.diversity_score = score
        return score
    
    def generate_report(self) -> Dict[str, Any]:
        """Generate comprehensive metrics report."""
        report = {
            "generated_at": datetime.now().isoformat(),
            "task_id": self.task_id,
        }
        
        # Current session
        if self.current_session:
            report["current_session"] = self.current_session.to_dict()
        
        # Historical sessions
        if self.sessions:
            report["sessions_count"] = len(self.sessions)
            report["total_alphas_passed"] = sum(s.total_alphas_passed for s in self.sessions)
            report["total_simulations"] = sum(s.total_simulations for s in self.sessions)
            report["avg_session_pass_rate"] = sum(s.avg_pass_rate for s in self.sessions) / len(self.sessions)
        
        # Knowledge evolution
        if self.knowledge_snapshots:
            latest = self.knowledge_snapshots[-1]
            report["knowledge_metrics"] = latest.to_dict()
            
            # Trend
            if len(self.knowledge_snapshots) > 1:
                first = self.knowledge_snapshots[0]
                report["knowledge_growth"] = {
                    "patterns_added": latest.success_patterns - first.success_patterns,
                    "pitfalls_added": latest.failure_pitfalls - first.failure_pitfalls,
                }
        
        return report
    
    def _log_debug(self, message: str):
        """Write to debug log file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"
        
        try:
            # Ensure directory exists
            self.debug_log_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Append to log file
            with open(self.debug_log_path, "a", encoding="utf-8") as f:
                f.write(log_line)
                
        except Exception as e:
            logger.warning(f"Failed to write debug log: {e}")


# =============================================================================
# Per-Node LLM Metrics (module-level accumulator)
# =============================================================================
# Decoupled from MetricsTracker / session lifecycle: LLMService is a process-
# wide singleton and emits one sample per call regardless of which task is
# active. Snapshot via `get_node_metrics_snapshot()` at round/session
# boundaries to fold into SessionMetrics; reset with `reset_node_metrics()`
# in A/B scripts between phases.

_node_metrics: Dict[str, NodeMetrics] = {}


def record_llm_call(
    node_key: str,
    effort: str,
    tokens: int,
    latency_ms: int,
    success: bool,
) -> None:
    """Record one LLM call sample (called from LLMService.call).

    Safe to call from any thread/coroutine; the GIL serializes dict
    mutation. Failure-isolated — never raises, so a metrics bug can
    never break the LLM call path.
    """
    try:
        key = node_key or "_unspecified"
        entry = _node_metrics.get(key)
        if entry is None:
            entry = NodeMetrics(node_key=key)
            _node_metrics[key] = entry
        entry.record(effort=effort, tokens=tokens, latency_ms=latency_ms, success=success)
    except Exception:
        # 严格不抛 — 指标累加不能干扰 LLM 主路径
        pass


def get_node_metrics_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a deep snapshot of per-node aggregates (safe for serialization)."""
    return {k: v.to_dict() for k, v in _node_metrics.items()}


def reset_node_metrics() -> None:
    """Wipe the accumulator (used by A/B scripts between phases / tests)."""
    _node_metrics.clear()
