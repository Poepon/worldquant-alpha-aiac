"""
Experiment trace with DAG structure based on RD-Agent architecture.

Key concepts:
- ExperimentTrace: Complete history with parent-child relationships
- TraceNode: Single node in the trace graph
- Support for branching explorations
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.agents.core.experiment import (
    AlphaExperiment,
    EvoStep,
    Hypothesis,
)
from backend.agents.core.feedback import HypothesisFeedback
from backend.agents.core.knowledge import EvolvingKnowledge, QueriedKnowledge


@dataclass
class TraceNode:
    """A single node in the experiment trace."""
    experiment: AlphaExperiment
    feedback: Optional[HypothesisFeedback] = None
    queried_knowledge: Optional[QueriedKnowledge] = None


class ExperimentTrace:
    """
    DAG-structured experiment trace based on RD-Agent's Trace class.
    
    Features:
    - Support for branching (multiple exploration directions)
    - Parent-child relationships for lineage tracking
    - SOTA (State of the Art) tracking
    - Knowledge base integration
    
    Usage:
        trace = ExperimentTrace(scenario_context)
        trace.add_experiment(experiment, feedback, parent_idx=None)  # New root
        trace.add_experiment(experiment2, feedback2, parent_idx=0)   # Child of first
    """
    
    NodeType = Tuple[AlphaExperiment, Optional[HypothesisFeedback]]
    NEW_ROOT: Tuple = ()
    
    def __init__(
        self,
        dataset_id: str = "",
        region: str = "",
        universe: str = "",
        knowledge_base: Optional[EvolvingKnowledge] = None
    ):
        """
        Initialize trace.
        
        Args:
            dataset_id: Dataset being mined
            region: Region context
            universe: Universe context
            knowledge_base: Evolving knowledge base
        """
        # Context
        self.dataset_id = dataset_id
        self.region = region
        self.universe = universe
        
        # History as list of (Experiment, Feedback) tuples
        self.hist: List[ExperimentTrace.NodeType] = []
        
        # DAG structure: dag_parent[i] = tuple of parent indices for hist[i]
        # () = root node, (1,) = single parent at index 1, (1, 2) = multiple parents
        self.dag_parent: List[Tuple[int, ...]] = []
        
        # Knowledge base
        self.knowledge_base = knowledge_base or EvolvingKnowledge()
        
        # Current selection (for iterative exploration)
        self.current_selection: Tuple[int, ...] = (-1,)  # -1 means "last"
        
        # Timestamps
        self.created_at = datetime.utcnow()
        self.last_updated = datetime.utcnow()
    
    def __len__(self) -> int:
        return len(self.hist)
    
    def add_experiment(
        self,
        experiment: AlphaExperiment,
        feedback: Optional[HypothesisFeedback] = None,
        queried_knowledge: Optional[QueriedKnowledge] = None,
        parent_idx: Optional[int] = None
    ) -> int:
        """
        Add experiment to trace.
        
        Args:
            experiment: The experiment to add
            feedback: Feedback for the experiment
            queried_knowledge: Knowledge that was queried before generation
            parent_idx: Index of parent experiment (None for new root)
            
        Returns:
            Index of the added experiment
        """
        self.hist.append((experiment, feedback))
        
        # Set parent
        if parent_idx is None:
            self.dag_parent.append(self.NEW_ROOT)
        else:
            self.dag_parent.append((parent_idx,))
        
        # Extract knowledge from feedback
        if feedback and feedback.knowledge_extracted:
            self.knowledge_base.extract_rules_from_feedback(
                experiment_id=experiment.id,
                hypothesis_text=experiment.hypothesis.statement if experiment.hypothesis else "",
                was_success=feedback.decision,
                knowledge_extracted=feedback.knowledge_extracted,
                dataset_id=self.dataset_id,
                region=self.region
            )
        
        self.last_updated = datetime.utcnow()
        return len(self.hist) - 1
    
    def update_feedback(self, idx: int, feedback: HypothesisFeedback):
        """Update feedback for an existing experiment."""
        if 0 <= idx < len(self.hist):
            experiment, _ = self.hist[idx]
            self.hist[idx] = (experiment, feedback)
            
            # Extract knowledge
            if feedback.knowledge_extracted:
                self.knowledge_base.extract_rules_from_feedback(
                    experiment_id=experiment.id,
                    hypothesis_text=experiment.hypothesis.statement if experiment.hypothesis else "",
                    was_success=feedback.decision,
                    knowledge_extracted=feedback.knowledge_extracted,
                    dataset_id=self.dataset_id,
                    region=self.region
                )
    
    def get_sota(self) -> Optional[NodeType]:
        """
        Get State of the Art (best successful experiment).
        
        Returns the most recent successful experiment.
        """
        for experiment, feedback in reversed(self.hist):
            if feedback and feedback.decision:
                return (experiment, feedback)
        return None
    
    def get_sota_hypothesis_and_experiment(self) -> Tuple[Optional[Hypothesis], Optional[AlphaExperiment]]:
        """Get SOTA hypothesis and experiment."""
        result = self.get_sota()
        if result:
            experiment, _ = result
            return experiment.hypothesis, experiment
        return None, None
    
    def is_root(self, idx: int) -> bool:
        """Check if experiment at idx is a root (new exploration)."""
        if idx < 0 or idx >= len(self.dag_parent):
            return True
        return len(self.dag_parent[idx]) == 0
    
    def get_parents(self, idx: int) -> List[int]:
        """Get all ancestor indices for an experiment."""
        if idx < 0 or idx >= len(self.dag_parent):
            return []
        
        if self.is_root(idx):
            return [idx]
        
        ancestors = []
        curr = idx
        
        while True:
            ancestors.insert(0, curr)
            parent_tuple = self.dag_parent[curr]
            
            if not parent_tuple or parent_tuple[0] == curr:
                break
            
            curr = parent_tuple[0]
        
        return ancestors
    
    def get_lineage(self, idx: int) -> List[NodeType]:
        """Get experiment lineage (root to idx)."""
        parent_indices = self.get_parents(idx)
        return [self.hist[i] for i in parent_indices]
    
    def get_children(self, idx: int) -> List[int]:
        """Get indices of child experiments."""
        children = []
        for i, parents in enumerate(self.dag_parent):
            if idx in parents:
                children.append(i)
        return children
    
    def get_experiments_for_hypothesis(
        self,
        hypothesis_text: str,
        threshold: float = 0.7
    ) -> List[NodeType]:
        """
        Get all experiments testing similar hypothesis.
        
        Useful for determining if a hypothesis should be abandoned.
        """
        from difflib import SequenceMatcher
        
        matches = []
        for experiment, feedback in self.hist:
            if experiment.hypothesis:
                similarity = SequenceMatcher(
                    None,
                    hypothesis_text.lower(),
                    experiment.hypothesis.statement.lower()
                ).ratio()
                
                if similarity >= threshold:
                    matches.append((experiment, feedback))
        
        return matches
    
    def should_abandon_hypothesis(
        self,
        hypothesis_text: str,
        max_failures: int = 3
    ) -> Tuple[bool, str]:
        """
        Determine if a hypothesis should be abandoned.
        
        Args:
            hypothesis_text: The hypothesis being considered
            max_failures: Maximum allowed failures
            
        Returns:
            (should_abandon, reason)
        """
        similar_experiments = self.get_experiments_for_hypothesis(hypothesis_text)
        
        if not similar_experiments:
            return False, "No prior experiments for this hypothesis"
        
        # Count failures
        failures = sum(
            1 for exp, fb in similar_experiments
            if fb is not None and not fb.decision and fb.attribution.value == "hypothesis"
        )
        
        if failures >= max_failures:
            return True, f"Hypothesis has failed {failures} times (attributed to hypothesis, not implementation)"
        
        return False, f"Hypothesis has {failures}/{max_failures} hypothesis-attributed failures"
    
    def get_recent_experiments(self, n: int = 10) -> List[NodeType]:
        """Get n most recent experiments."""
        return self.hist[-n:] if len(self.hist) >= n else self.hist
    
    def get_successful_experiments(self) -> List[NodeType]:
        """Get all successful experiments."""
        return [
            (exp, fb) for exp, fb in self.hist
            if fb is not None and fb.decision
        ]
    
    def get_failed_experiments(self) -> List[NodeType]:
        """Get all failed experiments."""
        return [
            (exp, fb) for exp, fb in self.hist
            if fb is not None and not fb.decision
        ]
    
    def query_knowledge(
        self,
        hypothesis: Optional[Hypothesis] = None,
        min_confidence: float = 0.3
    ) -> QueriedKnowledge:
        """
        Query knowledge base for experiment generation.
        
        Args:
            hypothesis: Optional hypothesis to query for
            min_confidence: Minimum confidence threshold
        """
        return self.knowledge_base.query(
            dataset_id=self.dataset_id,
            region=self.region,
            min_confidence=min_confidence
        )
    
    def to_prompt_context(self, max_experiments: int = 5) -> str:
        """
        Format trace for inclusion in prompts.
        
        Provides recent experiment history for LLM context.
        """
        recent = self.get_recent_experiments(max_experiments)
        
        if not recent:
            return "No previous experiments in this trace."
        
        lines = ["## Recent Experiment History"]
        
        for i, (experiment, feedback) in enumerate(recent, 1):
            hypothesis_text = experiment.hypothesis.statement if experiment.hypothesis else "N/A"
            result = "Success" if feedback and feedback.decision else "Failed"
            
            lines.append(f"""
