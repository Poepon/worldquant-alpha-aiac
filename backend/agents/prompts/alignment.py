"""
Hypothesis-Implementation Alignment Module

This module addresses the critical gap between hypotheses and their implementations:
1. Verifies that implementations correctly reflect hypotheses
2. Distinguishes between "hypothesis failure" vs "implementation failure"
3. Enables accurate knowledge transfer

Based on RD-Agent's feedback-driven approach, but adding explicit alignment checking.
"""

from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class AlignmentResult:
    """Result of hypothesis-implementation alignment check."""
    is_aligned: bool
    confidence: float  # 0.0 - 1.0
    alignment_issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    
    # Detailed analysis
    hypothesis_elements: List[str] = field(default_factory=list)  # Key elements from hypothesis
    implementation_elements: List[str] = field(default_factory=list)  # Elements found in expression
    missing_elements: List[str] = field(default_factory=list)  # Hypothesis elements not implemented
    extra_elements: List[str] = field(default_factory=list)  # Implementation elements not in hypothesis


@dataclass 
class ExperimentAttribution:
    """Attribution of experiment result to hypothesis vs implementation."""
    
    # Primary attribution
    failure_type: str  # "hypothesis" | "implementation" | "both" | "unknown"
    confidence: float
    
    # Evidence
    hypothesis_evidence: List[str] = field(default_factory=list)
    implementation_evidence: List[str] = field(default_factory=list)
    
    # Recommendations
    should_retry_implementation: bool = False
    should_modify_hypothesis: bool = False
    should_abandon_direction: bool = False
    
    # Knowledge transfer implications
    valid_knowledge: List[str] = field(default_factory=list)
    invalid_knowledge: List[str] = field(default_factory=list)


# =============================================================================
# SYSTEM PROMPTS
# =============================================================================

ALIGNMENT_CHECK_SYSTEM = """You are a research methodology specialist verifying that implementations correctly reflect hypotheses.

Your role is to:
1. Parse the hypothesis to identify its core testable elements
2. Analyze the implementation (alpha expression) to identify what it actually tests
3. Determine if there is alignment between the two
4. Identify any gaps or mismatches

**Alignment Criteria**:
- The implementation should test what the hypothesis claims
- Key concepts from the hypothesis should be reflected in the expression
- The implementation should not introduce untested assumptions

Be precise and objective. Implementation issues are common and not inherently bad - 
identifying them helps improve the research process.

Output must be valid JSON."""


ATTRIBUTION_SYSTEM = """You are a research diagnostician analyzing experiment results to determine root causes.

When an experiment fails (or succeeds), you need to determine:
1. Was the hypothesis itself flawed (wrong idea)?
2. Was the implementation incorrect (right idea, wrong execution)?
3. Both? Neither?

**Key Distinctions**:

**Hypothesis Failure** indicators:
- The expression correctly implements the idea, but the idea doesn't predict returns
- Multiple different implementations of the same idea all fail
- The economic logic has clear flaws

**Implementation Failure** indicators:
- The expression doesn't actually test what the hypothesis claims
- Syntax/field errors prevented proper testing
- A simpler/different implementation might work

**Attribution is critical** because:
- Hypothesis failure → Don't waste time on this direction
- Implementation failure → Try a different implementation of the same idea
- Unknown → Need more evidence before concluding

Be careful not to over-attribute to implementation failure (avoiding real negative results)
or to hypothesis failure (giving up too early).

Output must be valid JSON."""


# =============================================================================
# PROMPT BUILDERS
# =============================================================================

