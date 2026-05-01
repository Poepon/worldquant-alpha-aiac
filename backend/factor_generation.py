"""LLM-guided T1 alpha generation (PR2).

Two-step pipeline:
1. select_t1_strategy_via_llm(...) → T1Strategy
   LLM picks fields (8-15) and ts_ops (5-8) and a window scale based on dataset
   context + recent T1 success patterns. ONE LLM call per round.
2. expand_t1_strategy(strategy, daily_goal, region) → List[Dict]
   Code enumerates fields × ops × windows, dedup + validate via classifier,
   stratified-sample down to daily_goal × 1.5 candidates. NO LLM call.

This replaces the legacy ALPHA_GENERATION_SYSTEM path, which had the LLM emit
strict expressions per-alpha (~5x more LLM calls and a lot of syntax-error
retries). The strategy → enumerate split keeps LLM focused on what it's actually
good at — economic intuition — and lets code handle deterministic enumeration.

Toggle via settings.T1_USE_LLM_GUIDED_STRATEGY (True by default; False routes
back to legacy generation prompts for A/B comparison).
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Dict, List, Literal, Optional

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

# Lazy imports for the strategy prompts and LLM service modules to avoid a
# circular import: backend.agents.__init__ pulls in mining_agent → graph →
# workflow → t1_nodes → factor_generation, which would otherwise loop back
# here while backend.agents is half-initialized.
from backend.factor_tier_classifier import _dedup_and_validate

# Type-only imports for IDE/type-checker hints (don't execute at module import)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from backend.agents.services.llm_service import LLMService


# =============================================================================
# T1Strategy — LLM output schema
# =============================================================================

class T1Strategy(BaseModel):
    """LLM-decided T1 signal selection strategy. Inputs: dataset/region/fields.
    Outputs: which fields × ts_ops × windows code should enumerate."""

    # Reasoning slots (LLM must fill all)
    economic_hypothesis: str = Field(
        ..., description="One-sentence economic story behind chosen signals"
    )
    signal_velocity: Literal["FUNDAMENTAL_SLOW", "FACTOR_COMPOSITE", "MEDIUM", "FAST"]
    window_scale: Literal["SHORT", "MEDIUM", "LONG"]

    # Enumeration inputs
    promising_fields: List[str] = Field(
        default_factory=list,
        description="8-15 field IDs picked from available_fields list",
    )
    preferred_ts_ops: List[
        Literal[
            "ts_rank",
            "ts_zscore",
            "ts_mean",
            "ts_std_dev",
            "ts_delta",
            "ts_delay",
            "ts_decay_linear",
            "ts_arg_max",
            "ts_arg_min",
            "ts_quantile",
            "ts_sum",
            "ts_max",
            "ts_min",
            "ts_corr",
        ]
    ] = Field(default_factory=list, description="5-8 ts_* operators matching velocity")

    rationale: str = Field("", description="2-3 sentences explaining the choice")


WINDOW_SCALE_MAP: Dict[str, List[int]] = {
    "SHORT": [5, 10],
    "MEDIUM": [20, 60],
    "LONG": [120, 240],
}


DEFAULT_T1_STRATEGY = T1Strategy(
    economic_hypothesis="generic time-series signal across mid-term windows",
    signal_velocity="MEDIUM",
    window_scale="MEDIUM",
    promising_fields=[],  # caller fills with top-coverage fields when needed
    preferred_ts_ops=["ts_rank", "ts_zscore", "ts_mean"],
    rationale="default fallback (LLM unavailable or returned invalid output)",
)


# =============================================================================
# LLM decision entry point
# =============================================================================

async def select_t1_strategy_via_llm(
    dataset_id: str,
    region: str,
    available_fields: List[Dict],
    success_patterns: Optional[List[Dict]],
    llm_service: "LLMService",
    last_round_feedback: Optional[Dict] = None,
) -> T1Strategy:
    """Run LLM to choose T1 strategy. Falls back to DEFAULT_T1_STRATEGY on
    failure (network error, JSON parse fail, schema mismatch).

    Args:
        dataset_id: Target dataset.
        region: BRAIN region.
        available_fields: From RAG_QUERY — list of {id, type, coverage, ...}.
        success_patterns: Recent T1 SUCCESS_PATTERNs (may be empty/synthesized).
        llm_service: Injected for testability.
        last_round_feedback: round_history[-1] when called from round N>1.
    """
    # Lazy-import to break the agents-package import cycle (see top-of-module note).
    from backend.agents.prompts.strategy_prompts import (
        T1_STRATEGY_SYSTEM,
        build_t1_strategy_user_prompt,
    )

    user_prompt = build_t1_strategy_user_prompt(
        dataset_id=dataset_id,
        region=region,
        available_fields=available_fields,
        success_patterns=success_patterns,
        last_round_feedback=last_round_feedback,
    )

    try:
        parsed, raw = await llm_service.call_with_schema(
            system_prompt=T1_STRATEGY_SYSTEM,
            user_prompt=user_prompt,
            schema=T1Strategy,
            temperature=0.7,
        )
    except Exception as e:
        logger.warning(f"[factor_generation] LLM call raised: {e}")
        return _fill_default_with_top_fields(available_fields)

    if parsed is None or not raw.success:
        logger.warning(
            f"[factor_generation] LLM returned no valid T1Strategy "
            f"(raw.success={getattr(raw, 'success', '?')}, error={getattr(raw, 'error', '?')})"
        )
        return _fill_default_with_top_fields(available_fields)

    # Sanity: if LLM returned empty fields, salvage with top-coverage fallback
    if not parsed.promising_fields:
        logger.info(
            f"[factor_generation] LLM returned empty promising_fields, salvaging with top-coverage"
        )
        topped = _fill_default_with_top_fields(available_fields)
        parsed = parsed.model_copy(update={"promising_fields": topped.promising_fields})

    logger.info(
        f"[factor_generation] T1Strategy selected | "
        f"velocity={parsed.signal_velocity} window={parsed.window_scale} "
        f"fields={len(parsed.promising_fields)} ops={len(parsed.preferred_ts_ops)}"
    )
    return parsed


def _fill_default_with_top_fields(available_fields: List[Dict]) -> T1Strategy:
    """Cold-start fallback: pick top-10 fields by coverage from the available list.

    Filters out categorical / group built-ins so the default doesn't crash on
    things like ts_rank(industry, 20).
    """
    from backend.alpha_semantic_validator import BUILTIN_GROUPS

    sorted_fields = sorted(
        (f for f in (available_fields or []) if (f.get("id") or "").lower() not in BUILTIN_GROUPS),
        key=lambda f: f.get("coverage", 0) or 0,
        reverse=True,
    )
    top_ids = [f["id"] for f in sorted_fields[:10] if f.get("id")]
    return DEFAULT_T1_STRATEGY.model_copy(
        update={"promising_fields": top_ids}
    )


# =============================================================================
# Programmatic expansion: strategy → concrete expressions
# =============================================================================

def expand_t1_strategy(
    strategy: T1Strategy,
    daily_goal: int,
    region: str,
    target_multiplier: float = 1.5,
) -> List[Dict]:
    """Enumerate fields × ops × windows, dedup+validate, stratified-sample down.

    Args:
        strategy: LLM-decided T1Strategy.
        daily_goal: Target PASS count for the round; produced count is
            daily_goal × target_multiplier (default 1.5).
        region: BRAIN region (passed to validator).
        target_multiplier: Over-sampling factor — 1.5x daily_goal lets the
            simulator dropout absorb low-yield candidates without the round
            ending below daily_goal.

    Returns:
        List[Dict] each with {expression, field, op, window} after dedup +
        semantic validation + tier=1 roundtrip check.
    """
    if not strategy.promising_fields or not strategy.preferred_ts_ops:
        logger.warning(
            f"[factor_generation] empty strategy — fields={len(strategy.promising_fields)} "
            f"ops={len(strategy.preferred_ts_ops)}; returning []"
        )
        return []

    windows = WINDOW_SCALE_MAP.get(strategy.window_scale, WINDOW_SCALE_MAP["MEDIUM"])
    candidates: List[Dict] = []
    for field in strategy.promising_fields:
        for op in strategy.preferred_ts_ops:
            for w in windows:
                candidates.append(
                    {
                        "expression": f"{op}({field}, {w})",
                        "field": field,
                        "op": op,
                        "window": w,
                    }
                )

    target_n = max(1, math.ceil(daily_goal * target_multiplier))
    if len(candidates) > target_n:
        candidates = stratified_sample(candidates, by="op", n=target_n)

    return _dedup_and_validate(candidates, target_tier=1, region=region)


def stratified_sample(items: List[Dict], by: str, n: int) -> List[Dict]:
    """Stratified sampling: ensures each group is represented before random fill.

    Algorithm:
      1. Bucket by `by` field.
      2. Take ⌈n / G⌉ from each bucket (G = number of groups).
      3. If we exceeded n, randomly truncate.
      4. If we under-sampled (n > Σ ceil(n/G) when buckets are uneven), fill
         the rest from the unselected pool.

    This guarantees each ts_op gets at least one shot per round, instead of
    `random.sample` accidentally picking 5 ts_rank candidates and 0 ts_zscore.
    """
    if n <= 0:
        return []
    if not items:
        return []

    groups: Dict[str, List[Dict]] = defaultdict(list)
    for it in items:
        groups[it[by]].append(it)

    per_group = math.ceil(n / max(1, len(groups)))
    out: List[Dict] = []
    for g_items in groups.values():
        bucket = list(g_items)
        random.shuffle(bucket)
        out.extend(bucket[:per_group])

    if len(out) > n:
        random.shuffle(out)
        out = out[:n]
    elif len(out) < n:
        # Fill from leftovers
        out_ids = {id(x) for x in out}
        rest = [it for it in items if id(it) not in out_ids]
        random.shuffle(rest)
        out.extend(rest[: n - len(out)])

    return out
