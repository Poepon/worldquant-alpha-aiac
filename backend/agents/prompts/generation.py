"""
Alpha generation prompts.

Redesigned based on RD-Agent's principles:
- Hypothesis-driven generation
- No preconceived biases about what works
- Emphasis on testability and precision
- Learning from experiment feedback

Contains:
- ALPHA_GENERATION_SYSTEM: System prompt for alpha generation
- build_alpha_generation_prompt: Builder function for user prompt
"""

from typing import List, Dict, Optional

from backend.agents.prompts.base import (
    PromptContext,
    build_fields_context,
    build_operators_context,
    build_patterns_context,
    build_strategy_constraints,
)


ALPHA_GENERATION_SYSTEM = """You are a quantitative researcher implementing alpha expressions to test investment hypotheses on the WorldQuant BRAIN platform.

## BRAIN submit gate (hard math constraint you MUST consider)

Every alpha must clear ALL of these to be submittable:
- Sharpe > 1.25
- Fitness > 1.0, where `fitness ≈ sharpe × √(returns / max(turnover, 0.125))`
- Turnover ∈ [0.01, 0.70]
- Self-correlation < 0.7
- Sub-universe Sharpe ≥ ~0.26 (delay-1, varies by universe)

**Derived constraint**: To satisfy `sharpe ≥ 1.5 AND fitness ≥ 1.0`:
- If predicted turnover ≥ 0.125: `returns / turnover ≥ 0.44` (every unit of turnover must capture ≥44% annualized returns)
- If predicted turnover < 0.125: `returns ≥ 0.055` (~5.5% annualized; turnover floored, no further benefit)
- **Sweet spot: turnover 0.125 - 0.20** — low enough for high fitness, high enough for non-trivial activity.

## Economic signal velocity classification (pick one before writing the expression)

| Velocity | Source | Typical turnover | Reachable fitness |
|---|---|---|---|
| **FUNDAMENTAL_SLOW** | quarterly accounting / cash-flow / quality | 0.05-0.25 | 1.0-2.0 |
| **FACTOR_COMPOSITE** | derived multi-factor scores, smoothed signals | 0.05-0.20 | 1.5-3.0+ |
| **MEDIUM** | monthly momentum (12-1), accruals | 0.20-0.40 | 0.8-1.5 |
| **FAST** | short-term reversal, order flow, technical | 0.50-1.00 | 0.2-0.7 (mostly fails fitness gate) |

⚠ Default preference: FUNDAMENTAL_SLOW or FACTOR_COMPOSITE. Pick FAST only with explicit justification — it usually fails the fitness gate.

## Mandatory 5-slot reasoning chain (output as part of each alpha)

For EVERY alpha, fill these slots IN ORDER:

1. **economic_hypothesis** (≥ 30 chars, must contain a domain term like "应计/accrual", "现金流/cashflow", "momentum", "reversal", "quality", "factor_composite"): One-sentence economic intuition.
2. **signal_velocity**: One of `FUNDAMENTAL_SLOW`, `FACTOR_COMPOSITE`, `MEDIUM`, `FAST`.
3. **predicted_turnover**: A numeric estimate (e.g. 0.18) consistent with signal_velocity.
4. **math_sanity_check**: A numeric self-check. Compute `predicted_returns / max(predicted_turnover, 0.125)` and assert ≥ 0.44; if FAST, explicitly note "expected to fail fitness gate".
5. **expression**: The FASTEXPR string. Window ≥ 20 preferred; smoothing operators (ts_zscore / ts_rank / ts_regression / ts_decay_linear) preferred for SLOW/COMPOSITE.

## Three reference examples (learn the pattern, don't copy literally)

### Example 1 — FACTOR_COMPOSITE (highest historical sharpe 3.22)
- economic_hypothesis: 复合因子分数的派生量反映多维度共振，月窗 zscore 平滑保留信号、降换手
- signal_velocity: FACTOR_COMPOSITE
- predicted_turnover: 0.09
- math_sanity_check: returns ≈ 7%, returns/max(0.09, 0.125) = 0.07/0.125 = 0.56 ≥ 0.44 ✓
- expression: ts_zscore(composite_factor_score_derivative, 20)

### Example 2 — FUNDAMENTAL_SLOW (sharpe 1.45, fitness 1.40)
- economic_hypothesis: 累计应计负债季度变化反映现金流压力，高应计公司未来 underperform
- signal_velocity: FUNDAMENTAL_SLOW
- predicted_turnover: 0.18
- math_sanity_check: returns ≈ 8%, returns/max(0.18, 0.125) = 0.08/0.18 = 0.44 ✓ (临界)
- expression: ts_rank(ts_scale(fn_accrued_liab_curr_q, 60), 5)

### Example 3 — FAST (反面教材，演示拒绝)
- economic_hypothesis: 短期价格反转
- signal_velocity: FAST
- predicted_turnover: 0.85
- math_sanity_check: returns ≈ 4%, 0.04/max(0.85, 0.125) = 0.047 ≪ 0.44 ❌ ABORT, switch to slower velocity
- expression: (do not produce; instead pick a SLOW alternative)

## Implementation guidelines

- Use only provided fields and operators.
- Ensure syntactic correctness (FASTEXPR).
- For FUNDAMENTAL_SLOW / FACTOR_COMPOSITE: prefer windows of 20-60 days with ts_zscore / ts_rank / ts_regression / ts_scale.
- Avoid bare short-window operators (ts_delta with window<5, signed_power on raw price) unless there is a strong FAST justification.

Output must be valid JSON matching the specified schema. **All 5 reasoning slots are required for every alpha**."""


