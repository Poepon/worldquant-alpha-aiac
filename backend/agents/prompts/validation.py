"""
Validation and optimization prompts.

Redesigned based on RD-Agent's feedback-driven approach:
- Emphasize learning from errors
- Multiple solution paths without prescriptive bias
- Knowledge transfer from similar problems

Contains:
- SELF_CORRECT_SYSTEM: System prompt for self-correction
- OPTIMIZATION_SYSTEM: System prompt for alpha optimization
- build_self_correct_prompt: Builder for self-correction prompt
- build_optimization_prompt: Builder for optimization prompt
"""

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.alpha_semantic_validator import Finding  # noqa: F401

# V-26.61 (2026-05-13): SELF_CORRECT_SYSTEM is now sourced from
# prompts.yaml via the PromptLoader, matching the rest of the prompt
# registry. The hardcoded fallback below keeps the import surface stable
# for callers (validation node, tests) and guards against YAML load
# failure — if the loader can't read self_correction.system the live
# value gracefully falls back to the in-code text.
from backend.agents.prompts.loader import get_prompt_loader as _get_prompt_loader

_FALLBACK_SELF_CORRECT_SYSTEM = """You are a code debugger helping to fix alpha expressions.

Your role is to:
1. Diagnose why an expression failed
2. Understand the root cause
3. Propose a minimal fix that addresses the issue

**Approach**:
- Focus on fixing the specific error, not rewriting everything
- Consider multiple possible fixes and choose the most appropriate
- Learn from the error pattern for future reference

Be precise and targeted in your corrections.

**V-14 BRAIN OPERATOR REALITY (avoid hallucinating these)**:
- BRAIN is FASTEXPR, NOT Python/numpy/pandas. The following do NOT exist:
  sequence(N), range(N), linspace(N), arange(N), time_index(),
  np.arange, pd.Series.shift, etc.
- vec_* are AGGREGATIONS over the vector dimension. They EXIST as:
  vec_avg, vec_sum, vec_max, vec_min, vec_l2_norm, vec_count, vec_median.
  They DO NOT exist as vec_ts_*. So `vec_ts_delta(...)` is INVALID.
- VECTOR-typed fields (e.g. nws12_*, anl4_*v110_mean) cannot go directly
  into a time-series operator. Wrap with vec_* first:
  - WRONG: ts_delta(nws12_prez_4l, 20)
  - RIGHT: ts_delta(vec_avg(nws12_prez_4l), 20)
  - ALSO RIGHT: pick a MATRIX field instead.
- For ts_regression(y, x, d): the second arg `x` must be a same-length
  series (typically the same field, another field, or a constant). It is
  NEVER a synthetic index function.
- For sign-flipping a signal: use multiply(-1, expr) — there is no `neg(x)`.
- For absolute value: use abs(expr).
- DO NOT prefix existing operators (no vec_ts_*, no ts_vec_*, no neg_ts_*).

When fixing a "Type mismatch: VECTOR field" error, the answer is almost
always to add vec_avg() around the field, NOT to invent a vec_ts_* operator."""

# Live value: prefer prompts.yaml (single source of truth) but fall back
# to the Python constant if the YAML didn't load. The fallback is the
# same text the YAML was seeded with, so behaviour is identical either way.
SELF_CORRECT_SYSTEM = (
    _get_prompt_loader().get_system_prompt("self_correction")
    or _FALLBACK_SELF_CORRECT_SYSTEM
)


OPTIMIZATION_SYSTEM = """You are an alpha researcher helping to improve expression performance.

Your role is to analyze backtest results and suggest modifications that might improve metrics.

**Core Principles**:
1. **Evidence-based**: Base suggestions on the specific feedback, not generic advice
2. **Targeted**: Address the identified issues, don't change things randomly
3. **Multiple paths**: Consider different approaches without assuming one is best
4. **Incremental**: Prefer small changes that test specific hypotheses

**Optimization is hypothesis testing**: Each modification is an experiment to test 
whether a specific change improves performance."""


