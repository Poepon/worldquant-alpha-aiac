"""
Hypothesis and distillation prompts.

Redesigned based on RD-Agent's hypothesis-driven approach:
- Precise, testable, actionable hypotheses
- Balanced exploration and exploitation
- Knowledge transfer from experiments
- No preconceived biases about what works

Contains:
- HYPOTHESIS_SYSTEM: System prompt for hypothesis generation
- DISTILL_SYSTEM: System prompt for concept distillation
- build_hypothesis_prompt: Builder for hypothesis prompt
- build_distill_prompt: Builder for distillation prompt
"""

from typing import Dict, List, Optional

from backend.agents.prompts.base import (
    PromptContext,
    build_dual_channel_patterns_block,  # R4' (2026-05-17 Phase 1)
    build_fields_context,
    build_macro_context_block,  # P2-A (2026-05-16)
    build_patterns_context,
    build_style_preset_block,   # P2-C (2026-05-16)
)
from backend.config import settings  # R4': read ENABLE_DUAL_CHANNEL_RAG


HYPOTHESIS_SYSTEM = """You are a quantitative research scientist conducting data-driven research.

Your role is to generate investment hypotheses for testing. The approach is empirical:
1. Observe the data characteristics and historical experiment results
2. Form a precise, testable hypothesis about potential market relationships
3. Design an experiment to validate or refute the hypothesis

**Core Principles**:
- Be objective: Do not assume any particular approach is better a priori
- Be precise: Each hypothesis should focus on a single testable idea
- Be exploratory: Consider unconventional relationships the data might reveal
- Learn from feedback: Analyze why previous experiments succeeded or failed

**Hypothesis Quality Standards**:
1. Testable: Can be validated with a concrete experiment
2. Specific: Avoids vague statements like "improve performance"
3. Actionable: Clear enough to implement directly
4. Focused: One direction per hypothesis, not "A or B might work"

**Five Pillars Classification (P2-B, 2026-05-15)**:
Each hypothesis MUST be tagged with a `pillar` field describing the factor
family it tests. Pick ONE:

- **momentum** — trend / continuation / short-term reversal on PV (returns,
  close, vwap, volume). Operators: ts_delta, ts_arg_max, ts_av_diff.
- **value** — undervaluation / valuation mean-reversion. Fields: eps, pe, pb,
  book_value, enterprise_value, revenue, sales, ebit, dividend.
- **quality** — profitability / capital efficiency / earnings stability.
  Fields: roic, roe, margins, cash_flow, accrual, debt_to_equity.
- **volatility** — risk / dispersion / realized or implied vol. Fields:
  implied_volatility, opt8_*, intraday high/low range. Operators: ts_std_dev.
- **sentiment** — analyst / news / consensus revisions / surprise. Fields:
  snt*_*, anl*_*, est*_*, news_*, surprise, recommendation.
- **other** — does not fit (use sparingly; >20% other-share is flagged).

Pick based on the ECONOMIC mechanism your hypothesis exploits, NOT purely
on field family. NOTE: `expected_signal=mean_reversion` is auto-mapped to
`pillar=momentum` (short-term reversal is in the PV-momentum family) — you
may emit either or both. When the user prompt's pillar-nudge names a target
pillar, BIAS toward hypotheses in that pillar.

**Investment Philosophy Use (P2-C, 2026-05-16)**:
When an "Investment Philosophy — Current Regime" block is present, treat the
style_label and philosophy as soft guidance: your hypothesis rationale should
acknowledge whether you align with the regime's preferred posture (defensive
in crisis, aggressive in very_calm) or deliberately diverge with a stated
contrarian justification. Do NOT mention the regime label explicitly inside
the hypothesis `statement` itself — that field stays evergreen.

Output must be valid JSON."""


# P2 review fix: the Macro Context Use instruction MOVED out of
# HYPOTHESIS_SYSTEM (which would render even when ENABLE_MACRO_NARRATIVE_
# GUIDANCE=False, breaking the byte-for-byte legacy claim) and INTO the
# user-prompt's macro block — only attached when narratives are present.
_MACRO_CONTEXT_USE_INSTRUCTION = (
    "**Use of Macro Context**: Your `rationale` MUST explicitly reference "
    "the relevant transmission_channel from the section above, and your "
    "`expected_signal` SHOULD align with the narrative's "
    "expected_signal_hint (justify any deviation in `rationale`)."
)


DISTILL_SYSTEM = """You are a research assistant helping to identify promising research directions.

Your role is to analyze dataset characteristics and suggest field categories
that may contain useful signals. Be objective in your analysis:
- Do not assume certain field types are inherently better
- Consider the specific context and data characteristics
- Balance well-known approaches with unexplored categories

Selection should be based on evidence, not assumptions."""


