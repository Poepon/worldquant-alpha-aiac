"""Attribution types — pure leaf module (extracted from agents/core/feedback.py).

Holds ``AttributionType`` (hypothesis-vs-implementation failure enum) and
``HypothesisFeedback`` (structured feedback dataclass). Extracted OUT of
``backend/agents/core/`` (Phase 1a of the four-pool decoupling) so that when the
dead RD-Agent ``core/`` cluster is deleted in Phase 1c, these two types are NOT
stranded — any surviving consumer (e.g. a slim R1a logging path) imports them
from here.

NOTE (necessary-but-not-sufficient): this move alone does NOT make ``core/``
deletable. ``core/integration.py`` still module-imports the whole dead cluster,
and the LIVE R1a call site (``evaluation.py:3176`` →
``core.integration.enhance_existing_node_evaluate``, gated by ``ENABLE_R1A_HOOK``,
ON in prod) drags it in at runtime. ``core/`` becomes deletable only when that
call site (+ the R1a block) is removed in the SAME Phase 1c-delete commit, AFTER
Phase 1b-flip turns ``ENABLE_R1A_HOOK`` OFF — see plan §4. This extraction's job
is purely to give the two types a non-core home before that happens.

PURE LEAF: stdlib only (enum / dataclass / datetime / typing). It must NEVER
import core.pipeline / core.trace / core.scenario / core.experiment (the dead
cluster). ``agents/core/feedback.py`` now re-exports these names so existing
``from backend.agents.core.feedback import ...`` / ``from backend.agents.core
import ...`` call sites stay byte-identical until Phase 1c.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class AttributionType(Enum):
    """Attribution of experiment outcome."""
    HYPOTHESIS = "hypothesis"  # The idea itself is wrong
    IMPLEMENTATION = "implementation"  # Right idea, wrong execution
    BOTH = "both"  # Both have issues
    UNKNOWN = "unknown"  # Need more evidence


@dataclass
class HypothesisFeedback:
    """
    Structured feedback for hypothesis evaluation.

    Based on RD-Agent's HypothesisFeedback, adapted for alpha mining.

    This provides:
    1. Observations - what actually happened
    2. Hypothesis evaluation - was the hypothesis validated?
    3. Attribution - hypothesis vs implementation failure
    4. New hypothesis - suggested next direction
    5. Knowledge - extractable rules
    """

    # Observation (factual)
    observations: str  # What was observed in the experiment

    # Hypothesis evaluation
    hypothesis_evaluation: str  # Was hypothesis supported/refuted?
    hypothesis_supported: Optional[bool] = None  # True/False/None(unknown)

    # Attribution
    attribution: AttributionType = AttributionType.UNKNOWN
    attribution_confidence: float = 0.5
    attribution_evidence: List[str] = field(default_factory=list)

    # Decision
    decision: bool = False  # Overall success/failure
    reason: str = ""  # Explanation

    # Recommendations
    should_continue_direction: bool = True
    should_retry_implementation: bool = False
    should_modify_hypothesis: bool = False
    should_abandon: bool = False

    # New hypothesis suggestion
    new_hypothesis: Optional[str] = None
    new_hypothesis_rationale: str = ""

    # Knowledge extraction
    knowledge_extracted: List[str] = field(default_factory=list)  # "If..., then..." rules
    knowledge_confidence: float = 0.5

    # Should NOT conclude (important for avoiding wrong lessons)
    invalid_conclusions: List[str] = field(default_factory=list)

    # Code/expression change summary
    code_change_summary: Optional[str] = None

    # Exception (if experiment failed to run)
    exception: Optional[str] = None

    # Timestamps
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __bool__(self) -> bool:
        """Feedback is truthy if experiment was successful."""
        return self.decision

    def __str__(self) -> str:
        return f"""Decision: {self.decision}
