"""Phase 3 R1b.1b: retry prompt template for CODE_GEN_RETRY node.

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §3.4.

When R1a / R5 attribution says IMPLEMENTATION (the hypothesis is sound but
the expression is buggy / structurally wrong), feed the failure context
into the LLM and ask for a rewritten expression that PRESERVES the
hypothesis intent while addressing the specific implementation issue.

Per plan [V1.2-A2-2]: include the R5 c₂ alignment reason because that's
already an LLM-generated diagnosis of WHY the implementation diverged from
the hypothesis. Reusing it is essentially 1-step CoSTEER reflection — much
cheaper than asking the retry LLM to re-discover the failure mode.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.agents.prompts.base import build_operators_context


# The "Operator reality" block below is mirrored — with intentional framing +
# above/below wording differences — in generation.ALPHA_GENERATION_SYSTEM and
# the self_correction system prompt (validation.py + prompts.yaml). Keep the
# operator-reality rules in sync across all three sites when editing.
R1B_RETRY_SYSTEM = """You are a quantitative alpha engineer.
The user submitted an expression that FAILED in simulation but the failure
was attributed to IMPLEMENTATION (the idea is sound, the code is buggy or
structurally inappropriate). Your job is to PRESERVE the hypothesis intent
while rewriting the expression to address the specific implementation issue.

Rules:
- DO NOT change the underlying hypothesis (e.g., if it tests momentum, your
  fix must still test momentum).
- DO change: operator choices, windows, neutralization, wrappers, type
  coercion, dimensionality.
- Stay within the allowed fields list provided.
- Output strict JSON, no markdown, no commentary.

Operator reality (FASTEXPR — do NOT hallucinate operators):
- Use ONLY operators from the allowed-operators list below, called with EXACTLY
  the arguments in their signature (e.g. ts_regression(y, x, d, ...) needs three
  inputs — passing two fails with "should be exactly 3 input(s)").
- Do NOT invent prefixed variants: there is no vec_ts_* (vec_ts_rank/vec_ts_delta),
  no ts_vec_*, no neg_ts_*. Sign-flip with multiply(-1, expr) (there is no neg(x));
  absolute value is abs(expr).
- BRAIN is not Python/numpy: range(N), sequence(N), arange, linspace, pow, vec_mean,
  vec_to_matrix do NOT exist. Use power(x, n) for exponentiation and a listed Vector
  operator for vector aggregation.
- A VECTOR-typed field must be reduced with a listed Vector operator before any
  ts_*/arithmetic operator (e.g. ts_rank(vec_avg(vector_field), 20))."""


R1B_RETRY_USER_TEMPLATE = """### Original expression (FAILED)
{original_expression}

### Hypothesis being tested
{original_hypothesis}

### Failure metrics
sharpe={sharpe!r}, fitness={fitness!r}, turnover={turnover!r}

### R1a evidence (heuristic diagnosis)
{r1a_evidence_bullets}

### R5 c2 implementation diagnosis (LLM judge alignment description vs expression)
{r5_c2_reason}

### Allowed fields (truncated)
{allowed_fields_str}

### Allowed operators (with signatures — call with EXACTLY these arguments)
{allowed_operators_str}

### Output (strict JSON, no markdown)
{{
  "fixed_expression": "<new BRAIN DSL expression>",
  "changes_made": "<1-sentence diff explanation>",
  "addresses": ["<which R1a/R5 evidence line this fix targets>"]
}}"""


def build_r1b_retry_prompt(
    *,
    original_expression: str,
    original_hypothesis: str,
    failure_metrics: Dict[str, Any],
    r1a_evidence: List[Any],
    r5_c2_reason: str,
    allowed_fields: List[str],
    operators: Optional[List[Dict]] = None,
    max_fields_in_prompt: int = 50,
) -> Tuple[str, str]:
    """Compose ``(system_prompt, user_prompt)`` for ``node_code_gen_retry``.

    All inputs defensive — missing / None values render as empty strings
    so the prompt is always well-formed.

    ``operators`` is the catalog (``state.operators`` shape, each carrying a
    ``definition`` signature). When provided, the retry prompt shows the LLM
    the exact operator signatures so a rewrite doesn't re-introduce arity /
    hallucinated-operator failures (parity with code_gen — see
    build_operators_context). None → a conservative placeholder.
    """
    metrics = failure_metrics or {}
    allowed_operators_str = (
        build_operators_context(operators)
        if operators
        else (
            "(operator list unavailable — use only standard BRAIN FASTEXPR "
            "operators, each called with its documented arity)"
        )
    )
    evidence_lines = []
    for line in (r1a_evidence or [])[:6]:
        text = str(line).strip()
        if text:
            evidence_lines.append(f"- {text}")
    if not evidence_lines:
        evidence_lines.append("- (no heuristic evidence recorded)")
    allowed_fields_str = ", ".join(
        str(f) for f in (allowed_fields or [])[:max_fields_in_prompt]
    ) or "(allowed-fields list empty — use BRAIN OHLCV defaults: close, open, high, low, volume, vwap)"
    user_prompt = R1B_RETRY_USER_TEMPLATE.format(
        original_expression=(original_expression or "").strip() or "<EMPTY>",
        original_hypothesis=(original_hypothesis or "").strip() or "(no hypothesis recorded)",
        sharpe=metrics.get("sharpe"),
        fitness=metrics.get("fitness"),
        turnover=metrics.get("turnover"),
        r1a_evidence_bullets="\n".join(evidence_lines),
        r5_c2_reason=(r5_c2_reason or "").strip() or "(no R5 c2 reason recorded — R5 may be OFF)",
        allowed_fields_str=allowed_fields_str,
        allowed_operators_str=allowed_operators_str,
    )
    return R1B_RETRY_SYSTEM, user_prompt


__all__ = [
    "R1B_RETRY_SYSTEM",
    "R1B_RETRY_USER_TEMPLATE",
    "build_r1b_retry_prompt",
]
