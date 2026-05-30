"""G5 Phase A: LLM-driven trajectory crossover (2026-05-19).

QuantaAlpha arxiv 2602.07085 (2026-02) trajectory mutation/crossover.
While flat-F3 ``llm_mutate_alpha`` does single-seed wrapper enhancement, G5
combines TWO high-reward PASS alpha "siblings" into hybrid offspring —
"combine the strengths of A and B" prompt pattern.

Architecture
- Pure-function module-level helpers + ``async llm_crossover_alpha(...)``
  primary API. Mirrors llm_mutate_alpha.py shape so callers can swap.
- Strict-JSON output schema (no markdown, no commentary)
- Soft-fail: on LLM exception / parse error / 0 valid offspring →
  caller falls back to no-op (round still produces alphas via normal
  hypothesis → code_gen pipeline)
- Per [[feedback_light_wiring_deferred_gate]] this is Phase A — offspring
  persist into ``task.config["g5_pending_offspring"]`` for next-round
  consumption (R1b.2-v2 same mechanism), then enter validate→simulate→
  evaluate→save_results via node_code_gen prepend.

Gated by ``settings.ENABLE_G5_CROSSOVER``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from backend.agents.services.llm_service import LLMService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CROSSOVER_SYSTEM = """You are a quantitative researcher combining two PASS alpha factor
expressions into hybrid offspring. Both parents have been BRAIN-simulated and
PASSED the quality gates (Sharpe ≥ 1.25, Fitness ≥ 1.0, Turnover 1-70%); your
task is to synthesise offspring that inherit each parent's signal strength
while attempting to dampen its weakness.

Combination strategies (pick the one that fits the parent pair):
  1. **WEIGHTED SUM** — `add(multiply(<A>, 0.6), multiply(<B>, 0.4))` when
     both signals have similar magnitude and you want to blend them.
  2. **SEQUENTIAL FILTER** — `trade_when(<A_condition>, <B>, ...)` when A
     gives a regime/timing signal and B is the asset selector (or v.v.).
  3. **CROSS-SECTIONAL CONFIRM** — `multiply(rank(<A>), rank(<B>))` —
     amplifies stocks where BOTH signals agree, dampens disagreers.
  4. **WRAPPER GRAFT** — pick A's signal core and B's wrapper (e.g.
     B uses `group_neutralize(..., subindustry)`, apply same wrapper to A).
  5. **DIFFERENCE FILTER** — `subtract(<A>, multiply(<B>, ts_corr(<A>, <B>, 60)))` —
     orthogonalise A against the part of itself correlated with B (only when
     the parents share a known correlation source you can name in rationale).

Rules:
  - Use placeholders `<A>` and `<B>` — caller substitutes the parent
    expressions verbatim.
  - Each offspring MUST reference BOTH `<A>` and `<B>` (no degenerate single-
    parent expression — that defeats the point of crossover).
  - Avoid stacking redundant wrappers already present in either parent
    (e.g. don't apply rank() to a parent that's already a rank).
  - Each offspring MUST be a distinct combination strategy.

Output strict JSON. No markdown."""


CROSSOVER_USER_TEMPLATE = """### Parent A (PASS)
Expression: {parent_a_expression}
Metrics: sharpe={parent_a_sharpe}, fitness={parent_a_fitness}, turnover={parent_a_turnover}
Pillar: {parent_a_pillar}

### Parent B (PASS)
Expression: {parent_b_expression}
Metrics: sharpe={parent_b_sharpe}, fitness={parent_b_fitness}, turnover={parent_b_turnover}
Pillar: {parent_b_pillar}

### Region
{region}

## Task

Propose up to {top_k} hybrid offspring combining A and B. Each offspring:
1. Reference both `<A>` and `<B>` placeholders (caller substitutes)
2. Pick a `combination_strategy` label from the 5 strategies above
3. Include 1-sentence `rationale` (<=140 chars) — name the weakness of one
   parent the offspring addresses with the other's strength

Output Schema (strict JSON):
{{
  "offspring": [
    {{
      "expression": "<combination>(<A>, <B>, ...)",
      "combination_strategy": "<weighted_sum|sequential_filter|cross_sectional_confirm|wrapper_graft|difference_filter>",
      "rationale": "<<=140 chars>"
    }},
    ...
  ]
}}

