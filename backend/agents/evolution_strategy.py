"""
Evolution Strategy Module

Provides a unified abstraction for mining strategy management across the evolution loop.
This module bridges StrategyAgent output with the actual mining execution.

Design Principles:
1. Immutable Strategy Objects: Strategies are value objects, not modified in place
2. Clear State Machine: Strategy transitions are explicit and traceable
3. Separation of Concerns: Strategy generation vs Strategy application
4. Testability: Pure functions where possible
"""

from __future__ import annotations
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, List, Dict, Any, Optional, Protocol, Tuple
from enum import Enum
import json


class StrategyMode(Enum):
    """Strategy modes that determine exploration/exploitation balance."""
    EXPLORE = "explore"       # High diversity, novel approaches
    EXPLOIT = "exploit"       # Refine successful patterns
    BALANCED = "balanced"     # Mix of both
    OPTIMIZE = "optimize"     # Focus on improving existing alphas
    RESCUE = "rescue"         # Emergency mode after repeated failures


@dataclass(frozen=True)
class EvolutionStrategy:
    """
    Immutable strategy object that guides a mining iteration.
    
    This is the single source of truth for all strategy parameters.
    Created by StrategyAgent, consumed by MiningWorkflow.
    """
    # Core parameters
    mode: StrategyMode = StrategyMode.BALANCED
    temperature: float = 0.7
    exploration_weight: float = 0.5
    
    # Field guidance
    preferred_fields: tuple = field(default_factory=tuple)
    avoid_fields: tuple = field(default_factory=tuple)
    screened_fields: tuple = field(default_factory=tuple)  # From FieldScreener
    
    # Hypothesis guidance
    focus_hypotheses: tuple = field(default_factory=tuple)
    avoid_patterns: tuple = field(default_factory=tuple)
    amplify_patterns: tuple = field(default_factory=tuple)
    
    # Operator guidance
    preferred_operators: tuple = field(default_factory=tuple)
    avoid_operators: tuple = field(default_factory=tuple)
    
    # Optimization targets (Chain-of-Alpha style)
    optimization_targets: tuple = field(default_factory=tuple)
    
    # Metadata
    action_summary: str = ""
    reasoning: str = ""
    iteration: int = 0

    # P2-C (2026-05-16): regime-aware threshold gating + style preset encoding.
    # ``regime`` is the smoothed 5-bucket market label (one of REGIME_ORDER:
    # crisis/elevated/normal/calm/very_calm) injected by mining_agent.
    # ``style_preset`` is the serialised RegimePreset dict (style_label,
    # style_philosophy, pillar_bias, regime). None on both = byte-for-byte
    # legacy (the three P2-C flags all default OFF; the injection only runs
    # when at least one effect flag is True).
    regime: Optional[str] = None
    style_preset: Optional[Dict[str, Any]] = None

    def with_updates(self, **kwargs) -> EvolutionStrategy:
        """Create new strategy with specified updates (immutable pattern)."""
        return replace(self, **kwargs)
    
    def to_prompt_context(self) -> Dict[str, Any]:
        """Convert strategy to prompt context dictionary."""
        return {
            "exploration_weight": self.exploration_weight,
            "preferred_fields": list(self.preferred_fields),
            "avoid_fields": list(self.avoid_fields),
            "focus_hypotheses": list(self.focus_hypotheses),
            "avoid_patterns": list(self.avoid_patterns),
        }
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for logging/persistence."""
        return {
            "mode": self.mode.value,
            "temperature": self.temperature,
            "exploration_weight": self.exploration_weight,
            "preferred_fields": list(self.preferred_fields),
            "avoid_fields": list(self.avoid_fields),
            "screened_fields": list(self.screened_fields),
            "focus_hypotheses": list(self.focus_hypotheses),
            "avoid_patterns": list(self.avoid_patterns),
            "amplify_patterns": list(self.amplify_patterns),
            "preferred_operators": list(self.preferred_operators),
            "avoid_operators": list(self.avoid_operators),
            "optimization_targets": list(self.optimization_targets),
            "action_summary": self.action_summary,
            "reasoning": self.reasoning,
            "iteration": self.iteration,
            # P2-C (2026-05-16)
            "regime": self.regime,
            "style_preset": dict(self.style_preset) if self.style_preset else None,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> EvolutionStrategy:
        """Deserialize from dictionary."""
        mode_str = data.get("mode", "balanced")
        try:
            mode = StrategyMode(mode_str)
        except ValueError:
            mode = StrategyMode.BALANCED
        
        # P2-C (2026-05-16) — round-trip new optional fields. Missing keys
        # in legacy serialised blobs default to None so existing call sites
        # keep working byte-for-byte.
        _sp_raw = data.get("style_preset")
        _style_preset = dict(_sp_raw) if isinstance(_sp_raw, dict) else None
        return cls(
            mode=mode,
            temperature=data.get("temperature", 0.7),
            exploration_weight=data.get("exploration_weight", 0.5),
            preferred_fields=tuple(data.get("preferred_fields", [])),
            avoid_fields=tuple(data.get("avoid_fields", [])),
            screened_fields=tuple(data.get("screened_fields", [])),
            focus_hypotheses=tuple(data.get("focus_hypotheses", [])),
            avoid_patterns=tuple(data.get("avoid_patterns", [])),
            amplify_patterns=tuple(data.get("amplify_patterns", [])),
            preferred_operators=tuple(data.get("preferred_operators", [])),
            avoid_operators=tuple(data.get("avoid_operators", [])),
            optimization_targets=tuple(data.get("optimization_targets", [])),
            action_summary=data.get("action_summary", ""),
            reasoning=data.get("reasoning", ""),
            iteration=data.get("iteration", 0),
            # P2-C (2026-05-16)
            regime=data.get("regime"),
            style_preset=_style_preset,
        )
    
    @classmethod
    def default(cls) -> EvolutionStrategy:
        """Create default balanced strategy."""
        return cls(
            mode=StrategyMode.BALANCED,
            action_summary="Default balanced strategy",
            reasoning="Initial iteration with no prior data"
        )
    
    @classmethod
    def explore_mode(cls, iteration: int = 0) -> EvolutionStrategy:
        """Create high-exploration strategy."""
        return cls(
            mode=StrategyMode.EXPLORE,
            temperature=0.9,
            exploration_weight=0.8,
            action_summary="Exploration mode: seeking novel approaches",
            reasoning="Prioritizing diversity over refinement",
            iteration=iteration,
        )
    
    @classmethod
    def exploit_mode(cls, successful_patterns: List[str], iteration: int = 0) -> EvolutionStrategy:
        """Create exploitation strategy based on successful patterns."""
        return cls(
            mode=StrategyMode.EXPLOIT,
            temperature=0.5,
            exploration_weight=0.2,
            amplify_patterns=tuple(successful_patterns[:5]),
            action_summary="Exploitation mode: refining successful patterns",
            reasoning="Building on patterns that have shown promise",
            iteration=iteration,
        )
    
    @classmethod
    def rescue_mode(cls, problematic_fields: List[str], iteration: int = 0) -> EvolutionStrategy:
        """Create rescue strategy after repeated failures."""
        return cls(
            mode=StrategyMode.RESCUE,
            temperature=1.0,
            exploration_weight=0.95,
            avoid_fields=tuple(problematic_fields[:10]),
            action_summary="Rescue mode: breaking out of failure pattern",
            reasoning="Multiple failures detected, drastically changing approach",
            iteration=iteration,
        )


@dataclass
class RoundResult:
    """Results from a single mining round, used to generate next strategy."""
    iteration: int
    total_generated: int = 0
    total_simulated: int = 0
    passed_count: int = 0
    failed_count: int = 0
    
    # Quality metrics (from passed alphas)
    best_sharpe: Optional[float] = None
    avg_sharpe: Optional[float] = None
    best_fitness: Optional[float] = None
    avg_fitness: Optional[float] = None
    avg_turnover: Optional[float] = None
    
    # Failure analysis
    syntax_errors: int = 0
    simulation_errors: int = 0
    quality_failures: int = 0
    
    # Identified patterns
    successful_patterns: List[str] = field(default_factory=list)
    problematic_fields: List[str] = field(default_factory=list)
    problematic_operators: List[str] = field(default_factory=list)
    
    # Optimization candidates
    optimization_candidates: List[Dict] = field(default_factory=list)
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        total = self.total_generated
        return self.passed_count / max(total, 1)
    
    @property
    def simulation_rate(self) -> float:
        """Calculate simulation success rate."""
        return self.total_simulated / max(self.total_generated, 1)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging."""
        return {
            "iteration": self.iteration,
            "total_generated": self.total_generated,
            "total_simulated": self.total_simulated,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "success_rate": round(self.success_rate, 3),
            "best_sharpe": self.best_sharpe,
            "avg_sharpe": round(self.avg_sharpe, 3) if self.avg_sharpe else None,
            "best_fitness": self.best_fitness,
            "syntax_errors": self.syntax_errors,
            "simulation_errors": self.simulation_errors,
            "quality_failures": self.quality_failures,
        }


