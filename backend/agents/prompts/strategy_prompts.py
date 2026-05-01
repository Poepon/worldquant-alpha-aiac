"""LLM-guided strategy prompts for T1 / T2 / T3 tier mining (PR2).

Three system prompts + three user-prompt builders.

Architecture (plan §3 / §11):
- LLM is given dataset / seed context, returns a strict JSON `Strategy` object
  (T1Strategy / T2Strategy / T3Strategy) describing what fields/wrappers/templates
  to use. It does NOT emit alpha expressions directly — that avoids language-
  model hallucination on operator syntax and minimizes IP exposure.
- Code (factor_generation.expand_t1_strategy / factor_wrapping.expand_t{2,3}_strategy)
  enumerates concrete expressions from the strategy.

System prompts here are versioned implicitly: changing them affects what the LLM
returns. If you need to A/B test a prompt change, branch via feature flag in
factor_generation/factor_wrapping rather than editing in place.
"""
from __future__ import annotations

from typing import Dict, List, Optional


# =============================================================================
# T1 — single ts_op over a single field
# =============================================================================

T1_STRATEGY_SYSTEM = """\
You are a quant analyst selecting candidate T1 alpha signals for a dataset.

T1 alphas are STRICTLY of the form `ts_op(field, window)` — a single time-series
operator applied to a single data field. No cross-sectional ranks, no multi-field
arithmetic, no nested operators. This is the explore-the-direction phase.

Given:
- Dataset & region context
- A list of available fields with types and coverage
- (Optional) recent T1 success patterns from the knowledge base

Output a JSON object matching the T1Strategy schema:
1. economic_hypothesis: ONE sentence describing the economic story behind the
   signals you're selecting. Example: "Quarterly fundamental quality signals
   exhibit medium-term momentum reversal in USA TOP3000."
2. signal_velocity: classification matching the dataset characteristics.
   - FUNDAMENTAL_SLOW: quarterly/annual fundamentals (fnd6_*, return_equity)
   - FACTOR_COMPOSITE: pre-computed factor scores (mdl4_*, model_*)
   - MEDIUM: analyst / sentiment data (snt1_*, est1_*)
   - FAST: price-volume / intraday (close, volume, returns)
3. window_scale:
   - SHORT: 5-10 day windows (FAST signals)
   - MEDIUM: 20-60 day windows (MEDIUM, FACTOR_COMPOSITE)
   - LONG: 120-240 day windows (FUNDAMENTAL_SLOW)
4. promising_fields: 8-15 field IDs picked from the available_fields list.
   - Avoid categorical / ID / group fields (industry, sector codes, exchange).
   - Prefer fields with coverage >= 0.7 when available.
   - Spread across sub-themes (don't pick 12 variants of the same balance sheet line).
5. preferred_ts_ops: 5-8 operators from this exact set (every name must
   exist in BRAIN — these were reconciled with DB):
   {ts_rank, ts_zscore, ts_mean, ts_std_dev, ts_delta, ts_delay,
    ts_decay_linear, ts_arg_max, ts_arg_min, ts_quantile, ts_sum, ts_corr,
    ts_av_diff, ts_count_nans, ts_product, ts_scale, ts_step,
    ts_regression, ts_covariance, ts_backfill}. Match them to the velocity:
   - SLOW: ts_rank / ts_zscore / ts_mean / ts_regression preferred
   - FAST: ts_delta / ts_decay_linear / ts_arg_max / ts_av_diff preferred
6. rationale: 2-3 sentences explaining the choice — what economic intuition
   ties the fields, ops, and window scale together.

Return ONLY the JSON object. No prose. No markdown fence. No commentary.
"""


