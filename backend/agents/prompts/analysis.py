"""
Analysis prompts for experiment feedback and knowledge extraction.

Redesigned based on RD-Agent's feedback-driven approach:
- Structured observation-evaluation-decision cycle
- Knowledge transfer and extraction
- Learning from both successes and failures
- No preconceived biases
- Hypothesis-Implementation alignment checking

Contains:
- ROUND_ANALYSIS_SYSTEM: System prompt for round analysis
- FAILURE_ANALYSIS_SYSTEM: System prompt for failure analysis
- FEEDBACK_GENERATION_SYSTEM: System prompt for generating experiment feedback
- build_round_analysis_prompt: Builder for round analysis
- build_feedback_prompt: Builder for experiment feedback
- build_enhanced_feedback_prompt: Feedback with alignment and attribution
"""

from typing import List, Dict, Optional

from backend.agents.prompts.alignment import (
    AlignmentResult,
    ExperimentAttribution,
    quick_alignment_check,
    determine_attribution_heuristic,
)


ROUND_ANALYSIS_SYSTEM = """You are an experimental results analyst in a research process.

Your role is to analyze experiment outcomes objectively and generate insights for the next iteration.

**Analysis Framework**:
1. **Observe**: What happened in the experiments?
2. **Evaluate**: Did the hypotheses hold? Why or why not?
3. **Decide**: What direction should the next iteration take?
4. **Extract**: What transferable knowledge can be captured?

**Principles**:
- Be evidence-based: Support conclusions with specific data points
- Be objective: Do not assume certain approaches are better a priori
- Be actionable: Provide specific, implementable recommendations
- Be balanced: Consider both exploitation (refine what works) and exploration (try new things)
"""


FAILURE_ANALYSIS_SYSTEM = """You are a research process analyst specializing in learning from failures.

Your role is to extract actionable insights from failed experiments.

**Approach**:
1. Categorize failures by root cause (not just symptoms)
2. Identify patterns that could be avoided
3. Extract transferable rules in "If..., then..." format
4. Suggest process improvements

**Principles**:
- Failures are data, not just problems
- Each failure can teach something for future experiments
- Look for systemic issues, not just individual mistakes
"""


FEEDBACK_GENERATION_SYSTEM = """You are a research feedback specialist helping to evaluate experiment results.

Your role is to provide structured feedback on experiments to guide the next iteration.

**Feedback Structure**:
1. **Observation**: Factual summary of what happened
2. **Hypothesis Evaluation**: Did the experiment support or refute the hypothesis?
3. **Decision**: Is this a successful direction to continue, or should we pivot?
4. **New Hypothesis**: Suggested next hypothesis based on this result
5. **Knowledge**: Transferable insight in "If..., then..." format

Be objective and evidence-based. Avoid unsubstantiated claims."""