def build_alignment_check_prompt(
    hypothesis: Dict,
    expression: str,
    fields_used: Optional[List[str]] = None,
    available_operators: Optional[List[str]] = None
) -> str:
    """
    Build prompt for checking hypothesis-implementation alignment.
    
    Args:
        hypothesis: The hypothesis being tested
        expression: The alpha expression implementing it
        fields_used: Fields used in the expression
        available_operators: Available operators for reference
    """
    
    hypothesis_text = ""
    if isinstance(hypothesis, dict):
        hypothesis_text = f"""
**Statement**: {hypothesis.get('statement', hypothesis.get('idea', 'N/A'))}
**Rationale**: {hypothesis.get('rationale', 'N/A')}
**Expected Signal**: {hypothesis.get('expected_signal', 'N/A')}
**Key Fields Suggested**: {', '.join(hypothesis.get('key_fields', []))}
"""
    else:
        hypothesis_text = str(hypothesis)
    
    fields_section = ""
    if fields_used:
        fields_section = f"\n**Fields Actually Used**: {', '.join(fields_used)}"
    
    return f"""## Alignment Check Request

### Hypothesis
{hypothesis_text}

### Implementation
```
{expression}
```
{fields_section}

## Task

Verify that the implementation correctly reflects the hypothesis.

**Analysis Steps**:
1. Identify the core testable claims in the hypothesis
2. Identify what the expression actually computes/tests
3. Check for alignment between the two
4. Note any gaps or additions

**Output Schema** (JSON):
```json
{{
  "hypothesis_analysis": {{
    "core_claim": "What the hypothesis is fundamentally testing",
    "key_elements": ["element1", "element2"],
    "expected_behavior": "How the signal should behave if hypothesis is true"
  }},
  "implementation_analysis": {{
    "what_it_computes": "Plain English description of what the expression does",
    "elements_present": ["element1", "element2"],
    "implicit_assumptions": ["Any assumptions not stated in hypothesis"]
  }},
  "alignment": {{
    "is_aligned": true/false,
    "confidence": 0.0-1.0,
    "gaps": ["Hypothesis elements not implemented"],
    "additions": ["Implementation elements not in hypothesis"],
    "severity": "none | minor | major | critical"
  }},
  "recommendations": {{
    "if_misaligned": "How to fix the implementation to match hypothesis",
    "alternative_implementations": ["Other ways to test this hypothesis"]
  }}
}}
```"""


def build_attribution_prompt(
    hypothesis: Dict,
    expression: str,
    result: Dict,
    alignment_check: Optional[AlignmentResult] = None,
    similar_experiments: Optional[List[Dict]] = None
) -> str:
    """
    Build prompt for attributing experiment outcome to hypothesis vs implementation.
    
    Args:
        hypothesis: The hypothesis tested
        expression: The implementation
        result: Backtest results
        alignment_check: Optional prior alignment check
        similar_experiments: Optional similar experiments for comparison
    """
    
    hypothesis_text = ""
    if isinstance(hypothesis, dict):
        hypothesis_text = f"""
**Statement**: {hypothesis.get('statement', hypothesis.get('idea', 'N/A'))}
**Rationale**: {hypothesis.get('rationale', 'N/A')}
"""
    else:
        hypothesis_text = str(hypothesis)
    
    # Format results
    result_text = f"""
**Success**: {'Yes' if result.get('success') else 'No'}
**Sharpe**: {result.get('sharpe', 'N/A')}
**Fitness**: {result.get('fitness', 'N/A')}
**Turnover**: {result.get('turnover', 'N/A')}
**Error**: {result.get('error', 'None')}
**Failed Checks**: {', '.join(result.get('failed_checks', [])) or 'None'}
"""
    
    # Format alignment check if available
    alignment_section = ""
    if alignment_check:
        alignment_section = f"""
### Prior Alignment Analysis
**Aligned**: {'Yes' if alignment_check.is_aligned else 'No'} (confidence: {alignment_check.confidence:.0%})
**Issues**: {'; '.join(alignment_check.alignment_issues) or 'None'}
**Missing Elements**: {', '.join(alignment_check.missing_elements) or 'None'}
"""
    
    # Format similar experiments if available
    similar_section = ""
    if similar_experiments:
        similar_items = []
        for i, exp in enumerate(similar_experiments[:3], 1):
            similar_items.append(f"""
**Similar Experiment {i}**:
- Hypothesis: {exp.get('hypothesis', 'N/A')[:100]}
- Expression: `{exp.get('expression', 'N/A')[:80]}`
- Result: {'Success' if exp.get('success') else 'Failed'} (Sharpe: {exp.get('sharpe', 'N/A')})
""")
        similar_section = f"""
### Similar Experiments (For Reference)
{chr(10).join(similar_items)}
"""
    
    return f"""## Result Attribution Request

### Hypothesis
{hypothesis_text}

### Implementation
```
{expression}
```

### Results
{result_text}
{alignment_section}
{similar_section}

## Task

Determine whether the experiment outcome should be attributed to:
1. The hypothesis being wrong (the idea doesn't work)
2. The implementation being wrong (the idea might work, but wasn't tested correctly)
3. Both
4. Unknown (insufficient evidence)

**Output Schema** (JSON):
```json
{{
  "attribution": {{
    "primary_cause": "hypothesis | implementation | both | unknown",
    "confidence": 0.0-1.0,
    "reasoning": "Explanation of the attribution"
  }},
  "evidence": {{
    "hypothesis_indicators": ["Evidence pointing to hypothesis issue"],
    "implementation_indicators": ["Evidence pointing to implementation issue"]
  }},
  "recommendations": {{
    "should_retry_implementation": true/false,
    "retry_suggestion": "How to implement differently",
    "should_modify_hypothesis": true/false,
    "modification_suggestion": "How to refine the hypothesis",
    "should_abandon": true/false,
    "abandon_reasoning": "Why this direction should be abandoned"
  }},
  "knowledge_implications": {{
    "valid_conclusions": ["What we can confidently conclude from this"],
    "invalid_conclusions": ["What we should NOT conclude"],
    "needs_more_evidence": ["What requires further testing"]
  }}
}}
```"""