class StrategyTransitionProtocol(Protocol):
    """Protocol for strategy transition logic (dependency injection)."""
    
    def compute_next_strategy(
        self,
        current_strategy: EvolutionStrategy,
        round_result: RoundResult,
        cumulative_success: int,
        target_goal: int,
        max_iterations: int
    ) -> EvolutionStrategy:
        """Compute next strategy based on round results."""
        ...


class RuleBasedTransition:
    """
    Rule-based strategy transition (fallback when LLM unavailable).
    
    Implements clear, deterministic rules for strategy evolution.
    """
    
    # Thresholds for strategy transitions
    EXPLORE_THRESHOLD = 0.1   # Below this success rate -> explore more
    EXPLOIT_THRESHOLD = 0.5   # Above this success rate -> exploit more
    RESCUE_THRESHOLD = 0      # Zero success -> rescue mode
    
    def compute_next_strategy(
        self,
        current_strategy: EvolutionStrategy,
        round_result: RoundResult,
        cumulative_success: int,
        target_goal: int,
        max_iterations: int
    ) -> EvolutionStrategy:
        """Compute next strategy using deterministic rules."""
        
        success_rate = round_result.success_rate
        progress = cumulative_success / max(target_goal, 1)
        remaining = max_iterations - round_result.iteration
        
        # Determine mode
        if success_rate == self.RESCUE_THRESHOLD and round_result.total_generated > 0:
            mode = StrategyMode.RESCUE
            temperature = 1.0
            exploration_weight = 0.95
            action = "Rescue: zero success, drastically changing approach"
        elif success_rate < self.EXPLORE_THRESHOLD:
            mode = StrategyMode.EXPLORE
            temperature = min(1.0, 0.7 + 0.1 * round_result.iteration)
            exploration_weight = min(0.9, 0.5 + 0.1 * round_result.iteration)
            action = f"Explore: low success rate ({success_rate:.1%})"
        elif success_rate > self.EXPLOIT_THRESHOLD:
            mode = StrategyMode.EXPLOIT
            temperature = max(0.3, 0.7 - 0.1 * round_result.passed_count)
            exploration_weight = 0.3
            action = f"Exploit: good success rate ({success_rate:.1%})"
        else:
            mode = StrategyMode.BALANCED
            temperature = 0.7
            exploration_weight = 0.5
            action = "Balanced: moderate success rate"
        
        # Urgency adjustment: if behind schedule, increase exploration
        expected_progress = round_result.iteration / max_iterations
        if progress < expected_progress * 0.7 and remaining > 1:
            exploration_weight = min(1.0, exploration_weight + 0.2)
            action += " [urgency boost]"
        
        # Build avoidance lists
        avoid_fields = tuple(round_result.problematic_fields[:5])
        avoid_operators = tuple(round_result.problematic_operators[:3])
        
        # Build amplification list from successful patterns
        amplify = tuple(round_result.successful_patterns[:5])
        
        # Identify optimization candidates
        opt_targets = tuple(
            c.get("expression", "") 
            for c in round_result.optimization_candidates[:3]
        )
        
        return EvolutionStrategy(
            mode=mode,
            temperature=temperature,
            exploration_weight=exploration_weight,
            avoid_fields=avoid_fields,
            avoid_operators=avoid_operators,
            amplify_patterns=amplify,
            optimization_targets=opt_targets,
            action_summary=action,
            reasoning=f"Success: {success_rate:.1%}, Progress: {progress:.1%}",
            iteration=round_result.iteration + 1,
        )