def build_round_analysis_prompt(
    iteration: int,
    max_iterations: int,
    metrics_summary: str,
    success_examples: str,
    failure_examples: str,
    dataset_id: str,
    region: str,
    cumulative_success: int,
    target_goal: int,
    experiment_trace: Optional[List[Dict]] = None
) -> str:
    """
    Build prompt for round analysis and strategy generation.
    
    Redesigned to include experiment trace and knowledge extraction.
    """
    
    progress_pct = (cumulative_success / max(target_goal, 1)) * 100
    remaining_rounds = max_iterations - iteration
    
    # Build experiment trace section
    trace_section = ""
    if experiment_trace:
        trace_items = []
        for i, exp in enumerate(experiment_trace[-5:], 1):
            trace_items.append(f"""
**Experiment {i}**:
- Hypothesis: {exp.get('hypothesis', 'N/A')}
- Result: {exp.get('result', 'N/A')}
- Key metrics: Sharpe={exp.get('sharpe', 'N/A')}, Fitness={exp.get('fitness', 'N/A')}
- Outcome: {'SUCCESS' if exp.get('success') else 'FAILED'} - {exp.get('reason', '')}
""")
        
        trace_section = f"""
### Recent Experiment Trace
{''.join(trace_items)}
"""
    
    return f"""## Round {iteration} Analysis

### Progress Overview
- **Cumulative Success**: {cumulative_success}/{target_goal} ({progress_pct:.0f}%)
- **Remaining Rounds**: {remaining_rounds}
- **Dataset**: {dataset_id} | **Region**: {region}

### This Round's Metrics
{metrics_summary}

### Successes This Round
{success_examples if success_examples else "No successes this round."}

### Failures This Round
{failure_examples if failure_examples else "No failures recorded."}
{trace_section}

## Analysis Task

Provide structured analysis following the observe-evaluate-decide framework.

**Output Schema** (JSON):
```json
{{
  "observation": {{
    "summary": "Factual summary of this round's results",
    "success_rate": "X/Y alphas passed",
    "key_metrics": "Notable metric patterns"
  }},
  "evaluation": {{
    "hypotheses_validated": ["Hypotheses that were supported by results"],
    "hypotheses_refuted": ["Hypotheses that were refuted by results"],
    "unexpected_findings": ["Surprises or anomalies observed"],
    "bottleneck_analysis": "What is the main obstacle to progress?"
  }},
  "decision": {{
    "continue_directions": ["Directions worth continuing to explore"],
    "pivot_from": ["Directions to move away from"],
    "new_directions": ["New unexplored directions to consider"],
    "balance": "exploitation | exploration | balanced"
  }},
  "strategy": {{
    "exploration_weight": 0.5,
    "temperature": 0.7,
    "focus_hypotheses": ["Specific hypotheses for next round"],
    "preferred_fields": ["Fields that showed promise"],
    "avoid_patterns": ["Patterns that consistently failed"],
    "action_summary": "Concise strategy in one sentence"
  }},
  "knowledge_transfer": {{
    "rules_extracted": [
      "If [condition], then [outcome/recommendation]"
    ],
    "patterns_discovered": ["New patterns discovered this round"],
    "confidence_adjustments": ["Beliefs that should be updated based on evidence"]
  }}
}}
```"""


def build_feedback_prompt(
    experiment: Dict,
    result: Dict,
    sota_result: Optional[Dict] = None
) -> str:
    """
    Build prompt for generating experiment feedback.
    
    Based on RD-Agent's feedback generation pattern.
    
    Args:
        experiment: The experiment that was run
        result: The result of the experiment
        sota_result: Optional current best result for comparison
    """
    
    # Build comparison section
    comparison_section = ""
    if sota_result:
        comparison_section = f"""
### Comparison with Current Best (SOTA)

**SOTA Performance**:
- Sharpe: {sota_result.get('sharpe', 'N/A')}
- Fitness: {sota_result.get('fitness', 'N/A')}
- Expression: `{sota_result.get('expression', 'N/A')[:80]}`

**This Experiment vs SOTA**:
- Sharpe difference: {_diff(result.get('sharpe'), sota_result.get('sharpe'))}
- Fitness difference: {_diff(result.get('fitness'), sota_result.get('fitness'))}
"""
    
    return f"""## Experiment Feedback Request

### Experiment Details
**Hypothesis**: {experiment.get('hypothesis', 'Not specified')}
**Expression**: `{experiment.get('expression', 'N/A')}`
**Rationale**: {experiment.get('rationale', 'Not provided')}

### Results
**Success**: {'Yes' if result.get('success') else 'No'}
**Sharpe**: {result.get('sharpe', 'N/A')}
**Fitness**: {result.get('fitness', 'N/A')}
**Turnover**: {result.get('turnover', 'N/A')}
**Error** (if any): {result.get('error', 'None')}
{comparison_section}

## Feedback Task

Provide structured feedback on this experiment.

**Output Schema** (JSON):
```json
{{
  "observation": "Factual summary of the experiment outcome",
  "hypothesis_evaluation": "Did this experiment support or refute the hypothesis? Why?",
  "decision": {{
    "success": true/false,
    "reasoning": "Why is this considered success/failure?",
    "replace_sota": true/false,
    "replace_reasoning": "Why should/shouldn't this replace current best?"
  }},
  "new_hypothesis": {{
    "statement": "Suggested next hypothesis based on this result",
    "rationale": "Why this is a logical next step"
  }},
  "knowledge_extracted": "If [observation from this experiment], then [conclusion/rule]"
}}
```"""