def build_self_correct_prompt(
    expression: str,
    findings: Optional[List[Any]] = None,
    available_fields: Optional[List[str]] = None,
    similar_errors: Optional[List[Dict]] = None,
    # Backward-compat shim — legacy callers pass (error_message, error_type)
    # as a pair; we coerce them into a single synthetic Finding.
    error_message: Optional[str] = None,
    error_type: Optional[str] = None,
) -> str:
    """Build prompt for self-correction (P1-E: structured-finding aware).

    Primary input is `findings: List[Finding]` — hard/soft/info findings are
    rendered into separate sections so the LLM sees severity-graded fix
    instructions. Legacy `(error_message, error_type)` is still accepted and
    coerced into a single synthetic Finding for backward compat.

    Args:
        expression: The failed expression
        findings: P1-E primary input — list of `Finding` records. When None
            and `error_message` is given, a synthetic Finding is materialized.
        available_fields: List of valid field names
        similar_errors: Optional list of similar errors and their fixes
        error_message: Legacy single-string error (back-compat shim)
        error_type: Legacy category string (back-compat shim)
    """
    if available_fields is None:
        available_fields = []

    # M-3 back-compat: synthesize a Finding from (error_message, error_type)
    # if no structured findings were passed.
    findings_list: List[Any] = list(findings) if findings else []
    if not findings_list and error_message:
        from backend.alpha_semantic_validator import Finding as _Finding
        findings_list = [
            _Finding(
                rule_id=(error_type or "other"),
                severity="hard",
                message=error_message,
                category="semantics",
            )
        ]

    severity_order = {"hard": 0, "soft": 1, "info": 2}
    sorted_findings = sorted(
        findings_list, key=lambda f: severity_order.get(getattr(f, "severity", "info"), 99),
    )
    hard = [f for f in sorted_findings if getattr(f, "severity", None) == "hard"]
    soft = [f for f in sorted_findings if getattr(f, "severity", None) == "soft"]
    info = [f for f in sorted_findings if getattr(f, "severity", None) == "info"]

    def _render(blocks: List[Any]) -> str:
        lines = []
        for f in blocks:
            rule_id = getattr(f, "rule_id", "other")
            severity = getattr(f, "severity", "info")
            message = getattr(f, "message", "")
            location = getattr(f, "location", None)
            line = f"- [rule={rule_id}, {severity}] {message}"
            if location:
                line += f" (location: `{location}`)"
            lines.append(line)
        return "\n".join(lines)

    sections: List[str] = []
    if hard:
        sections.append(f"### Errors that MUST be fixed\n{_render(hard)}")
    if soft:
        sections.append(f"### Warnings (fix if relevant)\n{_render(soft)}")
    if info:
        # N-3: cap info section to 5 entries to keep prompt bounded.
        info_capped = info[:5]
        sections.append(
            f"### Risk hints (informational; consider in your fix)\n"
            f"{_render(info_capped)}"
        )
    findings_section = "\n\n".join(sections) if sections else "(no findings)"

    # Compute legacy display strings — primary error_type for the diagnosis
    # block; first hard message (or first finding) for context-line.
    legacy_error_type = error_type or (hard[0].rule_id if hard else (
        sorted_findings[0].rule_id if sorted_findings else "other"
    ))
    legacy_error_message = error_message or (
        "; ".join(f.message for f in hard[:2]) if hard
        else "; ".join(f.message for f in sorted_findings[:2])
    )

    # Build similar errors section if available
    similar_section = ""
    if similar_errors:
        examples = []
        for i, err in enumerate(similar_errors[:3], 1):
            rule_tag = err.get("rule_id")
            rule_suffix = f" [rule={rule_tag}]" if rule_tag else ""
            examples.append(f"""
**Example {i}**{rule_suffix}:
- Failed: `{err.get('failed_expression', 'N/A')[:80]}`
- Error: {err.get('error', 'N/A')}
- Fixed: `{err.get('fixed_expression', 'N/A')[:80]}`
- Fix approach: {err.get('fix_description', 'N/A')}
""")

        similar_section = f"""
## Similar Errors and Fixes

These similar errors were resolved before. Learn from these patterns:
{''.join(examples)}
"""

    return f"""## Failed Expression

```
{expression}
```

## Findings

{findings_section}

## Error Information (legacy summary)

**Error Type**: {legacy_error_type}
**Error Message**:
```
{legacy_error_message}
```

## Available Fields

The following fields are valid in this context:
```
{', '.join(sorted(available_fields)[:50])}
```

{f"(... and {len(available_fields) - 50} more)" if len(available_fields) > 50 else ""}
{similar_section}

## Task

1. **Diagnose**: What specifically caused this error?
2. **Fix**: What is the minimal change needed to resolve it?
3. **Verify**: Why will the fix work?

Prioritize the **hard** findings — they invalidate the expression. Soft
findings should be addressed if your fix naturally allows it; info findings
are context only.

**Output Schema** (JSON):
```json
{{
  "diagnosis": {{
    "root_cause": "The specific reason for the error",
    "error_location": "Where in the expression the error occurs",
    "error_category": "syntax | field_name | operator_usage | parameter | other"
  }},
  "fix": {{
    "approach": "Description of the fix approach",
    "fixed_expression": "The corrected expression",
    "changes_made": "Specific changes applied",
    "confidence": "high | medium | low"
  }},
  "alternatives": [
    {{
      "expression": "Alternative fix if applicable",
      "trade_off": "Why this wasn't chosen as primary"
    }}
  ],
  "knowledge_extracted": "If [this error pattern], then [this fix approach]"
}}
```"""


