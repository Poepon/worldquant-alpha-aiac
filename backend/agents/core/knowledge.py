"""
Knowledge management based on RD-Agent architecture.

Key concepts:
- KnowledgeRule: "If..., then..." transferable rule
- QueriedKnowledge: Knowledge retrieved for experiment generation
- EvolvingKnowledge: Knowledge that grows from experiments
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class KnowledgeType(Enum):
    """Type of knowledge.

    V-26.38 (2026-05-13) deprecation notice:
      FIELD_INSIGHT / OPERATOR_INSIGHT / DATASET_INSIGHT and the
      sibling HYPOTHESIS_INSIGHT (declared in the persistence layer)
      have NO retrieve path. The write path is gated behind
      settings.WRITE_FIELD_HYPOTHESIS_INSIGHTS=False since V-24.E.
      Decision (retire vs build retrieve vs defer) due 2026-Q3 —
      see `docs/v26_38_39_field_insight_deprecation.md`. Do NOT add
      new write call sites for these types without revisiting that
      decision.
    """
    SUCCESS_PATTERN = "success_pattern"  # What worked
    FAILURE_PATTERN = "failure_pattern"  # What didn't work
    OPTIMIZATION_RULE = "optimization_rule"  # How to improve
    FIELD_INSIGHT = "field_insight"      # DEPRECATED (V-26.38); no retrieve path
    OPERATOR_INSIGHT = "operator_insight"  # DEPRECATED (V-26.38); no retrieve path
    DATASET_INSIGHT = "dataset_insight"  # DEPRECATED (V-26.38); no retrieve path


@dataclass
class KnowledgeRule:
    """
    A transferable knowledge rule in "If..., then..." format.
    
    Based on RD-Agent's knowledge transfer mechanism.
    These rules are:
    - Extracted from experiments
    - Queryable for new experiments
    - Evolving with new evidence
    """
    
    # The rule
    condition: str  # "If [this condition is observed]"
    conclusion: str  # "then [this outcome/recommendation]"
    
    # Type and context
    knowledge_type: KnowledgeType = KnowledgeType.SUCCESS_PATTERN
    dataset_id: Optional[str] = None  # Specific to dataset, or None for general
    region: Optional[str] = None
    
    # Confidence and evidence
    confidence: float = 0.5  # 0.0 - 1.0
    evidence_count: int = 1  # How many experiments support this
    
    # Source experiments
    source_experiment_ids: List[str] = field(default_factory=list)
    
    # Timestamps
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_updated: datetime = field(default_factory=datetime.utcnow)
    
    def __str__(self) -> str:
        return f"If {self.condition}, then {self.conclusion}"
    
    def to_prompt_text(self) -> str:
        """Format for inclusion in prompts."""
        confidence_text = "high" if self.confidence >= 0.7 else "medium" if self.confidence >= 0.4 else "low"
        return f"- {self} (confidence: {confidence_text})"
    
    def update_with_evidence(self, supports: bool):
        """Update confidence based on new evidence."""
        self.evidence_count += 1
        
        # Bayesian-like update
        if supports:
            self.confidence = min(0.95, self.confidence + (1 - self.confidence) * 0.1)
        else:
            self.confidence = max(0.05, self.confidence - self.confidence * 0.15)
        
        self.last_updated = datetime.utcnow()
    
    def matches_context(
        self,
        dataset_id: Optional[str] = None,
        region: Optional[str] = None,
        knowledge_types: Optional[List[KnowledgeType]] = None
    ) -> bool:
        """Check if rule matches query context."""
        # Type filter
        if knowledge_types and self.knowledge_type not in knowledge_types:
            return False
        
        # Dataset filter (None matches all, or specific match)
        if self.dataset_id is not None and dataset_id is not None:
            if self.dataset_id != dataset_id:
                return False
        
        # Region filter
        if self.region is not None and region is not None:
            if self.region != region:
                return False
        
        return True
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "condition": self.condition,
            "conclusion": self.conclusion,
            "knowledge_type": self.knowledge_type.value,
            "dataset_id": self.dataset_id,
            "region": self.region,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class QueriedKnowledge:
    """
    Knowledge retrieved for experiment generation.
    
    When generating a new experiment, we query the knowledge base for:
    - Success patterns to emulate
    - Failure patterns to avoid
    - Optimization rules for improvement
    """
    
    # Retrieved rules by type
    success_patterns: List[KnowledgeRule] = field(default_factory=list)
    failure_patterns: List[KnowledgeRule] = field(default_factory=list)
    optimization_rules: List[KnowledgeRule] = field(default_factory=list)
    
    # Similar past experiments
    similar_successes: List[str] = field(default_factory=list)  # Experiment IDs
    similar_failures: List[str] = field(default_factory=list)
    
    # Query context
    query_dataset_id: Optional[str] = None
    query_region: Optional[str] = None
    query_hypothesis: Optional[str] = None
    
    # Timestamps
    queried_at: datetime = field(default_factory=datetime.utcnow)
    
    def has_relevant_knowledge(self) -> bool:
        """Check if any relevant knowledge was found."""
        return bool(
            self.success_patterns or 
            self.failure_patterns or 
            self.optimization_rules
        )
    
    def to_prompt_context(self) -> str:
        """Format knowledge for inclusion in prompts."""
        sections = []
        
        if self.success_patterns:
            patterns_text = "\n".join(r.to_prompt_text() for r in self.success_patterns[:5])
            sections.append(f"**Patterns that have worked** (for reference):\n{patterns_text}")
        
        if self.failure_patterns:
            patterns_text = "\n".join(r.to_prompt_text() for r in self.failure_patterns[:5])
            sections.append(f"**Patterns to be cautious about**:\n{patterns_text}")
        
        if self.optimization_rules:
            rules_text = "\n".join(r.to_prompt_text() for r in self.optimization_rules[:3])
            sections.append(f"**Optimization insights**:\n{rules_text}")
        
        return "\n\n".join(sections) if sections else "No specific knowledge available."


@dataclass
class EvolvingKnowledge:
    """
    Knowledge base that evolves from experiments.
    
    Based on RD-Agent's EvolvingKnowledgeBase concept.
    
    Features:
    - Automatic rule extraction from experiments
    - Confidence updating with new evidence
    - Query by context
    """
    
    # All rules
    rules: List[KnowledgeRule] = field(default_factory=list)
    
    # Indexing for fast lookup
    _by_dataset: Dict[str, List[int]] = field(default_factory=dict)
    _by_type: Dict[KnowledgeType, List[int]] = field(default_factory=dict)
    
    def add_rule(self, rule: KnowledgeRule):
        """Add a new rule to the knowledge base."""
        # Check for duplicate
        for existing in self.rules:
            if existing.condition == rule.condition and existing.conclusion == rule.conclusion:
                # Update existing instead
                existing.update_with_evidence(True)
                return
        
        # Add new
        idx = len(self.rules)
        self.rules.append(rule)
        
        # Update indexes
        if rule.dataset_id:
            if rule.dataset_id not in self._by_dataset:
                self._by_dataset[rule.dataset_id] = []
            self._by_dataset[rule.dataset_id].append(idx)
        
        if rule.knowledge_type not in self._by_type:
            self._by_type[rule.knowledge_type] = []
        self._by_type[rule.knowledge_type].append(idx)
    
    def query(
        self,
        dataset_id: Optional[str] = None,
        region: Optional[str] = None,
        knowledge_types: Optional[List[KnowledgeType]] = None,
        min_confidence: float = 0.3,
        max_results: int = 10
    ) -> QueriedKnowledge:
        """
        Query knowledge base for relevant rules.
        
        Args:
            dataset_id: Filter by dataset
            region: Filter by region
            knowledge_types: Filter by type
            min_confidence: Minimum confidence threshold
            max_results: Maximum rules per type
        """
        result = QueriedKnowledge(
            query_dataset_id=dataset_id,
            query_region=region
        )
        
        for rule in self.rules:
            if rule.confidence < min_confidence:
                continue
            
            if not rule.matches_context(dataset_id, region, knowledge_types):
                continue
            
            # Categorize by type
            if rule.knowledge_type == KnowledgeType.SUCCESS_PATTERN:
                if len(result.success_patterns) < max_results:
                    result.success_patterns.append(rule)
            elif rule.knowledge_type == KnowledgeType.FAILURE_PATTERN:
                if len(result.failure_patterns) < max_results:
                    result.failure_patterns.append(rule)
            elif rule.knowledge_type == KnowledgeType.OPTIMIZATION_RULE:
                if len(result.optimization_rules) < max_results:
                    result.optimization_rules.append(rule)
        
        # Sort by confidence
        result.success_patterns.sort(key=lambda r: r.confidence, reverse=True)
        result.failure_patterns.sort(key=lambda r: r.confidence, reverse=True)
        result.optimization_rules.sort(key=lambda r: r.confidence, reverse=True)
        
        return result
    
    def extract_rules_from_feedback(
        self,
        experiment_id: str,
        hypothesis_text: str,
        was_success: bool,
        knowledge_extracted: List[str],
        dataset_id: Optional[str] = None,
        region: Optional[str] = None
    ) -> List[KnowledgeRule]:
        """
        Extract knowledge rules from experiment feedback.
        
        Parses "If..., then..." strings and creates KnowledgeRule objects.
        """
        new_rules = []
        
        for rule_text in knowledge_extracted:
            # Parse "If..., then..." format
            rule_lower = rule_text.lower()
            
            if "if " in rule_lower and " then " in rule_lower:
                parts = rule_text.split(" then ", 1)
                if len(parts) == 2:
                    condition = parts[0].replace("If ", "").replace("if ", "").strip()
                    conclusion = parts[1].strip()
                    
                    rule = KnowledgeRule(
                        condition=condition,
                        conclusion=conclusion,
                        knowledge_type=KnowledgeType.SUCCESS_PATTERN if was_success else KnowledgeType.FAILURE_PATTERN,
                        dataset_id=dataset_id,
                        region=region,
                        confidence=0.5 if was_success else 0.4,
                        source_experiment_ids=[experiment_id]
                    )
                    
                    self.add_rule(rule)
                    new_rules.append(rule)
        
        return new_rules
    
    def get_stats(self) -> Dict[str, Any]:
        """Get knowledge base statistics."""
        return {
            "total_rules": len(self.rules),
            "by_type": {
                kt.value: len(indices) 
                for kt, indices in self._by_type.items()
            },
            "by_dataset": {
                ds: len(indices) 
                for ds, indices in self._by_dataset.items()
            },
            "avg_confidence": sum(r.confidence for r in self.rules) / len(self.rules) if self.rules else 0,
        }