def _diff(a, b):
    """Calculate difference between two values."""
    try:
        if a is None or b is None:
            return 'N/A'
        diff = float(a) - float(b)
        sign = '+' if diff >= 0 else ''
        return f"{sign}{diff:.3f}"
    except:
        return 'N/A'


def build_enhanced_feedback_prompt(
    experiment: Dict,
    result: Dict,
    alignment_check: Optional[AlignmentResult] = None,
    similar_experiments: Optional[List[Dict]] = None,
    sota_result: Optional[Dict] = None
) -> str:
    """
    Build enhanced feedback prompt with alignment and attribution analysis.
    
    This is the key prompt for ensuring accurate knowledge transfer by:
    1. Checking if implementation matches hypothesis
    2. Attributing outcomes to hypothesis vs implementation
    3. Filtering knowledge based on attribution confidence
    
    Args:
        experiment: The experiment that was run
        result: The result of the experiment
        alignment_check: Optional prior alignment check result
        similar_experiments: Optional similar experiments for comparison
        sota_result: Optional current best result for comparison
    """
    
    # Build hypothesis section
    hypothesis = experiment.get('hypothesis', {})
    if isinstance(hypothesis, dict):
        hypothesis_text = f"""
**Statement**: {hypothesis.get('statement', hypothesis.get('idea', 'N/A'))}
**Rationale**: {hypothesis.get('rationale', 'N/A')}
**Expected Signal**: {hypothesis.get('expected_signal', 'N/A')}
**Key Fields**: {', '.join(hypothesis.get('key_fields', []))}
"""
    else:
        hypothesis_text = f"**Statement**: {hypothesis}"
    
    expression = experiment.get('expression', 'N/A')
    
    # Build alignment section
    alignment_section = ""
    if alignment_check:
        alignment_section = f"""
### Hypothesis-Implementation Alignment Check

**Aligned**: {'Yes' if alignment_check.is_aligned else 'No'} (confidence: {alignment_check.confidence:.0%})

**Hypothesis Elements**: {', '.join(alignment_check.hypothesis_elements) or 'Not analyzed'}
**Implementation Elements**: {', '.join(alignment_check.implementation_elements) or 'Not analyzed'}

**Alignment Issues**:
{chr(10).join(f'- {issue}' for issue in alignment_check.alignment_issues) or 'None identified'}

**Missing from Implementation**: {', '.join(alignment_check.missing_elements) or 'None'}
**Extra in Implementation**: {', '.join(alignment_check.extra_elements) or 'None'}
"""
    else:
        # Quick heuristic check if no formal alignment check
        fields = experiment.get('fields', [])
        is_aligned, issues = quick_alignment_check(hypothesis if isinstance(hypothesis, dict) else {}, expression, fields)
        if issues:
            alignment_section = f"""
### Quick Alignment Check (Heuristic)

**Likely Aligned**: {'Yes' if is_aligned else 'No'}
**Potential Issues**:
{chr(10).join(f'- {issue}' for issue in issues)}
"""
    
    # Build comparison section
    comparison_section = ""
    if sota_result:
        comparison_section = f"""
### Comparison with Current Best (SOTA)

**SOTA Performance**:
- Sharpe: {sota_result.get('sharpe', 'N/A')}
- Fitness: {sota_result.get('fitness', 'N/A')}

**This Experiment vs SOTA**:
- Sharpe difference: {_diff(result.get('sharpe'), sota_result.get('sharpe'))}
"""
    
    # Build similar experiments section
    similar_section = ""
    if similar_experiments:
        similar_items = []
        for i, exp in enumerate(similar_experiments[:3], 1):
            similar_items.append(f"""
**Similar {i}**: {exp.get('hypothesis', 'N/A')[:80]}
- Result: {'Success' if exp.get('success') else 'Failed'}
- Attribution: {exp.get('attribution', 'Unknown')}
""")
        similar_section = f"""
### Similar Past Experiments
{chr(10).join(similar_items)}
"""
    
    return f"""## Enhanced Experiment Feedback Request

### Hypothesis Under Test
{hypothesis_text}

### Implementation
```
{expression}
```

### Results
**Success**: {'Yes' if result.get('success') else 'No'}
**Sharpe**: {result.get('sharpe', 'N/A')}
**Fitness**: {result.get('fitness', 'N/A')}
**Turnover**: {result.get('turnover', 'N/A')}
**Error** (if any): {result.get('error', 'None')}
**Failed Checks**: {', '.join(result.get('failed_checks', [])) or 'None'}
{alignment_section}
{comparison_section}
{similar_section}

## Analysis Task

Provide comprehensive feedback with careful attribution.

**CRITICAL**: Distinguish between:
1. **Hypothesis Failure**: The idea itself is wrong (multiple implementations failed, clear logical flaw)
2. **Implementation Failure**: The idea might be valid, but wasn't tested correctly (misalignment, errors)
3. **Uncertain**: Need more evidence before concluding

Only extract knowledge that is well-supported by the attribution.

**Output Schema** (JSON):
```json
{{
  "observation": "Factual summary of what happened",
  
  "alignment_assessment": {{
    "is_aligned": true/false,
    "confidence": 0.0-1.0,
    "issues": ["Any alignment issues identified"]
  }},
  
  "attribution": {{
    "primary_cause": "hypothesis | implementation | both | unknown",
    "confidence": 0.0-1.0,
    "hypothesis_evidence": ["Evidence for/against hypothesis being the issue"],
    "implementation_evidence": ["Evidence for/against implementation being the issue"]
  }},
  
  "hypothesis_evaluation": {{
    "supported": true/false/null,
    "evaluation": "Did results support or refute the hypothesis?",
    "caveats": ["Reasons to be cautious about this conclusion"]
  }},
  
  "decision": {{
    "should_retry_implementation": true/false,
    "retry_approach": "How to implement differently if retrying",
    "should_modify_hypothesis": true/false,
    "modification": "How to refine hypothesis if modifying",
    "should_abandon": true/false,
    "abandon_reason": "Why abandon if applicable",
    "replace_sota": true/false
  }},
  
  "knowledge_extraction": {{
    "confident_knowledge": [
      "If [well-supported condition], then [well-supported conclusion]"
    ],
    "tentative_knowledge": [
      "If [partially supported condition], then [tentative conclusion] (needs more evidence)"
    ],
    "should_not_conclude": [
      "We should NOT conclude X because Y"
    ]
  }},
  
  "next_hypothesis": {{
    "statement": "Suggested next hypothesis if applicable",
    "rationale": "Why this is a logical next step",
    "builds_on": "What this builds on from current experiment"
  }}
}}
```"""