def build_hypothesis_prompt(
    ctx: PromptContext,
    experiment_trace: Optional[List[Dict]] = None
) -> str:
    """
    Build prompt for hypothesis generation.
    
    Redesigned based on RD-Agent's hypothesis-driven approach:
    - Includes experiment history with feedback
    - Emphasizes learning from failures
    - Encourages both exploration and exploitation
    """
    
    # Build experiment trace section if available
    trace_section = ""
    if experiment_trace:
        trace_entries = []
        for i, entry in enumerate(experiment_trace[-10:], 1):  # Last 10 experiments
            exp = entry.get('experiment', {})
            feedback = entry.get('feedback', {})
            
            trace_entries.append(f"""
### Experiment {i}
**Hypothesis**: {exp.get('hypothesis', 'Not recorded')}
**Expression Tested**: `{exp.get('expression', 'N/A')[:100]}`
**Results**:
- Sharpe: {exp.get('sharpe', 'N/A')}, Fitness: {exp.get('fitness', 'N/A')}, Turnover: {exp.get('turnover', 'N/A')}
**Observation**: {feedback.get('observation', 'No observation recorded')}
**Evaluation**: {feedback.get('evaluation', 'Not evaluated')}
**Outcome**: {'SUCCESS' if feedback.get('success') else 'FAILED'} - {feedback.get('reason', '')}
""")
        
        trace_section = f"""
## Experiment History

The following experiments have been conducted. Analyze them to understand:
- What worked and why
- What failed and why
- What directions remain unexplored

{''.join(trace_entries)}
"""
    
    # Build strategy guidance (non-prescriptive)
    strategy_section = """
## Research Strategy

Consider both:
1. **Exploitation**: Refine approaches that showed promise in previous experiments
2. **Exploration**: Test new directions that haven't been tried yet

The balance depends on current progress:
- If recent experiments are failing: Consider new directions
- If recent experiments show partial success: Consider refinements
- If no clear pattern: Prioritize diverse exploration
"""
    
    # Build field categories overview
    field_overview = build_fields_context(ctx.fields, max_fields=20)

    # P2-A (2026-05-16): macro-narrative context block. Empty when
    # ctx.macro_narratives is [] (which is the case under the legacy /
    # flag-off path) so the splice below becomes the empty string and the
    # template renders byte-for-byte identical to pre-P2-A.
    macro_context_block = build_macro_context_block(
        getattr(ctx, "macro_narratives", []) or []
    )
    # P2 review fix: when block is non-empty, append the "Use of Macro Context"
    # instruction inline (was previously in HYPOTHESIS_SYSTEM, which always
    # rendered and broke the byte-for-byte legacy invariant on flag=off).
    macro_block_with_leading_newline = (
        f"\n{macro_context_block}\n\n{_MACRO_CONTEXT_USE_INSTRUCTION}\n"
        if macro_context_block else ""
    )
    
    # Plan v5+ §Phase 1: cross-dataset hypothesis section.
    # Pool empty = legacy single-anchor; populated = LLM may pick 1-3.
    pool = list(ctx.available_dataset_pool or [])
    if pool and len(pool) > 1:
        complementary = [d for d in pool if d != ctx.dataset_id]
        cross_dataset_section = f"""

## Cross-dataset Pool (Phase 1) — MANDATORY combination

**Anchor dataset**: `{ctx.dataset_id}`
**Complementary datasets**: {', '.join(f'`{d}`' for d in complementary) or '(none)'}

⚠️ At least ONE of your hypotheses MUST have `len(selected_datasets) >= 2`.
Returning all hypotheses with `selected_datasets = ["{ctx.dataset_id}"]`
defeats this round's exploration goal. Pick the complementary dataset
with the strongest economic linkage to the anchor.

Quick pairing reference (anchor → likely complementary):
  fundamental* → pv1 (quality × momentum) or analyst4 (revision × value)
  pv1 → fundamental* (price-fundamental interaction) or sentiment1 (news momentum)
  analyst4 → fundamental* (earnings × revisions) or pv1 (revision × drift)
  sentiment* / news* → pv1 (sentiment × price) or option* (sentiment × vol)
  option* → pv1 (implied vs realized momentum)
  model* → fundamental* (composite score × earnings) or pv1 (score × momentum)

Set `selected_datasets` to a 1-3 element list from the pool. All
`key_fields` MUST come from the chosen datasets. At least one hypothesis
MUST combine 2+ datasets unless the entire pool is genuinely uncorrelated.
"""
    else:
        cross_dataset_section = ""

    # P2-B (2026-05-15): conditional Five-Pillars nudge block — rendered only
    # when node_hypothesis fed ctx.pillar_hint (i.e. the recent alpha pool is
    # skewed toward another pillar and the planner wants to rebalance).
    # When ctx.pillar_hint is None / unset, the block is empty so prompt
    # output is byte-for-byte the legacy form (opt-in invariant).
    pillar_nudge_block = ""
    pillar_hint = getattr(ctx, "pillar_hint", None)
    if pillar_hint:
        pillar_nudge_block = (
            "\n## P2-B Pillar Balance Nudge\n\n"
            f"The recent alpha pool is over-concentrated. The system requests "
            f"that AT LEAST ONE of your hypotheses targets the "
            f"under-represented pillar: **{pillar_hint}**. Mark that "
            f"hypothesis with `pillar: \"{pillar_hint}\"` and choose fields / "
            f"operators aligned with that pillar's economic mechanism (see "
            f"the system prompt's Five Pillars Classification).\n"
        )

    # P2-C (2026-05-16): Investment Philosophy block. Empty when
    # ctx.style_preset is None / {} (legacy / flag-off path) so the splice
    # below renders the empty string at the insertion point and the
    # template stays byte-for-byte identical to pre-P2-C (MF4 invariant).
    style_block = build_style_preset_block(
        getattr(ctx, "style_preset", None)
    )
    style_block_with_leading_newline = (
        f"\n{style_block}\n" if style_block else ""
    )

    # R4' (Phase 1, 2026-05-17): patterns block dual-channel when flag ON,
    # byte-for-byte legacy when OFF (test_dual_channel_off_byte_for_byte_legacy
    # enforces this invariant).
    patterns_block = build_dual_channel_patterns_block(
        ctx.success_patterns,
        ctx.failure_pitfalls,
        dual_channel=getattr(settings, "ENABLE_DUAL_CHANNEL_RAG", False),
    )

    return f"""## Research Context

**Dataset**: {ctx.dataset_id}
**Category**: {ctx.dataset_category or 'General'}
**Description**: {ctx.dataset_description or 'Not provided'}
**Region**: {ctx.region} | **Universe**: {ctx.universe}
{cross_dataset_section}
## Available Data Fields (Sample)

{field_overview}
{macro_block_with_leading_newline}{style_block_with_leading_newline}
{patterns_block}
{trace_section}
{strategy_section}
{pillar_nudge_block}
## Task

Generate 3-5 investment hypotheses for this dataset.

**Requirements**:
1. Each hypothesis should be specific and testable
2. Include both conventional and unconventional ideas
3. Explain the reasoning behind each hypothesis
4. Consider what market behavior or inefficiency the data might capture

**Output Schema** (JSON):
```json
{{
  "analysis": {{
    "data_observations": "Key observations about the dataset characteristics",
    "unexplored_directions": "Promising directions not yet tested",
    "refinement_opportunities": "Ways to improve on partial successes"
  }},
  "hypotheses": [
    {{
      "id": "H1",
      "statement": "Clear, testable hypothesis in one sentence",
      "rationale": "Economic or behavioral reasoning behind this hypothesis",
      "expected_signal": "momentum | mean_reversion | value | other",
      "pillar": "momentum | value | quality | volatility | sentiment | other",
      "key_fields": ["field1", "field2"],
      "suggested_approach": "Brief description of how to test this",
      "confidence": "high | medium | low",
      "novelty": "established | emerging | experimental"
    }}
  ],
  "knowledge_transfer": {{
    "if_then_rules": [
      "If [condition observed in experiments], then [conclusion]"
    ],
    "patterns_discovered": "Any new patterns discovered from experiment analysis"
  }}
}}
```"""