def build_t1_strategy_user_prompt(
    dataset_id: str,
    region: str,
    available_fields: List[Dict],
    success_patterns: Optional[List[Dict]] = None,
    last_round_feedback: Optional[Dict] = None,
) -> str:
    """Compose the T1 user prompt.

    Args:
        dataset_id: Target dataset (e.g. "fundamental2").
        region: Target region (e.g. "USA").
        available_fields: List of {id, type, coverage, description} from RAG_QUERY.
            Truncated to first 80 entries to keep prompt manageable.
        success_patterns: Recent T1 SUCCESS_PATTERN rows (from RAG); each entry
            should expose pattern, expected_sharpe, is_synthesized.
        last_round_feedback: When called for round N (N>1), pass the last round's
            summary so the LLM can shift fields/velocity if it produced 0 PASS.
    """
    field_lines = []
    for f in (available_fields or [])[:80]:
        fid = f.get("id") or f.get("name") or "?"
        ftype = f.get("type", "MATRIX")
        cov = f.get("coverage", 1.0)
        desc = (f.get("description") or "").strip().replace("\n", " ")[:80]
        field_lines.append(f"  - {fid}\t({ftype}, cov={cov:.2f}) {desc}")
    fields_block = "\n".join(field_lines) if field_lines else "  (no fields available)"

    pattern_lines = []
    for p in (success_patterns or [])[:6]:
        synth = " (synthesized)" if p.get("is_synthesized") else ""
        sharpe = p.get("expected_sharpe")
        sharpe_str = f", expected_sharpe={sharpe:.2f}" if isinstance(sharpe, (int, float)) else ""
        pattern_lines.append(f"  - {p.get('pattern','?')}{sharpe_str}{synth}")
    patterns_block = "\n".join(pattern_lines) if pattern_lines else "  (none — cold start)"

    feedback_block = ""
    if last_round_feedback:
        rs = last_round_feedback
        feedback_block = (
            f"\nLast round feedback (round {rs.get('round_index','?')}):\n"
            f"  pass_rate: {rs.get('pass_rate', 'n/a')}\n"
            f"  best_sharpe: {rs.get('best_sharpe', 'n/a')}\n"
            f"  n_passed: {rs.get('n_alphas_passed', 0)}\n"
            f"  hint: if 0 passed, shift fields or window_scale; "
            f"if some passed, double down on what worked.\n"
        )

    return f"""Dataset: {dataset_id}
Region: {region}

Available fields (first 80):
{fields_block}

Recent T1 success patterns:
{patterns_block}
{feedback_block}
Now produce the T1Strategy JSON for this round.
"""


# =============================================================================
# T2 — wrap a T1 PASS seed with cross-sectional / smoothing wrappers
# =============================================================================

T2_STRATEGY_SYSTEM = """\
You are a quant analyst selecting WRAPPING strategies for a T1 alpha signal.

A T1 signal is a single ts_op over a single field (e.g. `ts_rank(close, 20)`).
Your job is to decide which T2 wrappers are worth trying — group neutralizations,
pure cross-sectional transforms, time-series smoothing — to convert the raw
signal into something that survives industry / size / volatility regimes.

You do NOT write expressions. You output wrapper choices; code enumerates the
concrete expressions.

Given:
- The T1 seed expression and its IS metrics (sharpe, fitness, turnover)
- Region & dataset context

Output a JSON object matching the T2Strategy schema:

1. signal_velocity (SLOW / MEDIUM / FAST): infer from the seed's window length
   and field type.

2. signal_source: one of {fundamental, pv, analyst, sentiment, factor_composite, other}.
   Used to decide which wrappers are likely to help.

3. is_normalized: TRUE if the seed already contains zscore/rank/normalize at
   any level. When TRUE, skip pure cross-sectional rank/zscore/normalize
   wrappers (they'd be no-ops) and prefer group_neutralize / group_mean.

4. Group wrappers (every op verified against BRAIN DB):
   use_group_neutralize / use_group_rank / use_group_zscore / use_group_mean /
   use_group_scale: each is a list of group choices to apply.
   Allowed groups: industry, subindustry, sector, market.
   Pick 0-3 group choices per wrapper. Prefer industry/subindustry over sector
   for stocks. Skip the entire wrapper by passing []
   if it doesn't fit the signal economics.
   - group_neutralize: residualize against group mean (most common)
   - group_rank: cross-sectional rank within group
   - group_zscore: standardize within group
   - group_mean: pure group-average residual
   - group_scale: size-normalize within group

5. use_pure_xs: list of pure cross-sectional ops to try.
   Allowed: rank, zscore, normalize, quantile, winsorize, signed_power, scale.
   Skip if is_normalized=True (the seed already does this kind of work).

6. use_smoothing: list of time-series smoothing wrappers in the form
   "{op}@{window}", e.g. "ts_decay_linear@10". Allowed combinations:
   ts_decay_linear@5/@10/@20, ts_mean@5/@10/@20, ts_std_dev@10/@20.
   Use for FAST signals to reduce noise; skip for SLOW fundamentals
   (already smooth).

7. skip_reasons: dict mapping wrapper kind → ONE-line reason for skipping.
   Helps post-task analytics understand why certain branches weren't tried.

8. rationale: 2-3 sentence economic justification.

Aim for ~8-12 total variants per seed across all wrappers combined. Code
will enforce dedup and tier validation; over-suggesting is fine but won't help.

Return ONLY the JSON object.
"""