FAILURE_ANALYSIS_USER = """## Failure Analysis Request

### Failure Statistics
**Total Failures**: {count}
**Time Period**: Current session

### Error Distribution
{error_distribution}

### Representative Failures
{sample_failures}

## Analysis Task

Extract actionable insights from these failures.

**Output Schema** (JSON):
```json
{{
  "categorization": {{
    "syntax_errors": {{
      "count": 0,
      "common_causes": ["List of causes"],
      "prevention": "How to prevent"
    }},
    "field_errors": {{
      "count": 0,
      "problematic_fields": ["Fields causing issues"],
      "prevention": "How to prevent"
    }},
    "quality_failures": {{
      "count": 0,
      "patterns": ["Common patterns in quality failures"],
      "improvement": "How to improve"
    }}
  }},
  "rules_extracted": [
    {{
      "condition": "If [this pattern is observed]",
      "recommendation": "Then [do this / avoid this]",
      "confidence": "high | medium | low",
      "frequency": "How often this pattern occurred"
    }}
  ],
  "process_improvements": [
    {{
      "area": "generation | validation | optimization",
      "suggestion": "Specific improvement suggestion",
      "expected_impact": "How this would help"
    }}
  ],
  "knowledge_base_updates": [
    {{
      "type": "add_to_pitfalls | add_to_blacklist | update_guidance",
      "content": "What to add/update",
      "reasoning": "Why this should be added"
    }}
  ]
}}
```"""