def merge_strategies(
    base: EvolutionStrategy,
    llm_strategy: Optional[Dict],
    rule_strategy: EvolutionStrategy
) -> EvolutionStrategy:
    """
    Merge LLM-generated strategy with rule-based fallback.
    
    LLM provides creativity, rules provide guardrails.
    """
    if not llm_strategy:
        return rule_strategy
    
    # Extract LLM suggestions with validation
    next_strat = llm_strategy.get("strategy", llm_strategy.get("next_strategy", {}))
    
    # Use LLM values where provided and valid, else fall back to rules
    temperature = next_strat.get("temperature")
    if temperature is None or not (0.0 <= temperature <= 1.0):
        temperature = rule_strategy.temperature
    
    exploration_weight = next_strat.get("exploration_weight")
    if exploration_weight is None or not (0.0 <= exploration_weight <= 1.0):
        exploration_weight = rule_strategy.exploration_weight
    
    # Merge lists (combine LLM suggestions with rule-based avoidances)
    preferred_fields = tuple(
        next_strat.get("preferred_fields", [])
    )
    avoid_fields = tuple(set(
        list(next_strat.get("avoid_fields", [])) +
        list(rule_strategy.avoid_fields)
    ))
    
    focus_hypotheses = tuple(
        next_strat.get("focus_hypotheses", [])
    )
    avoid_patterns = tuple(
        next_strat.get("avoid_patterns", [])
    )
    amplify_patterns = tuple(
        next_strat.get("amplify_patterns", []) or
        list(rule_strategy.amplify_patterns)
    )
    
    # Extract optimization targets
    opt_targets_raw = llm_strategy.get("optimization_targets", [])
    opt_targets = tuple(
        t.get("expression", t) if isinstance(t, dict) else t
        for t in opt_targets_raw[:5]
    )
    
    return EvolutionStrategy(
        mode=rule_strategy.mode,  # Mode from rules (more reliable)
        temperature=temperature,
        exploration_weight=exploration_weight,
        preferred_fields=preferred_fields,
        avoid_fields=avoid_fields,
        focus_hypotheses=focus_hypotheses,
        avoid_patterns=avoid_patterns,
        amplify_patterns=amplify_patterns,
        optimization_targets=opt_targets or rule_strategy.optimization_targets,
        action_summary=next_strat.get("action_summary", rule_strategy.action_summary),
        reasoning=next_strat.get("reasoning", rule_strategy.reasoning),
        iteration=rule_strategy.iteration,
    )