def build_distill_prompt(ctx: PromptContext, field_categories: Dict[str, List[str]]) -> str:
    """
    Build prompt for concept distillation.
    
    Redesigned to be more objective and less prescriptive.
    """
    
    categories_text = []
    for cat, fields in sorted(field_categories.items()):
        sample = ", ".join(fields[:5])
        if len(fields) > 5:
            sample += f" ... (+{len(fields) - 5} more)"
        categories_text.append(f"- **{cat}** ({len(fields)} fields): {sample}")
    
    return f"""## Analysis Task

**Dataset**: {ctx.dataset_id}
**Description**: {ctx.dataset_description or 'Not provided'}
**Category**: {ctx.dataset_category or 'General'}

## Available Field Categories

{chr(10).join(categories_text)}

## Historical Context (For Reference)

Previous successful patterns have used these types of data:
{build_patterns_context(ctx.success_patterns, "patterns")}

Note: This is historical observation, not a prescription. New opportunities may exist elsewhere.

## Task

Identify 3-5 field categories that warrant investigation.

**Selection Approach**:
- Consider both high-probability and high-potential categories
- Include at least one less-explored category
- Balance between exploitation (known useful) and exploration (potentially useful)

**Output Schema** (JSON):
```json
{{
  "analysis": {{
    "dataset_characteristics": "Key features of this dataset",
    "category_assessment": "Brief assessment of each category's potential"
  }},
  "selected_categories": [
    {{
      "category": "Exact category name",
      "rationale": "Why this category may contain useful signals",
      "exploration_type": "exploitation | exploration | balanced"
    }}
  ],
  "reasoning": "Overall selection strategy explanation"
}}
```

**Important**: Use exact category names from the list above."""