Return at most {top_k} offspring. Empty array allowed if A and B are too
similar / orthogonal in incompatible ways. Each offspring MUST be a distinct
expression.
"""


def build_crossover_prompt(
    parent_a_expression: str,
    parent_b_expression: str,
    *,
    parent_a_metrics: Optional[Dict[str, Any]] = None,
    parent_b_metrics: Optional[Dict[str, Any]] = None,
    parent_a_pillar: Optional[str] = None,
    parent_b_pillar: Optional[str] = None,
    region: str = "USA",
    top_k: int = 2,
) -> str:
    """Build the LLM crossover prompt user-side. System prompt is CROSSOVER_SYSTEM."""
    a_m = parent_a_metrics or {}
    b_m = parent_b_metrics or {}

    def _fmt(v):
        try:
            return f"{float(v):.3f}"
        except (TypeError, ValueError):
            return "?"

    return CROSSOVER_USER_TEMPLATE.format(
        parent_a_expression=(parent_a_expression or "").strip()[:400],
        parent_a_sharpe=_fmt(a_m.get("sharpe")),
        parent_a_fitness=_fmt(a_m.get("fitness")),
        parent_a_turnover=_fmt(a_m.get("turnover")),
        parent_a_pillar=parent_a_pillar or "?",
        parent_b_expression=(parent_b_expression or "").strip()[:400],
        parent_b_sharpe=_fmt(b_m.get("sharpe")),
        parent_b_fitness=_fmt(b_m.get("fitness")),
        parent_b_turnover=_fmt(b_m.get("turnover")),
        parent_b_pillar=parent_b_pillar or "?",
        region=region or "USA",
        top_k=max(1, min(3, int(top_k))),
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_offspring(content: str, *, max_offspring: int = 2) -> List[Dict[str, str]]:
    """Parse LLM strict-JSON response into a list of offspring dicts.

    Each dict has keys: expression, combination_strategy, rationale.
    Drops malformed entries (missing <A> or <B> placeholder, duplicates).
    On parse failure returns empty list (caller no-ops, round unaffected).
    """
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
        if not isinstance(parsed, dict):
            return []
        raw = parsed.get("offspring")
        if not isinstance(raw, list):
            return []
        out: List[Dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if len(out) >= max_offspring:
                break
            if not isinstance(item, dict):
                continue
            expr = str(item.get("expression", "")).strip()
            strat = str(item.get("combination_strategy", "")).strip()
            rationale = str(item.get("rationale", ""))[:200]
            # Both placeholders required — degenerate single-parent offspring
            # would defeat crossover semantics.
            if not expr or "<A>" not in expr or "<B>" not in expr:
                continue
            if expr in seen:
                logger.debug(f"[llm_crossover] dropping duplicate offspring: {expr[:80]}")
                continue
            seen.add(expr)
            out.append({
                "expression": expr,
                "combination_strategy": strat or "llm_crossover_unspecified",
                "rationale": rationale,
            })
        return out
    except Exception as ex:
        logger.debug(f"[llm_crossover] parse failure (returning empty): {ex}")
        return []


def _substitute_parents(
    offspring: Dict[str, str],
    parent_a_expr: str,
    parent_b_expr: str,
) -> Dict[str, str]:
    """Replace <A> and <B> placeholders with actual parent expressions."""
    expr = offspring["expression"].replace("<A>", parent_a_expr).replace("<B>", parent_b_expr)
    return {**offspring, "expression": expr}


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------


async def llm_crossover_alpha(
    parent_a_expression: str,
    parent_b_expression: str,
    *,
    region: str,
    llm_service: LLMService,
    parent_a_metrics: Optional[Dict[str, Any]] = None,
    parent_b_metrics: Optional[Dict[str, Any]] = None,
    parent_a_pillar: Optional[str] = None,
    parent_b_pillar: Optional[str] = None,
    top_k: int = 2,
) -> List[Dict[str, str]]:
    """Generate up to top_k hybrid offspring combining 2 PASS parents via LLM.

    Soft-fails to empty list on any error — caller MUST handle empty output
    as "no crossover this round" (round still produces alphas via the normal
    hypothesis → code_gen path).

    Returns: list of dicts {expression, combination_strategy, rationale}.
    Each expression has <A> and <B> already substituted with the parents.
    """
    if not parent_a_expression or not parent_a_expression.strip():
        return []
    if not parent_b_expression or not parent_b_expression.strip():
        return []
    if parent_a_expression.strip() == parent_b_expression.strip():
        logger.debug("[llm_crossover] identical parents, returning empty")
        return []

    user_prompt = build_crossover_prompt(
        parent_a_expression,
        parent_b_expression,
        parent_a_metrics=parent_a_metrics,
        parent_b_metrics=parent_b_metrics,
        parent_a_pillar=parent_a_pillar,
        parent_b_pillar=parent_b_pillar,
        region=region,
        top_k=top_k,
    )

    # PR3 review (2026-05-31): the old swap of llm_service.model to
    # LLM_CROSSOVER_MODEL is retired — same as llm_mutate_alpha (PR2). It mutated
    # the shared process singleton (not concurrency-safe → a concurrent coroutine
    # reads the swapped model) and, once per-call routing is ON, was dead anyway
    # (call() prefers the node_key-routed eff_model over self.model → two
    # conflicting selection mechanisms on one node). Model selection for this
    # block now flows SOLELY through node_key="llm_crossover_alpha": flag ON picks
    # the routed model (LLM_FUNCTION_MODEL_MAP), flag OFF uses the service default
    # (legacy LLM_CROSSOVER_MODEL is no longer honored — set the routing map
    # instead). resp.model carries the effective model for the G5 log label.
    try:
        resp = await llm_service.call(
            system_prompt=CROSSOVER_SYSTEM,
            user_prompt=user_prompt,
            json_mode=True,
            max_tokens=1024,
            node_key="llm_crossover_alpha",
        )
        content = getattr(resp, "content", "") or ""
        offspring = _parse_offspring(content, max_offspring=top_k)
        if not offspring:
            logger.warning(
                f"[llm_crossover] LLM returned 0 valid offspring for parents "
                f"A=`{parent_a_expression[:60]}...` B=`{parent_b_expression[:60]}...`"
            )
            return []
        return [_substitute_parents(o, parent_a_expression, parent_b_expression) for o in offspring]
    except Exception as ex:
        logger.warning(f"[llm_crossover] LLM call failed (non-fatal): {ex}")
        return []


__all__ = [
    "CROSSOVER_SYSTEM",
    "CROSSOVER_USER_TEMPLATE",
    "build_crossover_prompt",
    "llm_crossover_alpha",
]