Reason: {self.reason}
Observations: {self.observations}
Hypothesis Evaluation: {self.hypothesis_evaluation}
New Hypothesis: {self.new_hypothesis}"""

    def is_hypothesis_failure(self) -> bool:
        """Check if this was primarily a hypothesis failure."""
        return self.attribution == AttributionType.HYPOTHESIS

    def is_implementation_failure(self) -> bool:
        """Check if this was primarily an implementation failure."""
        return self.attribution == AttributionType.IMPLEMENTATION

    def get_confident_knowledge(self) -> List[str]:
        """Get knowledge rules with high confidence."""
        if self.knowledge_confidence >= 0.7:
            return self.knowledge_extracted
        return []

    def get_tentative_knowledge(self) -> List[str]:
        """Get knowledge rules with moderate confidence."""
        if 0.4 <= self.knowledge_confidence < 0.7:
            return self.knowledge_extracted
        return []

    def should_record_to_knowledge_base(self) -> bool:
        """
        Check if this feedback should be recorded to knowledge base.

        Implementation failures should NOT be recorded as hypothesis failures.
        """
        return self.attribution != AttributionType.IMPLEMENTATION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "observations": self.observations,
            "hypothesis_evaluation": self.hypothesis_evaluation,
            "hypothesis_supported": self.hypothesis_supported,
            "attribution": self.attribution.value,
            "attribution_confidence": self.attribution_confidence,
            "decision": self.decision,
            "reason": self.reason,
            "should_continue_direction": self.should_continue_direction,
            "should_retry_implementation": self.should_retry_implementation,
            "new_hypothesis": self.new_hypothesis,
            "new_hypothesis_rationale": self.new_hypothesis_rationale,
            "knowledge_extracted": self.knowledge_extracted,
            "knowledge_confidence": self.knowledge_confidence,
            "invalid_conclusions": self.invalid_conclusions,
        }

    @classmethod
    def from_exception(cls, e: Exception) -> 'HypothesisFeedback':
        """Create feedback from an exception."""
        return cls(
            observations=f"Experiment failed with error: {str(e)}",
            hypothesis_evaluation="Cannot evaluate - experiment failed to run",
            decision=False,
            reason=f"Experiment failed due to: {str(e)}",
            exception=str(e),
            attribution=AttributionType.IMPLEMENTATION,
            should_retry_implementation=True,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'HypothesisFeedback':
        """Create from dictionary (e.g., LLM response)."""
        attribution_raw = data.get("attribution", {})
        if isinstance(attribution_raw, dict):
            attribution_str = attribution_raw.get("primary_cause", "unknown")
            attr_confidence = attribution_raw.get("confidence", 0.5)
        elif isinstance(attribution_raw, str):
            attribution_str = attribution_raw
            attr_confidence = data.get("attribution_confidence", 0.5)
        else:
            attribution_str = "unknown"
            attr_confidence = 0.5

        try:
            attribution = AttributionType(attribution_str.lower())
        except ValueError:
            attribution = AttributionType.UNKNOWN

        # Handle decision field
        decision_raw = data.get("decision", False)
        if isinstance(decision_raw, dict):
            decision = decision_raw.get("success", False)
            reason = data.get("reason", decision_raw.get("reasoning", ""))
            should_retry = decision_raw.get("should_retry_implementation", False)
        else:
            decision = decision_raw
            reason = data.get("reason", "")
            should_retry = data.get("should_retry_implementation", False)

        # Handle new_hypothesis field
        new_hypo_raw = data.get("new_hypothesis")
        if isinstance(new_hypo_raw, dict):
            new_hypothesis = new_hypo_raw.get("statement")
            new_hypothesis_rationale = new_hypo_raw.get("rationale", "")
        else:
            new_hypothesis = new_hypo_raw
            new_hypothesis_rationale = ""

        # Handle knowledge_extraction field
        knowledge_raw = data.get("knowledge_extraction", {})
        if isinstance(knowledge_raw, dict):
            knowledge_extracted = knowledge_raw.get("confident_knowledge", [])
            invalid_conclusions = knowledge_raw.get("should_not_conclude", [])
        else:
            knowledge_extracted = data.get("knowledge_extracted", [])
            invalid_conclusions = []

        return cls(
            observations=data.get("observation", data.get("observations", "")),
            hypothesis_evaluation=data.get("hypothesis_evaluation", ""),
            hypothesis_supported=data.get("hypothesis_supported"),
            attribution=attribution,
            attribution_confidence=attr_confidence,
            decision=decision,
            reason=reason,
            should_continue_direction=data.get("should_continue_direction", True),
            should_retry_implementation=should_retry,
            new_hypothesis=new_hypothesis,
            new_hypothesis_rationale=new_hypothesis_rationale,
            knowledge_extracted=knowledge_extracted,
            invalid_conclusions=invalid_conclusions,
        )