# ============================================================================
# Phase 1 R2/Q7 (2026-05-17): Contextual Thompson Sampling DirectionBandit
# ============================================================================
#
# Arms reflect AIAC's strategy-generation-mode space (not RD-Agent's
# task-direction arms — plan v1.4 §4.2 caveat). R1a Phase 0 production data
# (236 rows / 18:3 hypothesis-dominant) locks the default 4-arm set per
# plan §4.2 hypothesis-dominant branch.
#
# Per-task contextual segments hash on (region, dataset_category,
# recent_failure_pattern). Cold segments (< 5 pulls) fall back to a global
# prior aggregated across all segments to avoid noisy random sampling on
# fresh tasks.
#
# State persistence: ``mining_tasks.config["contextual_bandit_v1"]`` JSONB —
# no Alembic in Phase 1. Off-policy log writes to ``direction_bandit_log``
# table (dedicated INSERT, R1a v1.6 lesson — don't piggyback on alpha
# persistence routing).

import logging
import random
from collections import Counter

# NOTE: ``asdict`` and ``Tuple`` were previously re-imported here (LOW from
# G3 review — M12 cleanup); already imported at module top (lines 15-16).

logger = logging.getLogger(__name__)


# Default arm set reflects R1a 18:3 hypothesis-dominant signal at Phase 0
# GO gate (plan §4.2 / memory [[project_r1a_attribution_distribution_2026_05_17]]).
# Override via ``settings.DIRECTION_BANDIT_ARMS`` for Phase 2+ experimentation.
DEFAULT_BANDIT_ARMS: tuple = (
    "rag_template",       # top RAG SUCCESS_PATTERN as template
    "knowledge_pattern",  # nearest KnowledgeEntry as base
    "llm_generation",     # free LLM generation (current default)
    "genetic_mutation",   # GA on top-3 sharpe recent alphas
)