### Experiment {i}
**Hypothesis**: {hypothesis_text[:100]}
**Expression**: `{experiment.expression[:80]}...`
**Result**: {result}
**Sharpe**: {experiment.get_sharpe()}, **Fitness**: {experiment.get_fitness()}
""")
            
            if feedback:
                lines.append(f"**Observation**: {feedback.observations[:100]}")
                if feedback.new_hypothesis:
                    lines.append(f"**Suggested Next**: {feedback.new_hypothesis[:100]}")
        
        # Add SOTA info
        sota = self.get_sota()
        if sota:
            exp, _ = sota
            lines.append(f"""
### Current Best (SOTA)
**Expression**: `{exp.expression[:80]}`
**Sharpe**: {exp.get_sharpe()}, **Fitness**: {exp.get_fitness()}
""")
        
        return "\n".join(lines)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get trace statistics."""
        successful = self.get_successful_experiments()
        failed = self.get_failed_experiments()

        return {
            "total_experiments": len(self.hist),
            "successful": len(successful),
            "failed": len(failed),
            "success_rate": len(successful) / len(self.hist) if self.hist else 0,
            "root_count": sum(1 for p in self.dag_parent if len(p) == 0),
            "max_depth": max(len(self.get_parents(i)) for i in range(len(self.hist))) if self.hist else 0,
            "knowledge_rules": len(self.knowledge_base.rules),
        }

    # === Phase 2 R6 adapter (plan v1.0 §3, 2026-05-18) ===
    # Minimal R6-v1 adapter for ExperimentRun.runtime_state["dag"] JSONB.
    # The runtime DAG state lives in `backend/agents/graph/dag_state.py` as
    # pure-function helpers — this class stays DORMANT in production except
    # for these two adapter methods. R6-v1 does NOT instantiate
    # ExperimentTrace itself; the JSONB dict and helpers are the production
    # interface. Adapter exists for future R6-v2 / pipeline.py integration.

    def to_runtime_state_dict(self) -> Dict[str, Any]:
        """Serialize trace's `current_selection` + DAG-relevant fields to the
        JSONB-friendly shape expected by `runtime_state["dag"]`. R6-v1 callers
        should prefer `dag_state.init_dag()` / `add_node()` directly; this is
        for forward-compat with R6-v2 when the in-memory ExperimentTrace
        becomes the source of truth.
        """
        return {
            "v": 1,
            "in_memory_only": True,
            "current_selection_tuple": list(self.current_selection),
            "hist_length": len(self.hist),
            "stats": self.get_stats(),
        }

    @classmethod
    def from_runtime_state_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["ExperimentTrace"]:
        """Inverse of to_runtime_state_dict — currently returns None in R6-v1
        because reconstructing AlphaExperiment/Hypothesis objects from JSONB
        requires the full Pipeline. R6-v2 may flesh this out. Callers should
        not depend on a non-None return.
        """
        return None
