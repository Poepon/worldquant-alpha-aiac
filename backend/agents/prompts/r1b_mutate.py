"""Phase 3 R1b.2a: mutate prompt template for HYPOTHESIS_MUTATE node.

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §4.3.

When R1a / R5 attribution flags the FAILURE as HYPOTHESIS (the idea is
wrong, not the implementation), the mutate node asks the LLM to propose
a REVISED hypothesis that addresses the empirical failure mode while
staying close to the original investment theme.

Per [V1.1-A1-3]: feed the failure_tree summary so the LLM doesn't
re-propose hypotheses that already failed in this family. Per [V1.2-A2-2]
include the R5 c1 alignment reason (hypothesis↔description LLM diagnosis)
as a 1-step CoSTEER reflection — the LLM already analyzed WHY the
hypothesis was misaligned, reuse that signal.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


R1B_MUTATE_SYSTEM = """You are a quantitative researcher revising an investment
hypothesis. The original hypothesis was tested via multiple alpha expressions
and ALL FAILED with quality issues that R5 judge / R1a heuristic attributed
to the HYPOTHESIS being wrong (the idea, not the implementation). Your job:
propose a REVISED hypothesis that addresses the specific empirical failure
mode WITHIN THE SAME pillar as the original hypothesis.

Rules:
- Output a strictly NEW hypothesis (not a paraphrase).
- The new hypothesis must be testable as a BRAIN alpha expression.
- The new hypothesis must explicitly call out what changed from the original
  AND WHY that addresses the observed failure pattern.
- Avoid resurrecting hypotheses already in the failure tree (provided).
- Output strict JSON, no markdown, no commentary.

PILLAR PRESERVATION (HARD CONSTRAINT):
- You MUST keep the `pillar` field == original pillar. The canonical pillars
  are: momentum, value, quality, volatility, sentiment, other.
  (Note: "mean_reversion" / "reversal" are short-horizon momentum sub-classes
  and map to pillar=momentum; do NOT emit them as a separate pillar.)
- Mutation MAY change signal source (which field), time horizon (window size),
  neutralization (group / industry / subindustry), or weighting (rank / zscore /
  decay) WITHIN the pillar.
- Mutation MUST NOT cross pillars. Example: if the original pillar is
  "momentum", you MAY revise to a different lookback or a different
  PV/return field, but you MUST NOT pivot to a valuation ratio (value),
  a profitability metric (quality), an analyst-sentiment field (sentiment),
  or a dispersion measure (volatility).
- Pillar boundaries underpin downstream diversity tracking and family caps;
  cross-pillar drift mid-cycle breaks those stats."""


R1B_MUTATE_USER_TEMPLATE = """### Original hypothesis (FAILED)
{original_hypothesis}

### Original alphas tested (and their outcomes)
{original_alpha_outcomes_bulleted}

### R5 c1 alignment diagnosis (hypothesis vs description)
{r5_c1_reason}

### Recent failure tree (this hypothesis family)
{failure_tree_summary}

### Region / dataset / pillar context
region={region}, dataset={dataset_id}, pillar={pillar}

### Output (strict JSON, no markdown)
{{
  "new_hypothesis": {{
    "statement": "<revised hypothesis>",
    "rationale": "<economic reasoning>",
    "expected_signal": "momentum|mean_reversion|value|quality|other",
    "pillar": "{pillar}",
    "key_fields": ["field1", "field2"],
    "suggested_operators": ["op1", "op2"]
  }},
  "diff_from_original": "<1-sentence what changed and why>",
  "addresses_failure_modes": ["<failure_mode_1>", "<failure_mode_2>"]
}}

NOTE: `pillar` field above MUST equal the original pillar ({pillar}). Any
cross-pillar mutation will be rejected and the original hypothesis kept."""


def build_r1b_mutate_prompt(
    *,
    original_hypothesis: str,
    original_alpha_outcomes: List[Dict[str, Any]],
    r5_c1_reason: str,
    failure_tree_summary: str = "",
    region: str = "USA",
    dataset_id: str = "",
    pillar: str = "",
    max_outcomes_in_prompt: int = 8,
) -> Tuple[str, str]:
    """Compose ``(system_prompt, user_prompt)`` for ``node_hypothesis_mutate``.

    All inputs defensive — missing / None values render as placeholders so
    the prompt is always well-formed.
    """
    outcome_lines: List[str] = []
    for o in (original_alpha_outcomes or [])[:max_outcomes_in_prompt]:
        expr = str((o or {}).get("expression", "")).strip()
        sharpe = (o or {}).get("sharpe")
        fitness = (o or {}).get("fitness")
        if expr:
            outcome_lines.append(
                f"- expr={expr!r} sharpe={sharpe!r} fitness={fitness!r}"
            )
    if not outcome_lines:
        outcome_lines.append("- (no alpha outcomes recorded)")

    user_prompt = R1B_MUTATE_USER_TEMPLATE.format(
        original_hypothesis=(original_hypothesis or "").strip() or "(no hypothesis recorded)",
        original_alpha_outcomes_bulleted="\n".join(outcome_lines),
        r5_c1_reason=(r5_c1_reason or "").strip() or "(no R5 c1 reason recorded — R5 may be OFF)",
        failure_tree_summary=(failure_tree_summary or "").strip() or "(no prior failures in this family)",
        region=region or "USA",
        dataset_id=dataset_id or "(unspecified)",
        pillar=pillar or "(unclassified)",
    )
    return R1B_MUTATE_SYSTEM, user_prompt


__all__ = [
    "R1B_MUTATE_SYSTEM",
    "R1B_MUTATE_USER_TEMPLATE",
    "build_r1b_mutate_prompt",
]
