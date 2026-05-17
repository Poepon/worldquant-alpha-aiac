"""R5 Hypothesis-Alignment LLM judge prompts (Phase 2, 2026-05-18).

AlphaAgent Eq. 7 dual-bridge judge:
  C(h, d, f) = α·c₁(h, d) + (1-α)·c₂(d, f), α=0.5

  c₁(h, d): hypothesis ↔ description alignment
  c₂(d, f): description ↔ expression alignment

Both prompts return strict JSON: {"aligned": bool, "confidence": 0-1, "reason": str}.

Per plan v1.0 §2-§3 (~/.claude/plans/phase2-r5-llm-judge-2026-05-18.md):
- Inputs truncated to 2000 chars each to bound cost
- Empty description → caller skips c₁ (see r5_judge.py)
- Default thinking effort: med (~8k token budget)
- Invalid output → parser falls back to abstain (aligned=True conf=0.5)
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# c₁(h, d) — hypothesis ↔ description
# ---------------------------------------------------------------------------

R5_C1_SYSTEM = """You are a research methodology specialist verifying whether a quantitative
hypothesis is faithfully captured by its accompanying natural-language description.

Your task is judgement of CONSISTENCY, not quality. You should answer:
"Does the description (Section D) plainly state what the hypothesis (Section H) claims?"

Alignment criteria:
- Description must reference the core economic mechanism of the hypothesis (e.g.,
  "earnings surprise drives short-term momentum" must mention earnings surprise + momentum)
- Description should NOT introduce mechanisms absent from the hypothesis (e.g., hypothesis says
  "sentiment-driven", description says "based on volume and sentiment" is MISALIGNED)
- Description fidelity, not quality: a description that perfectly restates a flawed hypothesis
  is ALIGNED. A correct hypothesis with a misleading description is MISALIGNED.

Confidence: report 0.9+ only when both sections are explicit and unambiguous.
Report 0.5-0.7 for partial overlap; report at most 0.4 for "I cannot tell from these inputs alone".

Output strict JSON. No commentary. No markdown."""


R5_C1_USER_TEMPLATE = """### Section H — Hypothesis
{hypothesis_statement}

### Section D — Description
{description}

## Task

Judge whether D faithfully describes H (consistency only, not quality).

Output Schema (JSON):
{{
  "aligned": true | false,
  "confidence": 0.0-1.0,
  "reason": "<=1 sentence, <=200 chars, no markdown, no quotes"
}}
"""


def build_r5_c1_prompt(hypothesis_statement: str, description: str) -> str:
    """Build c₁(h,d) prompt — hypothesis ↔ description alignment.

    Args:
        hypothesis_statement: Alpha.hypothesis text (or hypothesis_dict['statement'])
        description: Alpha.logic_explanation text

    Returns:
        Formatted user prompt; system prompt is R5_C1_SYSTEM.
    """
    h = (hypothesis_statement or "").strip()[:2000]
    d = (description or "").strip()[:2000]
    return R5_C1_USER_TEMPLATE.format(hypothesis_statement=h, description=d)


# ---------------------------------------------------------------------------
# c₂(d, f) — description ↔ expression
# ---------------------------------------------------------------------------

R5_C2_SYSTEM = """You are a research methodology specialist verifying whether a quantitative
description faithfully corresponds to its BRAIN DSL expression.

Your task is judgement of CORRESPONDENCE, not correctness. You should answer:
"Does the expression (Section F) implement what the description (Section D) claims?"

Alignment criteria:
- Fields named in D should appear in F (e.g., D says "we use earnings_yield", F must reference
  a field name resembling earnings_yield / ey / fnd6_earnings — fuzzy ok, missing not ok)
- Operators implied by D should appear in F (e.g., D says "smoothed rolling mean", F should
  contain ts_mean and/or ts_decay_linear)
- Direction/sign in D should match F (e.g., D says "buy stocks with high X", F should not
  return high X -> negative weight; rank/zscore polarity matters)
- F can have additional wrappers (group_neutralize, winsorize) that D doesn't mention — that
  is NOT a misalignment, those are conventional T2/T3 wrappers.

Confidence: 0.9+ when correspondence is unambiguous and fields/operators are explicit.
0.5-0.7 for partial overlap (e.g., D claims an op family without naming the op). <=0.4 for
"can't determine from these inputs".

Output strict JSON. No commentary. No markdown."""


R5_C2_USER_TEMPLATE = """### Section D — Description
{description}

### Section F — Expression (BRAIN DSL)
{expression}
{operators_section}
## Task

Judge whether F faithfully implements D (correspondence only, not correctness/quality).

Output Schema (JSON):
{{
  "aligned": true | false,
  "confidence": 0.0-1.0,
  "reason": "<=1 sentence, <=200 chars, no markdown, no quotes"
}}
"""


def build_r5_c2_prompt(description: str, expression: str, operators_used: list = None) -> str:
    """Build c₂(d,f) prompt — description ↔ expression alignment.

    Args:
        description: Alpha.logic_explanation text
        expression: Alpha.expression (BRAIN DSL)
        operators_used: Optional list of operator names extracted from expression
            (helps LLM by avoiding re-parsing the DSL)
    """
    d = (description or "").strip()[:2000]
    f = (expression or "").strip()[:2000]
    ops_section = ""
    if operators_used:
        ops_section = f"\n### Operators in F\n{', '.join(operators_used[:20])}\n"
    return R5_C2_USER_TEMPLATE.format(
        description=d, expression=f, operators_section=ops_section,
    )


__all__ = [
    "R5_C1_SYSTEM", "R5_C1_USER_TEMPLATE", "build_r5_c1_prompt",
    "R5_C2_SYSTEM", "R5_C2_USER_TEMPLATE", "build_r5_c2_prompt",
]