def build_t2_strategy_user_prompt(
    seed_expression: str,
    seed_metrics: Optional[Dict] = None,
    region: str = "USA",
    dataset_id: str = "",
    region_groups: Optional[List[str]] = None,
) -> str:
    """Compose the T2 user prompt.

    Args:
        seed_expression: The T1 PASS expression to wrap, e.g. "ts_rank(close, 20)".
        seed_metrics: {sharpe, fitness, turnover, returns} from the seed alpha.
        region: BRAIN region.
        dataset_id: Source dataset.
        region_groups: Available group names for this region. CHN drops "sector",
            EUR/ASI add "country". Caller looks this up from settings.REGION_GROUPS.
    """
    metrics = seed_metrics or {}
    metric_str = ", ".join(
        f"{k}={metrics[k]:.3f}" if isinstance(metrics.get(k), (int, float)) else f"{k}=n/a"
        for k in ("sharpe", "fitness", "turnover", "returns")
    )
    groups_str = ", ".join(region_groups or ["industry", "subindustry", "sector", "market"])

    return f"""T1 seed expression:
  {seed_expression}

Seed metrics: {metric_str}
Region: {region} (available groups: {groups_str})
Dataset: {dataset_id}

Choose 8-12 wrapper variants. Output the T2Strategy JSON.
"""


# =============================================================================
# T3 — wrap a T2 PASS seed with trade_when entry-filter templates
# =============================================================================

T3_STRATEGY_SYSTEM = """\
You are a quant analyst selecting trade_when entry-filter templates for a T2 alpha.

T3 wraps a T2 signal in `trade_when(condition, <T2 expr>, exit)` to enter only
when a market condition is met. This drops effective turnover dramatically and
turns medium-turnover T2 signals into submission-ready alphas.

You do NOT write trade_when expressions. You pick which templates to apply;
code substitutes the seed.

Given:
- The T2 seed expression and its metrics (especially turnover — high turnover
  T2 benefits most from selective entry)
- Region (some templates require region-specific fields)

Output a JSON object matching the T3Strategy schema:

1. signal_velocity (SLOW / MEDIUM / FAST): copy from the underlying T2 seed.

2. use_templates: list of trade_when templates to try. Pick 2-5; quality
   over quantity. Available templates:
   - high_volume_entry: enter on above-average volume days
   - trend_entry: enter when short-term momentum is positive
   - vol_spike_entry: enter on above-2-sigma return moves
   - rebound_entry: enter after recent bottom
   - oversold_entry: enter on negative-2-sigma return moves
   - earnings_entry: enter near earnings announcement (USA only — skip for CHN)

3. skip_reason: ONE-line reason if you're returning fewer than 2 templates
   (e.g. "seed turnover already 0.2 — entry filter would over-restrict").

4. rationale: 2-3 sentences justifying which templates fit the seed economics.

Return ONLY the JSON object.
"""


def build_t3_strategy_user_prompt(
    seed_t2_expression: str,
    seed_metrics: Optional[Dict] = None,
    region: str = "USA",
    dataset_id: str = "",
) -> str:
    """Compose the T3 user prompt.

    Args:
        seed_t2_expression: The T2 PASS expression to wrap.
        seed_metrics: {sharpe, fitness, turnover, returns} from seed.
        region: BRAIN region — controls which templates are available.
        dataset_id: Source dataset.
    """
    metrics = seed_metrics or {}
    metric_str = ", ".join(
        f"{k}={metrics[k]:.3f}" if isinstance(metrics.get(k), (int, float)) else f"{k}=n/a"
        for k in ("sharpe", "fitness", "turnover", "returns")
    )
    note = (
        "earnings_entry NOT available (CHN region lacks days_to_announcement field)"
        if region == "CHN"
        else "all templates available"
    )

    return f"""T2 seed expression:
  {seed_t2_expression}

Seed metrics: {metric_str}
Region: {region} ({note})
Dataset: {dataset_id}

Choose 2-5 trade_when templates. Output the T3Strategy JSON.
"""
