"""Phase 3 flat-F3: LLM-driven T2 wrapper mutation (2026-05-18).

Per master plan §4.5 flat-F3: replace T2 group_* + pure_xs full sweep
(`backend/factor_wrapping.py:expand_t2_strategy` produces 8-12 variants
of group_neutralize / group_rank / group_zscore / group_mean /
group_scale / winsorize / signed_power) with an LLM call that looks at:
  - The seed expression (T1 PASS alpha)
  - Recent _failed_tests / _brain_failed_checks from the alphas table
    (same region, same hypothesis if available)
  - Top P2-D pitfalls (KnowledgeEntry entry_type='FAILURE_PITFALL'
    meta_data['region'] = task.region)
  - Q9 Decayed Alpha seeds with overlapping operators

…and proposes 2-3 wrapper expressions (capped by LLM_MUTATE_TOP_K) with
rationale, avoiding documented failure modes.

Why: cascade T2 phase often sweeps 8-12 wrappers per seed, ~70% FAIL.
LLM-guided 2-3 wrappers (cost ~$0.01 per seed) replaces this with
informed selection. Empirical estimate (master plan §4.5): 40-75% BRAIN
sim cost reduction + higher PASS rate.

Architecture
- Pure-function module-level helpers + `async llm_mutate_alpha(seed, ...)`
  primary API. Mirrors `r5_judge.py` / `factor_wrapping.py` pattern.
- Strict-JSON output schema parsed via shared parser
- Soft-fail: on LLM exception / parse error / 0 valid variants returned,
  callers should fall back to `expand_t2_strategy`
- Cost: haiku-4-5 default, ~$0.01/call/seed at low effort

Caller is `backend/agents/graph/nodes/tier_seed.py:node_tier_seed_load`
T2 branch — gated by `settings.ENABLE_LLM_MUTATE_ALPHA`.
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

MUTATE_SYSTEM = """You are a quantitative researcher proposing wrapper transformations for
an alpha factor expression. Your task is to apply 2-3 thoughtful wrappers that
typically improve the factor's portfolio behaviour (industry neutralization,
cross-sectional ranking, robustness), AVOIDING patterns that have empirically
failed.

Allowed wrapper operations (BRAIN DSL):
  - group_neutralize(<seed>, <group>)        — industry/subindustry neutralization
  - group_rank(<seed>, <group>)              — within-group percentile rank
  - group_zscore(<seed>, <group>)            — within-group z-score
  - subtract(<seed>, group_mean(<seed>, <weight>, <group>))  — cap-weighted residualize
  - winsorize(<seed>, std=4)                 — outlier clip
  - signed_power(<seed>, 0.5)                — variance-shrink while preserving sign
  - rank(<seed>)                             — cross-sectional percentile rank
  - ts_zscore(<seed>, <window>)              — time-series z-score
  - ts_rank(<seed>, <window>)                — time-series percentile rank

Group choices (region-dependent — caller will filter):
  industry, subindustry, sector, country, exchange, market

Pick wrappers that:
  - Address documented failure modes when context provides them
  - Avoid stacking redundant transforms (e.g. don't apply both group_rank and rank)
  - Match the alpha's signal type (momentum factors benefit from winsorize +
    group_neutralize; value factors benefit from rank + group_zscore)

Output strict JSON. No commentary. No markdown."""


MUTATE_USER_TEMPLATE = """### Seed expression (T1 PASS alpha)
{seed_expression}

### Region
{region}

### Recent failure context (avoid these patterns)
{failure_context}

### Decay context (Q9 reference — avoid post-pub decayed templates if seed resembles)
{decay_context}

## Task

Propose up to {top_k} wrapper expressions for the seed. Each wrapper must:
1. Reference the seed via the placeholder `<SEED>` (do NOT inline the seed
   text — caller substitutes); e.g. `group_neutralize(<SEED>, subindustry)`.
2. Pick a `wrapper_kind` label matching the wrapping (e.g. `group_neutralize_subindustry`,
   `winsorize_std4`, `signed_power_05`, `subtract_group_mean_cap_subindustry`).
3. Include a 1-sentence `rationale` (<=120 chars) tied to the avoid-list above.