def build_alpha_generation_prompt(
    ctx: PromptContext,
    target_hypothesis: Optional[Dict] = None,
    experiment_feedback: Optional[List[Dict]] = None
) -> str:
    """
    Build user prompt for alpha generation.
    
    Redesigned to be hypothesis-driven and feedback-aware.
    
    Args:
        ctx: Prompt context with fields, operators, etc.
        target_hypothesis: Optional specific hypothesis to implement
        experiment_feedback: Optional list of previous experiment results
    """
    
    # Build hypothesis section
    hypothesis_section = ""
    if target_hypothesis:
        hypothesis_section = f"""
## Target Hypothesis

You are implementing this specific hypothesis:

**Statement**: {target_hypothesis.get('statement', 'Not specified')}
**Rationale**: {target_hypothesis.get('rationale', 'Not specified')}
**Expected Signal**: {target_hypothesis.get('expected_signal', 'Not specified')}
**Suggested Fields**: {', '.join(target_hypothesis.get('key_fields', []))}
"""
    
    # Build feedback section
    feedback_section = ""
    if experiment_feedback:
        recent_feedback = experiment_feedback[-5:]  # Last 5 experiments
        feedback_entries = []
        
        for fb in recent_feedback:
            expr = fb.get('expression', 'N/A')
            if len(expr) > 80:
                expr = expr[:80] + "..."
            
            feedback_entries.append(f"""
- **Expression**: `{expr}`
  - Result: {fb.get('result', 'N/A')}
  - Sharpe: {fb.get('sharpe', 'N/A')}, Fitness: {fb.get('fitness', 'N/A')}
  - Issue: {fb.get('issue', 'None identified')}
""")
        
        feedback_section = f"""
## Recent Experiment Feedback

Learn from these recent attempts:
{''.join(feedback_entries)}

Consider:
- What worked partially that could be refined?
- What approaches haven't been tried yet?
- Are there common failure patterns to avoid?
"""
    
    # Build implementation guidance (non-prescriptive)
    implementation_guidance = """
## Implementation Approach

Consider multiple ways to implement the hypothesis:
1. **Direct implementation**: Straightforward translation of the hypothesis
2. **Normalized version**: Apply cross-sectional normalization (rank, zscore)
3. **Smoothed version**: Add time-series smoothing if appropriate
4. **Inverted version**: Test if the opposite relationship holds

Start with simpler implementations. Complexity can be added in subsequent iterations if needed.
"""
    
    # Build field reminder (critical constraint, but framed as a resource)
    field_section = f"""
## Available Resources

**Data Fields** (use only these):
{build_fields_context(ctx.fields)}

**Operators** (grouped by function):
{build_operators_context(ctx.operators)}

Note: Only fields listed above exist. Do not assume standard fields like 'close', 'volume', 
'returns', or 'cap' exist unless explicitly listed.
"""
    
    # Build patterns section (framed as observations, not rules)
    patterns_section = ""
    if ctx.success_patterns or ctx.failure_pitfalls:
        patterns_section = f"""
## Historical Observations

**Patterns that have worked** (for reference, not prescription):
{build_patterns_context(ctx.success_patterns, "success patterns")}

**Approaches that have struggled** (considerations, not prohibitions):
{build_patterns_context(ctx.failure_pitfalls, "challenges")}

These are historical observations. Context matters - what failed in one setting may work in another.
"""
    
    return f"""## Context

**Dataset**: {ctx.dataset_id}
**Description**: {ctx.dataset_description or 'Not provided'}
**Category**: {ctx.dataset_category or 'General'}
**Region**: {ctx.region} | **Universe**: {ctx.universe}
{hypothesis_section}
{field_section}
{patterns_section}
{feedback_section}
{implementation_guidance}

## Constraints

{build_strategy_constraints(ctx)}

## Task

Generate {ctx.num_alphas} distinct alpha expressions.

For each expression:
1. State the specific hypothesis being tested
2. Explain the implementation approach
3. Describe what market behavior this might capture
4. Note any assumptions or limitations

**Output Schema** (JSON — ALL fields including the 5 reasoning slots are REQUIRED for every alpha):
```json
{{
  "implementation_notes": "Brief notes on the overall approach taken",
  "alphas": [
    {{
      "economic_hypothesis": "≥30 chars, contains a domain term (e.g. 应计/accrual/cashflow/momentum/reversal/quality/factor_composite)",
      "signal_velocity": "FUNDAMENTAL_SLOW | FACTOR_COMPOSITE | MEDIUM | FAST",
      "predicted_turnover": 0.18,
      "math_sanity_check": "Show the computation: returns/max(turnover,0.125) >= 0.44. If FAST, note expected fitness-gate failure",
      "expression": "Valid FASTEXPR using only provided fields and operators",
      "hypothesis_tested": "The specific hypothesis this expression tests (can echo economic_hypothesis)",
      "explanation": {{
        "approach": "How the hypothesis is translated into code",
        "market_logic": "What market inefficiency or behavior this captures",
        "assumptions": "Key assumptions this relies on"
      }},
      "fields_used": ["field1", "field2"],
      "complexity": "simple | moderate | complex",
      "novelty_level": "established | variation | experimental"
    }}
  ],
  "alternatives_considered": [
    {{
      "expression": "Alternative implementation not used",
      "reason_not_chosen": "Why this wasn't the primary choice"
    }}
  ]
}}
```"""
