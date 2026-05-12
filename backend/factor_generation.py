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
            # Reconciled with DB Operator table (Time Series category) — every
            # value here MUST exist in BRAIN. ts_max / ts_min were removed
            # because they're not BRAIN ops; ts_av_diff / ts_count_nans /
            # ts_product / ts_scale / ts_step / ts_regression / ts_covariance /
            # ts_backfill added because they ARE in BRAIN and are useful T1
            # building blocks the LLM should be able to choose.
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
            "ts_corr",
            "ts_av_diff",        # 平均差 — anomaly detection
            "ts_count_nans",     # 缺失计数 — data quality signals
            "ts_product",        # 累乘 — compound effects
            "ts_scale",          # window scale — alt to zscore
            "ts_step",           # step indicator
            "ts_regression",     # 回归 — high-value for residualization
            "ts_covariance",     # 协方差 — alt to corr
            "ts_backfill",       # backfill — pre-process step
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
    selected_datasets: Optional[List[str]] = None,
    dedup_skeletons: Optional[List[str]] = None,
    explore_mode: bool = False,
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
        dedup_skeletons: Layer 1 Anti-collapse — skeletons already rejected
            by pre-simulate dedup gate this run; forwarded to prompt builder
            as a "DO NOT REGENERATE" blacklist.
        explore_mode: Layer 1 ε-greedy — when True, prompt hides RAG examples
            and instructs LLM to prioritize structural novelty.
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
        selected_datasets=selected_datasets,
        dedup_skeletons=dedup_skeletons,
        explore_mode=explore_mode,
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

    # V-22.8 (2026-05-13) — cross-dataset hallucination guard.
    # Spike on task 534/535 found LLM occasionally adds fields not in the
    # available_fields list (e.g. opt8_put_call_ratio_30d in a fundamental6
    # task, anl4_afv4_cfps_mean when the dataset was option). These hit
    # VALIDATE with "Field 'X' not found in dataset" and waste 1-2 BRAIN
    # sim slots per round (SELF_CORRECT must rewrite, drops more
    # candidates). Hard-filter LLM output to the strict subset of
    # available_fields IDs.
    if available_fields:
        available_ids = {
            (f.get("id") or f.get("name") or "").lower()
            for f in (available_fields or [])
            if isinstance(f, dict)
        }
        before = list(parsed.promising_fields)
        kept = [
            fid for fid in before if (fid or "").lower() in available_ids
        ]
        dropped = [fid for fid in before if (fid or "").lower() not in available_ids]
        if dropped:
            logger.warning(
                f"[factor_generation] V-22.8 dropped {len(dropped)} hallucinated fields "
                f"not in available_fields: {dropped[:10]}"
            )
            # If filter strips too aggressively, backfill from top-coverage.
            if len(kept) < 5:
                logger.info(
                    f"[factor_generation] V-22.8 after-hallucination-filter has only "
                    f"{len(kept)} fields, backfilling with top-coverage"
                )
                topped = _fill_default_with_top_fields(available_fields)
                # Merge: keep what LLM picked that's valid + top-coverage backfill,
                # dedup preserving order
                seen = set(fid.lower() for fid in kept)
                for fid in topped.promising_fields:
                    fl = fid.lower()
                    if fl not in seen:
                        kept.append(fid)
                        seen.add(fl)
                    if len(kept) >= 12:
                        break
            parsed = parsed.model_copy(update={"promising_fields": kept})

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
    # P1 (2026-05-07): auto ts_decay_linear wrapper config — decay-wrapped
    # variants classify as T2 (smoothing). They mine alongside raw T1 ts_op
    # candidates, with allowed_tiers={1,2} on the validate step.
    from backend.config import settings as _settings
    decay_enabled = bool(getattr(_settings, "T1_AUTO_DECAY_WRAPPER", False))
    decay_d = int(getattr(_settings, "T1_AUTO_DECAY_VALUE", 4))

    candidates: List[Dict] = []
    for field in strategy.promising_fields:
        for op in strategy.preferred_ts_ops:
            for w in windows:
                base_expr = f"{op}({field}, {w})"
                candidates.append(
                    {
                        "expression": base_expr,
                        "field": field,
                        "op": op,
                        "window": w,
                    }
                )
                if decay_enabled:
                    # ts_decay_linear smoothing usually halves turnover and
                    # boosts fitness via noise reduction (verified on close-
                    # open intraday return: fit 0.85 → 1.47, to 0.81 → 0.51).
                    candidates.append(
                        {
                            "expression": f"ts_decay_linear({base_expr}, {decay_d})",
                            "field": field,
                            # Distinct op bucket for stratified sample so
                            # decay variants don't crowd out raw ts_op.
                            "op": f"decay{decay_d}_{op}",
                            "window": w,
                        }
                    )

    # Plan v5+ #2 (2026-05-07) — append field-pair candidates from the
    # interaction graph. These are role-classified two-field combinations
    # (PE / PB / accruals / intraday range / synthetic returns / etc.)
    # that expand the static 15-pattern Quasi-T1 white-list to whatever
    # financially-meaningful pairs the current region's fields support.
    # Each generated expression must independently classify as Quasi-T1
    # via the new structural patterns added to _QUASI_T1_PATTERNS.
    try:
        from backend.agents.seed_pool.field_interactions import (
            generate_pair_candidates,
        )
        pairs = generate_pair_candidates(
            available_fields=strategy.promising_fields,
            region=region,
            max_per_template=1,
        )
        for p in pairs:
            candidates.append({
                "expression": p["expression"],
                # Encoded for stratified sample diversity (treat each
                # template as its own "op" bucket so single-field ts_op
                # candidates don't crowd them out)
                "field": "_pair_" + ",".join(p["field_pair"]),
                "op": f"pair_{p['template_id']}",
                "window": 0,
            })
        if pairs:
            logger.info(
                f"[factor_generation] #2 field-pair candidates appended: "
                f"{len(pairs)} from {len(strategy.promising_fields)} fields"
            )
    except Exception as _pair_e:
        logger.warning(
            f"[factor_generation] #2 pair generation failed (non-fatal): {_pair_e}"
        )

    # V-22.6 (2026-05-12) — composite-field T1 candidates. Mining rounds 16-20
    # produced 100% single-field alphas; high-Δscore variants got blocked by
    # OS self-corr because the alpha pool is dominated by returns-reversal.
    # Composites synthesize multi-field signals (PE / accrual / intraday range
    # / overnight gap / ...) BEFORE ts_op, breaking the returns-only monoculture.
    if bool(getattr(_settings, "COMPOSITE_T1_ENABLED", False)):
        try:
            from backend.agents.seed_pool.composite_fields import (
                generate_composite_t1_candidates,
            )
            composites = generate_composite_t1_candidates(
                ts_ops=strategy.preferred_ts_ops,
                windows=windows,
                available_fields=strategy.promising_fields,
                region=region,
                max_per_composite=int(
                    getattr(_settings, "COMPOSITE_T1_MAX_PER_COMPOSITE", 2)
                ),
                apply_preprocess=bool(
                    getattr(_settings, "COMPOSITE_T1_APPLY_PREPROCESS", False)
                ),
                backfill_window=int(
                    getattr(_settings, "COMPOSITE_T1_BACKFILL_WINDOW", 120)
                ),
                winsorize_std=int(
                    getattr(_settings, "COMPOSITE_T1_WINSORIZE_STD", 4)
                ),
                apply_decay_wrapper=bool(
                    getattr(_settings, "COMPOSITE_T1_AUTO_DECAY_WRAPPER", False)
                ),
                decay_value=int(
                    getattr(_settings, "COMPOSITE_T1_AUTO_DECAY_VALUE", 4)
                ),
            )
            candidates.extend(composites)
            if composites:
                logger.info(
                    f"[factor_generation] V-22.6 composite candidates appended: "
                    f"{len(composites)}"
                )
        except Exception as _comp_e:
            logger.warning(
                f"[factor_generation] V-22.6 composite generation failed "
                f"(non-fatal): {_comp_e}"
            )

    target_n = max(1, math.ceil(daily_goal * target_multiplier))

    # V-22.6.5 (2026-05-12) — Reserved composite quota in the final pool.
    # Before this, stratified_sample averaged composite candidates against
    # raw_t1 / decay-twin / pair buckets, leaving composites at ~21% of the
    # final pool. Spike on V-22.6.4 verification rounds saw 5 fundamental6
    # mining rounds yield 0 fund-composite alphas saved (expected 0.96/round
    # × ~10% PASS = 0.5 in 5 rounds, hit lower bound). Reserving a fraction
    # of target_n for composite candidates raises the per-round expectation
    # 2-3x without doubling BRAIN sim cost.
    composite_quota_pct = float(
        getattr(_settings, "COMPOSITE_T1_FINAL_POOL_QUOTA_PCT", 0.33)
    )
    composite_candidates = [
        c for c in candidates if c.get("field", "").startswith("_composite_")
    ]
    non_composite = [
        c for c in candidates if not c.get("field", "").startswith("_composite_")
    ]
    if composite_candidates and composite_quota_pct > 0:
        # Floor 1, cap at min(quota, len(available composites))
        composite_quota = min(
            len(composite_candidates),
            max(1, math.ceil(target_n * composite_quota_pct)),
        )
        non_comp_quota = max(1, target_n - composite_quota)

        if len(composite_candidates) > composite_quota:
            composite_candidates = stratified_sample(
                composite_candidates, by="op", n=composite_quota,
            )
        if len(non_composite) > non_comp_quota:
            non_composite = stratified_sample(
                non_composite, by="op", n=non_comp_quota,
            )
        candidates = composite_candidates + non_composite
        logger.info(
            f"[factor_generation] V-22.6.5 final pool: "
            f"composite={len(composite_candidates)} (quota={composite_quota}) + "
            f"non_composite={len(non_composite)} = {len(candidates)}"
        )
    elif len(candidates) > target_n:
        # Legacy path when no composites OR quota disabled
        candidates = stratified_sample(candidates, by="op", n=target_n)

    # P1 (2026-05-07): when decay wrapping is enabled, T2-classified twins
    # need to pass the validator's tier check too. allowed_tiers={1,2}.
    allowed = {1, 2} if decay_enabled else {1}
    return _dedup_and_validate(
        candidates, target_tier=1, region=region, allowed_tiers=allowed,
    )


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