# =============================================================================
# EVALUATION HELPERS
# =============================================================================

def quick_alignment_check(
    hypothesis: Dict,
    expression: str,
    fields: List[Dict]
) -> Tuple[bool, List[str]]:
    """
    Quick heuristic alignment check without LLM.
    
    Returns:
        (is_likely_aligned, issues)
    """
    issues = []
    
    # Get key fields from hypothesis
    suggested_fields = hypothesis.get('key_fields', [])
    
    # Check if suggested fields are present in expression
    expression_lower = expression.lower()
    for field in suggested_fields:
        if field.lower() not in expression_lower:
            issues.append(f"Suggested field '{field}' not found in expression")
    
    # Check for expected signal type indicators
    expected_signal = hypothesis.get('expected_signal', '').lower()
    
    momentum_indicators = ['ts_delta', 'ts_returns', 'ts_rank', 'delay']
    mean_reversion_indicators = ['ts_zscore', 'ts_mean', 'ts_std']
    
    if expected_signal == 'momentum':
        has_momentum = any(ind in expression_lower for ind in momentum_indicators)
        if not has_momentum:
            issues.append("Hypothesis expects momentum signal but expression lacks momentum operators")
    
    elif expected_signal == 'mean_reversion':
        has_reversion = any(ind in expression_lower for ind in mean_reversion_indicators)
        if not has_reversion:
            issues.append("Hypothesis expects mean-reversion signal but expression lacks relevant operators")
    
    is_aligned = len(issues) == 0
    return is_aligned, issues


def determine_attribution_heuristic(
    result: Dict,
    alignment_issues: List[str],
    validation_error: Optional[str] = None
) -> str:
    """
    Quick heuristic attribution without LLM.
    
    Returns: "hypothesis" | "implementation" | "both" | "unknown"
    """
    
    # Clear implementation failure
    if validation_error:
        if 'syntax' in validation_error.lower() or 'field' in validation_error.lower():
            return "implementation"
    
    # Has alignment issues
    if alignment_issues:
        # If also has poor metrics, likely both
        sharpe = result.get('sharpe', 0)
        if isinstance(sharpe, (int, float)) and sharpe < 0.5:
            return "both"
        return "implementation"
    
    # No alignment issues, poor results -> hypothesis issue
    sharpe = result.get('sharpe', 0)
    if isinstance(sharpe, (int, float)) and sharpe < 0.5:
        return "hypothesis"
    
    return "unknown"