Output Schema (strict JSON):
{{
  "variants": [
    {{
      "expression": "<wrapper_op>(<SEED>, ...)",
      "wrapper_kind": "<label>",
      "rationale": "<<=120 chars>"
    }},
    ...
  ]
}}

Return at most {top_k} variants. Empty array allowed if no high-confidence pick.
"""


def build_mutate_prompt(
    seed_expression: str,
    *,
    region: str,
    failure_context: str = "",
    decay_context: str = "",
    top_k: int = 3,
) -> str:
    """Build the LLM mutate prompt user-side. System prompt is MUTATE_SYSTEM."""
    seed = (seed_expression or "").strip()[:500]
    fc = (failure_context or "").strip()[:1500] or "(no recent failures recorded)"
    dc = (decay_context or "").strip()[:800] or "(no decay-related concerns)"
    return MUTATE_USER_TEMPLATE.format(
        seed_expression=seed,
        region=region or "USA",
        failure_context=fc,
        decay_context=dc,
        top_k=max(1, min(5, int(top_k))),
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_variants(content: str, *, max_variants: int = 3) -> List[Dict[str, str]]:
    """Parse LLM strict-JSON response into a list of variant dicts.

    Each variant dict has keys: expression, wrapper_kind, rationale.
    On parse failure or unexpected shape: returns empty list (caller falls
    back to legacy sweep).
    """
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
        if not isinstance(parsed, dict):
            return []
        raw = parsed.get("variants")
        if not isinstance(raw, list):
            return []
        out: List[Dict[str, str]] = []
        for item in raw[:max_variants]:
            if not isinstance(item, dict):
                continue
            expr = str(item.get("expression", "")).strip()
            kind = str(item.get("wrapper_kind", "")).strip()
            rationale = str(item.get("rationale", ""))[:200]
            if not expr or "<SEED>" not in expr:
                # Malformed — skip silently
                continue
            out.append({
                "expression": expr,
                "wrapper_kind": kind or "llm_mutate_unspecified",
                "rationale": rationale,
            })
        return out
    except Exception as ex:
        logger.debug(f"[llm_mutate] parse failure (returning empty): {ex}")
        return []


def _substitute_seed(variant: Dict[str, str], seed: str) -> Dict[str, str]:
    """Replace <SEED> placeholder with actual seed expression."""
    expr = variant["expression"].replace("<SEED>", seed)
    return {**variant, "expression": expr}


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

async def llm_mutate_alpha(
    seed_expression: str,
    *,
    region: str,
    llm_service: LLMService,
    failure_context: str = "",
    decay_context: str = "",
    top_k: int = 3,
) -> List[Dict[str, str]]:
    """Generate up to top_k wrapper variants for the seed via LLM.

    Soft-fails to empty list on any error — caller MUST fall back to
    legacy `expand_t2_strategy` to ensure round produces variants.

    Returns: list of dicts {expression, wrapper_kind, rationale}. Each
    expression has <SEED> already substituted with the seed_expression.
    """
    if not seed_expression or not seed_expression.strip():
        logger.debug("[llm_mutate] empty seed, returning empty")
        return []

    user_prompt = build_mutate_prompt(
        seed_expression,
        region=region,
        failure_context=failure_context,
        decay_context=decay_context,
        top_k=top_k,
    )

    try:
        resp = await llm_service.call(
            system_prompt=MUTATE_SYSTEM,
            user_prompt=user_prompt,
            json_mode=True,
            max_tokens=1024,
            node_key="llm_mutate_alpha",
        )
        content = getattr(resp, "content", "") or ""
        variants = _parse_variants(content, max_variants=top_k)
        if not variants:
            logger.warning(
                f"[llm_mutate] LLM returned 0 valid variants for seed=`{seed_expression[:60]}...`"
            )
            return []
        # Substitute <SEED> placeholder
        return [_substitute_seed(v, seed_expression) for v in variants]
    except Exception as ex:
        logger.warning(
            f"[llm_mutate] LLM call failed for seed=`{seed_expression[:60]}...`: {ex}"
        )
        return []


__all__ = [
    "MUTATE_SYSTEM",
    "MUTATE_USER_TEMPLATE",
    "build_mutate_prompt",
    "llm_mutate_alpha",
]