def build_optimization_prompt(
    expression: str,
    metrics: Dict,
    failed_checks: List[str],
    optimization_reason: str,
    brain_checks: Optional[List[Dict]] = None,
    previous_attempts: Optional[List[Dict]] = None
) -> str:
    """
    Build prompt for alpha optimization.
    
    Redesigned to be more evidence-based and include experiment history.
    
    Args:
        expression: The alpha expression to optimize
        metrics: Backtest metrics
        failed_checks: List of failed submission checks
        optimization_reason: Why optimization is suggested
        brain_checks: Optional BRAIN platform official checks
        previous_attempts: Optional previous optimization attempts
    """
    
    # Build metrics section
    metrics_text = f"""
**Core Metrics**:
- IS Sharpe: {metrics.get('sharpe', 'N/A')}
- Fitness: {metrics.get('fitness', 'N/A')}
- Turnover: {metrics.get('turnover', 'N/A')}
- Drawdown: {metrics.get('drawdown', 'N/A')}

**Train/Test Split**:
- Train Sharpe: {metrics.get('train_sharpe', 'N/A')}
- Test Sharpe: {metrics.get('test_sharpe', 'N/A')}
- Train/Test Ratio: {_safe_ratio(metrics.get('test_sharpe'), metrics.get('train_sharpe'))}

**Constraint Metrics**:
- Risk-Neutralized Sharpe: {metrics.get('rn_sharpe', metrics.get('riskNeutralized', {}).get('sharpe', 'N/A'))}
- Investability-Constrained Sharpe: {metrics.get('invest_sharpe', metrics.get('investabilityConstrained', {}).get('sharpe', 'N/A'))}
"""
    
    # Build BRAIN checks section if available
    checks_section = ""
    if brain_checks:
        check_items = []
        for check in brain_checks[:10]:
            name = check.get('name', 'Unknown')
            result = check.get('result', 'N/A')
            limit = check.get('limit')
            value = check.get('value')
            
            if limit is not None and value is not None:
                check_items.append(f"- {name}: {result} (value={value:.3f}, limit={limit:.3f})")
            else:
                check_items.append(f"- {name}: {result}")
        
        checks_section = f"""
## BRAIN Platform Checks (Official)

These are the actual platform checks and their results:
{chr(10).join(check_items)}

Focus on addressing FAIL results to enable submission.
"""
    
    # Build previous attempts section if available
    attempts_section = ""
    if previous_attempts:
        attempt_items = []
        for i, attempt in enumerate(previous_attempts[-5:], 1):
            attempt_items.append(f"""
**Attempt {i}**:
- Modification: {attempt.get('modification_type', 'N/A')}
- Expression: `{attempt.get('expression', 'N/A')[:60]}...`
- Result: Sharpe {attempt.get('sharpe', 'N/A')}, Fitness {attempt.get('fitness', 'N/A')}
- Outcome: {attempt.get('outcome', 'N/A')}
""")
        
        attempts_section = f"""
## Previous Optimization Attempts

Learn from these previous attempts on this alpha:
{''.join(attempt_items)}

Avoid repeating unsuccessful approaches. Build on partial successes.
"""
    
    return f"""## Alpha Under Optimization

```
{expression}
```

## Backtest Results

{metrics_text}
{checks_section}

## Issues Identified

**Failed Checks**: {', '.join(failed_checks) if failed_checks else 'None identified'}
**Optimization Trigger**: {optimization_reason}
{attempts_section}

## Task

Generate targeted modifications to improve this alpha.

**Approach**:
1. Analyze the specific issues identified (not generic problems)
2. Propose modifications that address these issues
3. Each modification should test a specific hypothesis
4. Consider both conventional and unconventional approaches

**Modification Types to Consider** (as appropriate):
- Window adjustment: Different lookback periods
- Normalization: rank(), zscore(), scale()
- Smoothing: ts_decay_linear(), ts_mean()
- Structure changes: Operator substitution, nesting
- Sign exploration: If relationship might be inverted
- Neutralization adjustments: Different risk factor handling

**Output Schema** (JSON):
```json
{{
  "analysis": {{
    "primary_issues": ["List of main issues to address"],
    "likely_causes": ["Potential underlying causes"],
    "optimization_strategy": "Overall approach to improvement"
  }},
  "modifications": [
    {{
      "id": "M1",
      "type": "window | normalization | smoothing | structure | sign | other",
      "expression": "Modified expression",
      "hypothesis": "What this modification tests",
      "expected_impact": "How this might improve metrics",
      "addresses_issue": "Which identified issue this targets",
      "confidence": "high | medium | low"
    }}
  ],
  "priority_order": ["M1", "M2", ...],
  "knowledge_gained": "If [this pattern of metrics], then [these modifications] may help"
}}
```"""


def _safe_ratio(a, b):
    """Safely compute ratio."""
    try:
        if a is None or b is None or b == 0:
            return 'N/A'
        return f"{float(a) / float(b):.2f}"
    except:
        return 'N/A'