# Below this segment-local pull count, ``select_arm`` samples from the global
# prior to avoid noisy decisions on fresh segments. Override via
# ``settings.DIRECTION_BANDIT_COLD_THRESHOLD``.
DEFAULT_COLD_THRESHOLD: int = 5


@dataclass
class DirectionArm:
    """Single Beta-Bernoulli arm; reward MUST be ∈ [0,1] (caller responsibility,
    ``update`` clips defensively per plan v1.3 MF-V1.3-5)."""

    name: str
    alpha: float = 1.0       # Beta α (successes + 1)
    beta: float = 1.0        # Beta β (failures + 1)
    total_pulls: int = 0
    total_reward: float = 0.0

    def sample(self) -> float:
        """Thompson sample — draw a value from Beta(α, β)."""
        # ``random.betavariate`` is stdlib (no numpy dep), fine for ~4 arms
        return random.betavariate(self.alpha, self.beta)

    def update(self, reward: float):
        """Update posterior after observing ``reward``. Defensive clip to [0,1]."""
        reward = max(0.0, min(1.0, float(reward)))
        self.alpha += reward
        self.beta += (1.0 - reward)
        self.total_pulls += 1
        self.total_reward += reward

    @property
    def mean_reward(self) -> float:
        if self.total_pulls == 0:
            return 0.5  # prior mean of Beta(1,1)
        return self.total_reward / self.total_pulls


def segment_id(ctx: Tuple[str, str, str]) -> str:
    """Stable JSONB key for a context tuple.

    String concat (NOT Python ``hash()``) — Python hash is non-deterministic
    across processes due to PYTHONHASHSEED randomization (MF-V1.2-4 lesson).
    Caller normalizes case before passing: region.upper(), category.lower().
    """
    return f"{ctx[0]}|{ctx[1]}|{ctx[2]}"


