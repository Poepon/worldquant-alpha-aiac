"""LLM-guided T2 / T3 alpha wrapping (PR2).

Two parallel pipelines, both following the same shape as factor_generation.T1:

T2 (cross-sectional / smoothing wrappers around a T1 PASS seed):
  select_t2_strategy_via_llm(seed, ...) → T2Strategy
  expand_t2_strategy(seed, strategy, region) → List[Dict]  # ~8-12 variants

T3 (trade_when entry-filter wrappers around a T2 PASS seed):
  select_t3_strategy_via_llm(seed, ...) → T3Strategy
  expand_t3_strategy(seed, strategy, region) → List[Dict]  # ~3-5 variants

Why "LLM picks strategy, code enumerates": the wrapper space (5 group ops × 4
group choices + 6 pure XS + 5 smoothing × 3 windows = ~40-50 per seed) is too
large to brute-force test; LLM cuts it to ~8-12 informed picks per seed.
Empirically that's a 5x BRAIN-budget save vs full enumerate, and a 5x cost save
vs LLM-emits-expressions.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

# Lazy imports for strategy prompts / LLM service to avoid the
# backend.agents.__init__ → mining_agent → graph → workflow → tier_seed →
# factor_wrapping circular import.
from backend.config import settings
from backend.factor_tier_classifier import _dedup_and_validate

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from backend.agents.services.llm_service import LLMService


# =============================================================================
# T2Strategy
# =============================================================================

GroupChoice = Literal["industry", "subindustry", "sector", "market", "country"]


class T2Strategy(BaseModel):
    """LLM-decided T2 wrapper selection. Inputs: T1 seed + region. Outputs:
    which group / pure-xs / smoothing wrappers to apply."""

    signal_velocity: Literal["SLOW", "MEDIUM", "FAST"]
    signal_source: Literal[
        "fundamental", "pv", "analyst", "sentiment", "factor_composite", "other"
    ] = "other"
    is_normalized: bool = False  # seed already contains zscore/rank?

    use_group_neutralize: List[GroupChoice] = Field(default_factory=list)
    use_group_rank: List[GroupChoice] = Field(default_factory=list)
    use_group_zscore: List[GroupChoice] = Field(default_factory=list)
    use_group_normalize: List[GroupChoice] = Field(default_factory=list)
    use_group_demean: List[GroupChoice] = Field(default_factory=list)

    use_pure_xs: List[
        Literal["rank", "zscore", "normalize", "quantile", "winsorize", "signed_power"]
    ] = Field(default_factory=list)

    use_smoothing: List[
        Literal[
            "ts_decay_linear@5",
            "ts_decay_linear@10",
            "ts_decay_linear@20",
            "ts_mean@5",
            "ts_mean@10",
            "ts_mean@20",
            "ts_std_dev@10",
            "ts_std_dev@20",
            "ts_max@10",
            "ts_min@10",
        ]
    ] = Field(default_factory=list)

    skip_reasons: Dict[str, str] = Field(default_factory=dict)
    rationale: str = ""


DEFAULT_T2_STRATEGY = T2Strategy(
    signal_velocity="MEDIUM",
    signal_source="other",
    is_normalized=False,
    use_group_neutralize=["industry"],
    use_group_rank=["industry"],
    use_pure_xs=["rank", "winsorize"],
    use_smoothing=["ts_decay_linear@10"],
    rationale="default fallback (LLM unavailable or returned invalid output)",
)


# =============================================================================
# T3Strategy
# =============================================================================

class T3Strategy(BaseModel):
    """LLM-decided T3 trade_when template selection."""

    signal_velocity: Literal["SLOW", "MEDIUM", "FAST"]
    use_templates: List[
        Literal[
            "high_volume_entry",
            "trend_entry",
            "vol_spike_entry",
            "rebound_entry",
            "oversold_entry",
            "earnings_entry",
        ]
    ] = Field(default_factory=list)
    skip_reason: Optional[str] = None
    rationale: str = ""


DEFAULT_T3_STRATEGY = T3Strategy(
    signal_velocity="MEDIUM",
    use_templates=["high_volume_entry", "vol_spike_entry"],
    rationale="default fallback (LLM unavailable or returned invalid output)",
)


# =============================================================================
# trade_when template registry (T3)
# =============================================================================

TRADE_WHEN_TEMPLATES: Dict[str, str] = {
    "high_volume_entry": "trade_when(volume > ts_mean(volume, 240), {expr}, -1)",
    "trend_entry": (
        "trade_when(rank(close - ts_delay(close, 5)) > 0.5, {expr}, ts_arg_max(close, 20) > 15)"
    ),
    "vol_spike_entry": "trade_when(abs(returns) > ts_std_dev(returns, 60) * 2, {expr}, -1)",
    "rebound_entry": (
        "trade_when(ts_arg_min(close, 20) < 5, {expr}, ts_arg_max(close, 60) > 30)"
    ),
    "oversold_entry": (
        "trade_when(returns < ts_zscore(returns, 60) * -1.5, {expr}, -1)"
    ),
    # USA-only: days_to_announcement is unavailable in CHN
    "earnings_entry": "trade_when(days_to_announcement < 5, {expr}, -1)",
}


def template_available(region: str, template_name: str) -> bool:
    """Region-aware template gating. CHN currently only blocks earnings_entry."""
    if template_name == "earnings_entry" and region == "CHN":
        return False
    return template_name in TRADE_WHEN_TEMPLATES


# =============================================================================
# Region group filtering
# =============================================================================

def _allowed_groups(region: str) -> set:
    """Return the set of valid group tokens for this region (from settings)."""
    region_groups = getattr(settings, "REGION_GROUPS", {})
    return set(region_groups.get(region, ["industry", "subindustry", "sector", "market"]))


# =============================================================================
# T2 LLM decision + expansion
# =============================================================================

async def select_t2_strategy_via_llm(
    seed_expression: str,
    seed_metrics: Dict,
    region: str,
    dataset_id: str,
    llm_service: "LLMService",
) -> T2Strategy:
    """Run LLM to choose T2 wrapper strategy. Falls back to DEFAULT on failure."""
    from backend.agents.prompts.strategy_prompts import (
        T2_STRATEGY_SYSTEM,
        build_t2_strategy_user_prompt,
    )

    region_groups = sorted(_allowed_groups(region))
    user_prompt = build_t2_strategy_user_prompt(
        seed_expression=seed_expression,
        seed_metrics=seed_metrics,
        region=region,
        dataset_id=dataset_id,
        region_groups=region_groups,
    )

    try:
        parsed, raw = await llm_service.call_with_schema(
            system_prompt=T2_STRATEGY_SYSTEM,
            user_prompt=user_prompt,
            schema=T2Strategy,
            temperature=0.7,
        )
    except Exception as e:
        logger.warning(f"[factor_wrapping] T2 LLM raised: {e}")
        return DEFAULT_T2_STRATEGY

    if parsed is None or not raw.success:
        logger.warning(
            f"[factor_wrapping] T2 LLM returned no valid strategy "
            f"(error={getattr(raw, 'error', '?')})"
        )
        return DEFAULT_T2_STRATEGY

    logger.info(
        f"[factor_wrapping] T2Strategy chosen | velocity={parsed.signal_velocity} "
        f"groups={sum(len(v) for v in [parsed.use_group_neutralize, parsed.use_group_rank, parsed.use_group_zscore, parsed.use_group_normalize, parsed.use_group_demean])} "
        f"pure_xs={len(parsed.use_pure_xs)} smoothing={len(parsed.use_smoothing)}"
    )
    return parsed


def expand_t2_strategy(
    seed: str, strategy: T2Strategy, region: str
) -> List[Dict]:
    """Materialize T2 expressions from strategy. Filters group choices unavailable
    for the region. Returns dedup+validated list of {expression, wrapper_kind}."""
    allowed = _allowed_groups(region)
    out: List[Dict] = []

    def _add_group(op_name: str, choices: List[str]):
        for g in choices:
            if g not in allowed:
                logger.debug(f"[factor_wrapping] T2 skip {op_name}_{g} (region={region})")
                continue
            out.append({
                "expression": f"{op_name}({seed}, {g})",
                "wrapper_kind": f"{op_name}_{g}",
            })

    _add_group("group_neutralize", strategy.use_group_neutralize)
    _add_group("group_rank", strategy.use_group_rank)
    _add_group("group_zscore", strategy.use_group_zscore)
    _add_group("group_normalize", strategy.use_group_normalize)
    _add_group("group_demean", strategy.use_group_demean)

    for op in strategy.use_pure_xs:
        if op == "winsorize":
            expr = f"winsorize({seed}, std=4)"
        elif op == "signed_power":
            expr = f"signed_power({seed}, 0.5)"
        else:
            expr = f"{op}({seed})"
        out.append({"expression": expr, "wrapper_kind": op})

    for spec in strategy.use_smoothing:
        # spec is "{op}@{window}", e.g. "ts_decay_linear@10"
        if "@" not in spec:
            logger.warning(f"[factor_wrapping] malformed smoothing spec: {spec}")
            continue
        op_name, window = spec.split("@", 1)
        out.append({
            "expression": f"{op_name}({seed}, {window})",
            "wrapper_kind": spec,
        })

    return _dedup_and_validate(out, target_tier=2, region=region)


# =============================================================================
# T3 LLM decision + expansion
# =============================================================================

async def select_t3_strategy_via_llm(
    seed_t2_expression: str,
    seed_metrics: Dict,
    region: str,
    dataset_id: str,
    llm_service: "LLMService",
) -> T3Strategy:
    """Run LLM to choose T3 trade_when templates. Falls back to DEFAULT on failure."""
    from backend.agents.prompts.strategy_prompts import (
        T3_STRATEGY_SYSTEM,
        build_t3_strategy_user_prompt,
    )

    user_prompt = build_t3_strategy_user_prompt(
        seed_t2_expression=seed_t2_expression,
        seed_metrics=seed_metrics,
        region=region,
        dataset_id=dataset_id,
    )

    try:
        parsed, raw = await llm_service.call_with_schema(
            system_prompt=T3_STRATEGY_SYSTEM,
            user_prompt=user_prompt,
            schema=T3Strategy,
            temperature=0.7,
        )
    except Exception as e:
        logger.warning(f"[factor_wrapping] T3 LLM raised: {e}")
        return DEFAULT_T3_STRATEGY

    if parsed is None or not raw.success:
        logger.warning(
            f"[factor_wrapping] T3 LLM returned no valid strategy "
            f"(error={getattr(raw, 'error', '?')})"
        )
        return DEFAULT_T3_STRATEGY

    logger.info(
        f"[factor_wrapping] T3Strategy chosen | velocity={parsed.signal_velocity} "
        f"templates={parsed.use_templates}"
    )
    return parsed


def expand_t3_strategy(
    seed_t2: str, strategy: T3Strategy, region: str
) -> List[Dict]:
    """Materialize T3 expressions by substituting seed into trade_when templates.
    Filters templates unavailable for the region."""
    out: List[Dict] = []
    for tpl_name in strategy.use_templates:
        if not template_available(region, tpl_name):
            logger.debug(
                f"[factor_wrapping] T3 skip template {tpl_name} (region={region})"
            )
            continue
        tpl = TRADE_WHEN_TEMPLATES[tpl_name]
        out.append({
            "expression": tpl.format(expr=seed_t2),
            "wrapper_kind": f"trade_when_{tpl_name}",
        })

    return _dedup_and_validate(out, target_tier=3, region=region)