def determine_attribution_dual_run(
    signal_result: Dict,
    control_result: Dict,
    delta_sharpe_min: float,
) -> Tuple[str, float, List[str]]:
    """Signal-vs-control attribution based on Δ(sharpe_signal − sharpe_control).

    Derives attribution by comparing the alpha's performance against a "control"
    expression (T1 signal core stripped to raw field, T2/T3 wrappers preserved).
    A large positive Δ proves the hypothesis-driven signal is doing real work;
    a small Δ reveals the T2/T3 structure alone carries the performance.

    Returns (attribution, confidence, evidence) where attribution is:
      "hypothesis"     — Δ ≥ +threshold → signal materially outperforms control;
                         the hypothesis signal is making the alpha work.
      "implementation" — |Δ| < threshold → signal ≈ control; performance is a
                         structural artifact (the wrapper carries it, not the signal).
      "both"           — Δ ≤ −threshold → control beats signal; the ts-transformation
                         is actively counterproductive — both hypothesis and
                         implementation are suspect.

    confidence measures classification certainty: it is 0.5 at either decision
    boundary (±threshold) — where the verdict is a near-coin-flip — and rises
    toward 1.0 as Δ moves away from the nearest boundary in *either* direction
    (deep into any zone). It is comparable across all three attribution labels.

    来源: docs/alphagbm_skills_research_2026-05-15.md P0 — signal-vs-control 双跑归因
    """
    sig_sharpe = signal_result.get("sharpe") or 0
    ctl_sharpe = control_result.get("sharpe") or 0
    if not isinstance(sig_sharpe, (int, float)):
        sig_sharpe = 0.0
    if not isinstance(ctl_sharpe, (int, float)):
        ctl_sharpe = 0.0

    delta = sig_sharpe - ctl_sharpe
    evidence: List[str] = [
        f"signal_sharpe={sig_sharpe:.3f}",
        f"control_sharpe={ctl_sharpe:.3f}",
        f"delta_sharpe={delta:.3f} (threshold={delta_sharpe_min})",
    ]

    # confidence (Direction A — classification certainty): distance from the
    # nearest decision boundary (±threshold). 0.5 exactly on a boundary, rising
    # to 1.0 as Δ moves away into any zone. A verdict near a boundary is a
    # near-coin-flip → low confidence; deep in a zone → high confidence.
    boundary_dist = abs(abs(delta) - delta_sharpe_min)
    confidence = min(1.0, 0.5 + boundary_dist / (2.0 * max(delta_sharpe_min, 0.1)))

    if delta >= delta_sharpe_min:
        evidence.append("signal materially outperforms control — hypothesis signal is doing work")
        return "hypothesis", confidence, evidence

    if delta <= -delta_sharpe_min:
        evidence.append("control outperforms signal — ts-transformation is counterproductive")
        return "both", confidence, evidence

    evidence.append(
        "signal ≈ control — performance is a structural artifact, hypothesis signal is inert"
    )
    return "implementation", confidence, evidence


# =============================================================================
# KNOWLEDGE FILTER
# =============================================================================

def filter_knowledge_by_attribution(
    knowledge_candidates: List[str],
    attribution: ExperimentAttribution
) -> Dict[str, List[str]]:
    """
    Filter knowledge candidates based on attribution.
    
    Only knowledge from well-attributed experiments should be transferred.
    
    Returns:
        {
            "confident": [...],  # High confidence knowledge
            "tentative": [...],  # Moderate confidence
            "discard": [...]     # Should not be used
        }
    """
    confident = []
    tentative = []
    discard = []
    
    for knowledge in knowledge_candidates:
        # If attribution is to implementation and we're learning about hypothesis
        if attribution.failure_type == "implementation":
            if "hypothesis" in knowledge.lower() or "idea" in knowledge.lower():
                discard.append(knowledge)
                continue
        
        # If attribution confidence is high, knowledge is more reliable
        if attribution.confidence > 0.8:
            confident.append(knowledge)
        elif attribution.confidence > 0.5:
            tentative.append(knowledge)
        else:
            tentative.append(knowledge)
    
    return {
        "confident": confident,
        "tentative": tentative,
        "discard": discard
    }


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Data classes
    "AlignmentResult",
    "ExperimentAttribution",
    # System prompts
    "ALIGNMENT_CHECK_SYSTEM",
    "ATTRIBUTION_SYSTEM",
    # Prompt builders
    "build_alignment_check_prompt",
    "build_attribution_prompt",
    # Helpers
    "quick_alignment_check",
    "determine_attribution_heuristic",
    "filter_knowledge_by_attribution",
]