class ContextualDirectionBandit:
    """Per-task contextual Thompson Sampling with cold-start fallback.

    State shape:
      segments: { segment_id: { arm_name: DirectionArm } }
      global_arms: { arm_name: DirectionArm }  # shared across all segments

    When a segment has fewer than ``cold_threshold`` pulls, ``select_arm``
    samples the global prior. Once warm, segment-local prior takes over.
    ``update`` always writes to BOTH segment AND global so cold segments
    inherit accumulated wisdom (sf-V1.3-C known bias — global is dominated
    by hot segments; monitor per-segment arm distribution in Phase 2+).

    ``last_select`` caches the most recent ``select_arm`` ``(ctx, arm)``
    tuple so the next round's reward (computed AFTER the round runs) can
    update the right (segment, arm) without the caller having to thread it.
    """

    def __init__(
        self,
        arm_names: Optional[Iterable[str]] = None,
        cold_threshold: int = DEFAULT_COLD_THRESHOLD,
    ):
        self.arm_names: List[str] = list(arm_names) if arm_names else list(DEFAULT_BANDIT_ARMS)
        self.cold_threshold = cold_threshold
        self.segments: Dict[str, Dict[str, DirectionArm]] = {}
        self.global_arms: Dict[str, DirectionArm] = {
            n: DirectionArm(name=n) for n in self.arm_names
        }
        # (ctx_tuple, arm_name) — set by select_arm, consumed by update_last_round
        self.last_select: Optional[Tuple[Tuple[str, str, str], str]] = None

    def _get_or_init_segment(self, sid: str) -> Dict[str, DirectionArm]:
        if sid not in self.segments:
            self.segments[sid] = {n: DirectionArm(name=n) for n in self.arm_names}
        return self.segments[sid]

    def _segment_total_pulls(self, sid: str) -> int:
        seg = self.segments.get(sid)
        if not seg:
            return 0
        return sum(arm.total_pulls for arm in seg.values())

    def is_cold_at(self, ctx: Tuple[str, str, str]) -> bool:
        """True when select_arm at ``ctx`` would fall back to global prior."""
        return self._segment_total_pulls(segment_id(ctx)) < self.cold_threshold

    def select_arm(self, ctx: Tuple[str, str, str]) -> str:
        """Sample one arm via Thompson Sampling.

        Caches ``(ctx, arm)`` to ``self.last_select`` so the caller can later
        invoke ``update_last_round(reward)`` without re-passing ctx.
        """
        sid = segment_id(ctx)
        if self._segment_total_pulls(sid) < self.cold_threshold:
            source = self.global_arms
        else:
            source = self._get_or_init_segment(sid)
        samples = [(n, arm.sample()) for n, arm in source.items()]
        arm = max(samples, key=lambda x: x[1])[0]
        self.last_select = (ctx, arm)
        return arm

    def update(self, ctx: Tuple[str, str, str], arm_name: str, reward: float):
        """Update both segment-local and global prior for (ctx, arm).

        Forward-compat: silently skip if ``arm_name`` is no longer in
        ``self.arm_names`` (Phase 2+ arm rename invariance).
        """
        if arm_name not in self.arm_names:
            return
        sid = segment_id(ctx)
        seg = self._get_or_init_segment(sid)
        seg[arm_name].update(reward)
        self.global_arms[arm_name].update(reward)

    def update_last_round(self, reward: float) -> Optional[Tuple[Tuple[str, str, str], str]]:
        """Apply ``reward`` to the (ctx, arm) cached by the last ``select_arm``.

        Returns the (ctx, arm) tuple that was updated, or None if no prior
        select. Clears ``self.last_select`` so a double-update is a no-op.

        M12 (2026-05-18): when ``last_select`` is None we SKIP the update
        entirely — never attribute this round's reward to a stale/unknown
        arm. Logs a ``warning`` if non-trivial reward signal is being dropped
        so the operator can investigate (typically this means the previous
        round's ``select_arm`` was wiped after an exception in
        ``_bandit_update_and_select``).
        """
        if self.last_select is None:
            if reward and reward > 0:
                logger.warning(
                    "[Bandit] update_last_round called with reward=%.3f but "
                    "last_select=None — skipping update (no arm to credit). "
                    "This usually means the prior round's bandit cycle raised "
                    "and last_select was cleared on the exception path.",
                    reward,
                )
            return None
        ctx, arm = self.last_select
        self.update(ctx, arm, reward)
        self.last_select = None
        return (ctx, arm)

    def to_dict(self) -> Dict:
        """Serialize full state to JSONB-safe dict for ``task.config`` persistence."""
        return {
            "v": 1,
            "arm_names": list(self.arm_names),
            "cold_threshold": self.cold_threshold,
            "global_arms": {n: asdict(a) for n, a in self.global_arms.items()},
            "segments": {
                sid: {n: asdict(a) for n, a in seg.items()}
                for sid, seg in self.segments.items()
            },
            "last_select": (
                [list(self.last_select[0]), self.last_select[1]]
                if self.last_select is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "ContextualDirectionBandit":
        arm_names = d.get("arm_names") or list(DEFAULT_BANDIT_ARMS)
        cold = int(d.get("cold_threshold") or DEFAULT_COLD_THRESHOLD)
        b = cls(arm_names=arm_names, cold_threshold=cold)
        for name, arm_dict in (d.get("global_arms") or {}).items():
            if name in b.global_arms and isinstance(arm_dict, dict):
                for k, v in arm_dict.items():
                    if hasattr(b.global_arms[name], k):
                        setattr(b.global_arms[name], k, v)
        for sid, seg_dict in (d.get("segments") or {}).items():
            seg = b._get_or_init_segment(sid)
            for name, arm_dict in (seg_dict or {}).items():
                if name in seg and isinstance(arm_dict, dict):
                    for k, v in arm_dict.items():
                        if hasattr(seg[name], k):
                            setattr(seg[name], k, v)
        ls = d.get("last_select")
        if isinstance(ls, list) and len(ls) == 2 and isinstance(ls[0], list) and len(ls[0]) == 3:
            b.last_select = (tuple(ls[0]), ls[1])  # type: ignore[assignment]
        return b


async def build_context(task, db_factory=None) -> Tuple[str, str, str]:
    """Compute 3-dim segment context for ContextualDirectionBandit.

    Returns ``(region, dataset_category, recent_failure_pattern)`` — all str.

    - ``region``: ``MiningTask.region`` top-level Column (MF-V1.3-1 fix —
      NOT ``task.config.get("regions")`` which never existed). UPPER-cased.
    - ``dataset_category``: first ``target_datasets[0]``'s
      ``DatasetMetadata.category`` lowercased; ``"other"`` on no datasets /
      lookup miss / DB error.
    - ``recent_failure_pattern``: majority vote (≥2 of 3) over the last 3
      rounds' ``R1aAttributionLog.attribution`` values for this task;
      ``"unknown"`` on insufficient data or tie.

    Each DB read uses a FRESH ``AsyncSessionLocal()`` (default ``db_factory``)
    to avoid MVCC staleness from the caller's long-lived session — R1a writes
    commit independently from a separate session (evaluation.py v1.6) so the
    caller's snapshot may miss recently-inserted rows (MF-V1.3-4 fix).
    """
    # Lazy import to keep module import light + dodge potential cycles
    from backend.database import AsyncSessionLocal
    from backend.models.metadata import DatasetMetadata
    from backend.models.r1a_attribution import R1aAttributionLog
    from sqlalchemy import select

    df = db_factory or AsyncSessionLocal

    region = (getattr(task, "region", None) or "USA").upper()

    ds_ids = getattr(task, "target_datasets", None) or []
    dataset_category = "other"
    if ds_ids:
        try:
            async with df() as s:
                row = (await s.execute(
                    select(DatasetMetadata.category)
                    .where(DatasetMetadata.dataset_id == str(ds_ids[0]))
                    .limit(1)
                )).scalar_one_or_none()
                if row:
                    dataset_category = str(row).lower()
        except Exception:
            # Fail-soft: leave "other" — bandit still works, just less granular
            pass

    failure_pattern = "unknown"
    task_id = getattr(task, "id", None)
    if task_id is not None:
        try:
            async with df() as s:
                recent = (await s.execute(
                    select(R1aAttributionLog.attribution)
                    .where(R1aAttributionLog.task_id == task_id)
                    .order_by(R1aAttributionLog.created_at.desc())
                    .limit(3)
                )).scalars().all()
            if len(recent) >= 3:
                # R1aAttributionLog writes lowercase strings; defend just in case.
                c = Counter((a or "unknown").lower() for a in recent)
                top, count = c.most_common(1)[0]
                if count >= 2:
                    failure_pattern = top
        except Exception:
            pass

    return (region, dataset_category, failure_pattern)


def compute_arm_reward(round_alphas: list) -> float:
    """5-dim weighted reward ∈ [0,1] for Beta-Bernoulli ``DirectionArm.update``.

    Weights per plan §1.6:
      sharpe (+0.30), fitness (+0.20), -turnover (-0.15), -self_corr (-0.20),
      composite_score (+0.35)

    MUST be called on the in-memory ``updated_alphas`` list (NOT a DB query) —
    most alphas are FAIL/OPTIMIZE and never INSERT, so DB query misses 95%
    of signal (R1a v1.6 lesson, plan §1.6 MF-A).

    Empty round → 0.0 (no signal, default Beta prior takes over).
    """
    if not round_alphas:
        return 0.0
    rewards = []
    for a in round_alphas:
        m = getattr(a, "metrics", None) or {}
        sharpe = max(0.0, float(m.get("sharpe") or 0))
        fitness = max(0.0, float(m.get("fitness") or 0))
        turnover = float(m.get("turnover") or 0)
        self_corr = float(m.get("_self_corr") or 0)
        cs = float(m.get("composite_score") or 0)
        r = 0.30 * sharpe + 0.20 * fitness - 0.15 * turnover - 0.20 * self_corr + 0.35 * cs
        # Defensive [0,1] clip per plan v1.3 MF-V1.3-5 (Beta-Bernoulli contract)
        rewards.append(max(0.0, min(1.0, r)))
    return sum(rewards) / len(rewards)
