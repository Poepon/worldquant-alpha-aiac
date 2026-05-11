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
- (Optional) Phase 1 multi-dataset context: when the user prompt names
  selected_datasets with 2+ entries, the available_fields list is the
  UNION of fields from those datasets. You MUST sample promising_fields
  from EACH selected_dataset.

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
   - **HARD: prefer fields with coverage >= 0.7**. Coverage < 0.5 fields almost
     always trigger BRAIN's CONCENTRATED_WEIGHT check because only a small
     subset of the universe has data → weight concentrates on those names.
   - **HARD: avoid `*_derivative`, `*_rank_derivative`, `implied_volatility_*`
     and any `bbg_*`/`pyth_*` suffix fields unless the dataset is the
     designated low-coverage option/IV/Bloomberg dataset for which the user
     explicitly opted in.** These are CW magnets in T1 form (their narrow
     coverage forces concentrated positions) and historical mining shows 0%
     of T1 alpha using them passed BRAIN's submission gate.
   - Spread across sub-themes (don't pick 12 variants of the same balance sheet line).
   - **V-21 FAMILY DIVERSITY MANDATE (HARD)** — promising_fields MUST span
     **at least 3 distinct field families**. Family classification:
       1. RETURNS family: `returns`, `ret_*` — short-term price reversal raw
       2. PRICE_PV family: `close`, `open`, `high`, `low`, `vwap`, `volume`, `amount`, `cap`
       3. FUNDAMENTAL family: `fnd*_*` — quarterly/annual financials
       4. ANALYST family: `anl*_*`, `est*_*`, `fam_*` — broker / forecast
       5. SENTIMENT family: `snt*_*`, `news_*`, `social_*`
       6. FACTOR_COMPOSITE family: `mdl*_*`, `model_*`, `composite_*`,
          `*_score_*`, `*_factor_*`, `fscore_*`
       7. OPTION family: `opt*_*`, `option_*`, IV-derived (avoid in T1 — see CW rule)
     **History shows the USA T1 PASS pool collapsed to 4/5 RETURNS-family
     mean-reversion alpha, producing T2 monoculture that BRAIN rejected as
     CONCENTRATED_WEIGHT / self-correlated.** Picking from ≥3 families forces
     genuine signal exploration across orthogonal economic mechanisms.
     If the dataset is fundamental-pure (e.g. fundamental6) or analyst-pure
     (e.g. analyst4), the families collapse — that's expected; aim for
     diversity within sub-themes (different fundamental concepts: profitability
     vs leverage vs growth vs cash-flow).
   - **PHASE 1 MANDATORY** (when selected_datasets has 2+ entries):
     promising_fields MUST include AT LEAST 2 fields from EACH listed
     dataset. Do not concentrate all picks on the anchor — that defeats
     the cross-dataset goal.
5. preferred_ts_ops: 5-8 operators from this exact set (every name must
   exist in BRAIN — these were reconciled with DB):
   {ts_rank, ts_zscore, ts_mean, ts_std_dev, ts_delta, ts_delay,
    ts_decay_linear, ts_arg_max, ts_arg_min, ts_quantile, ts_sum, ts_corr,
    ts_av_diff, ts_count_nans, ts_product, ts_scale, ts_step,
    ts_regression, ts_covariance, ts_backfill}. Match them to the velocity:
   - SLOW: ts_rank / ts_zscore / ts_mean / ts_regression preferred
   - FAST: ts_delta / ts_decay_linear / ts_arg_max / ts_av_diff preferred

   **V-14 BRAIN OPERATOR CHEAT SHEET — DO NOT INVENT OPERATORS**:
   - vec_* operators are AGGREGATIONS over vector dimensions. They EXIST:
     vec_avg, vec_sum, vec_max, vec_min, vec_l2_norm, vec_count, vec_median.
     They DO NOT exist as vec_ts_*. To time-series a VECTOR field:
     `ts_zscore(vec_avg(VECTOR_field), 20)` — aggregate first, then ts_op.
   - DO NOT use sequence(N), range(N), linspace(N), arange(N), time_index() —
     these are numpy/pandas, NOT BRAIN. For ts_regression(y, x, d) the
     second arg must be a same-length series — typically the same field
     or another time series, not a synthetic index.
   - DO NOT prefix ts_* with vec_ (no vec_ts_delta, vec_ts_zscore etc.)
   - VECTOR field type means cross-sectional/static-per-row data. To use
     in time-series operators, wrap with vec_avg / vec_sum first.
6. rationale: 2-3 sentences explaining the choice — what economic intuition
   ties the fields, ops, and window scale together. When selected_datasets
   has 2+ entries, EXPLICITLY name how each dataset contributes.

**V-22 BRAIN FEEDBACK INTERPRETATION (HARD)** — each Recent T1 success
pattern below is tagged with BRAIN's verdict:
   - `[BRAIN_OK ✓ submittable]` — passed BRAIN's /check, can actually be
     submitted. These are the only true successes; mimic their structure
     and field family.
   - `[BRAIN_REJECTED: LOW_FITNESS / CONCENTRATED_WEIGHT / SELF_CORR / ...]`
     — IS PASS was a false positive. BRAIN rejected the pattern at the
     submission gate. **Do NOT propose fields or structures that mirror
     [BRAIN_REJECTED] patterns** — the rejection reason tells you why:
       · LOW_FITNESS → signal too weak even after wrappers
       · CONCENTRATED_WEIGHT → field has narrow coverage / extreme values
       · SELF_CORR → too similar to existing portfolio
       · LOW_SUB_UNIVERSE_SHARPE → fails on smaller universes
   - `[BRAIN_PENDING]` — refresh in flight; treat as unknown.

If most patterns are [BRAIN_REJECTED] you'll see a V-22 BRAIN-REJECTION
ALERT in the user prompt — heed it and pivot field selection.

Return ONLY the JSON object. No prose. No markdown fence. No commentary.
"""


def build_t1_strategy_user_prompt(
    dataset_id: str,
    region: str,
    available_fields: List[Dict],
    success_patterns: Optional[List[Dict]] = None,
    last_round_feedback: Optional[Dict] = None,
    selected_datasets: Optional[List[str]] = None,
    dedup_skeletons: Optional[List[str]] = None,
    explore_mode: bool = False,
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
        dedup_skeletons: Layer 1 Anti-collapse (2026-05-11) — skeletons that
            the pre-simulate dedup gate already rejected this run. Renders a
            "DO NOT REGENERATE" block so the LLM stops sampling the same
            narrow neighborhood.
        explore_mode: Layer 1 ε-greedy — when True, hide RAG success patterns
            and prepend an EXPLORE MODE directive instructing the LLM to
            prioritize structural novelty over historical mimicry. Caller
            (strategy_select node) tosses a coin against EXPLORE_BUDGET_PCT
            once per round.
    """
    # In explore mode, intentionally hide success_patterns from the LLM so
    # it can't anchor to known PASS shapes. The LLM still sees fields,
    # dedup_skeletons, and the explore directive.
    if explore_mode:
        success_patterns = []
    # P0 CW防御 (2026-05-07): 硬过滤明显 CW-prone 字段。低覆盖 / IV / 衍生
    # 后缀字段在 T1 (bare ts_op) 形态下几乎必然触发 BRAIN CONCENTRATED_WEIGHT。
    # 历史 mining: 4 batch / 38 PROV+PASS alpha / 0 submittable, 主因即此类字段。
    # 注: 这是 prompt 层硬截; LLM 收不到这些字段就不会选它们。如果将来 T2
    # wrap 这些字段确认有效, 把它们留给 T2 mining seed pool 而非 T1。
    _CW_PRONE_PATTERNS = (
        "_derivative", "implied_volatility_", "_bbg_", "_pyth_",
        "_dvd_cash_",  # pv96 dividend fields — account permission issue
    )
    def _is_cw_prone(fid: str, cov) -> bool:
        if cov is not None and cov < 0.5:
            return True
        flow = (fid or "").lower()
        return any(p in flow for p in _CW_PRONE_PATTERNS)

    raw_fields = list(available_fields or [])
    filtered = [f for f in raw_fields if not _is_cw_prone(
        f.get("id") or f.get("name") or "",
        f.get("coverage"),
    )]
    # Defense: if filter strips 90%+ of fields (e.g. all-IV dataset), keep
    # original list to avoid empty-pool deadlock — LLM still gets the prompt
    # warning to lean toward higher-coverage choices.
    if len(filtered) >= max(8, int(len(raw_fields) * 0.3)):
        usable_fields = filtered
    else:
        usable_fields = raw_fields

    field_lines = []
    for f in usable_fields[:80]:
        fid = f.get("id") or f.get("name") or "?"
        ftype = f.get("type", "MATRIX")
        cov = f.get("coverage", 1.0)
        desc = (f.get("description") or "").strip().replace("\n", " ")[:80]
        field_lines.append(f"  - {fid}\t({ftype}, cov={cov:.2f}) {desc}")
    fields_block = "\n".join(field_lines) if field_lines else "  (no fields available)"

    # V-21 (2026-05-10): augment patterns with detected family bins so the LLM
    # sees BOTH "what worked" and "where the existing pool is concentrated"
    # — this nudges it to explore under-represented families per the
    # DIVERSITY MANDATE in the system prompt above.
    def _classify_family(text: str) -> str:
        t = (text or "").lower()
        if "returns" in t or "ret_" in t:
            return "RETURNS"
        if "fnd" in t:
            return "FUNDAMENTAL"
        if "anl" in t or "fam_" in t or "est" in t:
            return "ANALYST"
        if "snt" in t or "news" in t or "social" in t:
            return "SENTIMENT"
        if "fscore" in t or "model_" in t or "mdl" in t or "composite" in t or "_score_" in t:
            return "FACTOR_COMPOSITE"
        if "opt" in t or "implied_vol" in t:
            return "OPTION"
        if any(w in t for w in ("close", "open", "high", "low", "vwap", "volume", "amount", "cap")):
            return "PRICE_PV"
        return "OTHER"

    pattern_lines = []
    family_counts: Dict[str, int] = {}
    brain_rejected_count = 0
    for p in (success_patterns or [])[:6]:
        synth = " (synthesized)" if p.get("is_synthesized") else ""
        sharpe = p.get("expected_sharpe")
        sharpe_str = f", expected_sharpe={sharpe:.2f}" if isinstance(sharpe, (int, float)) else ""
        pattern_text = p.get("pattern", "?")
        fam = _classify_family(pattern_text)
        family_counts[fam] = family_counts.get(fam, 0) + 1

        # V-22 (2026-05-10): surface BRAIN /check verdict so the LLM sees
        # which IS-PASS skeletons actually failed BRAIN's submission gate.
        # The LLM's local-optimum (returns reversal) often passes IS but
        # gets rejected by BRAIN on fitness < 1.0 / self-correlation /
        # CONCENTRATED_WEIGHT — without this signal the LLM can't tell.
        brain_can_submit = p.get("brain_can_submit")
        failed_checks = p.get("brain_failed_checks") or []
        if brain_can_submit is True:
            brain_tag = " [BRAIN_OK ✓ submittable]"
        elif brain_can_submit is False:
            brain_rejected_count += 1
            fail_names = ",".join(c.get("name", "?") for c in failed_checks[:3])
            brain_tag = f" [BRAIN_REJECTED: {fail_names}]"
        else:
            brain_tag = " [BRAIN_PENDING]"
        pattern_lines.append(f"  - [{fam}]{brain_tag} {pattern_text}{sharpe_str}{synth}")
    patterns_block = "\n".join(pattern_lines) if pattern_lines else "  (none — cold start)"

    # V-21: emit a family-distribution callout when the patterns are
    # concentrated. >50% of patterns in one family is a clear signal the
    # LLM should diversify in this round.
    diversity_callout = ""
    if family_counts:
        total = sum(family_counts.values())
        top_fam, top_n = max(family_counts.items(), key=lambda kv: kv[1])
        if total >= 3 and top_n / total > 0.5:
            other_fams = "FUNDAMENTAL/ANALYST/SENTIMENT/FACTOR_COMPOSITE/PRICE_PV"
            diversity_callout = (
                f"\n**V-21 DIVERSITY ALERT** — recent patterns concentrated in "
                f"{top_fam} ({top_n}/{total}). To break the monoculture, prefer "
                f"fields from under-represented families this round: "
                f"{other_fams}.\n"
            )

    # V-22 (2026-05-10): emit a BRAIN-rejection callout when the success
    # pool is dominated by IS-PASS-but-BRAIN-rejected patterns. Pure IS PASS
    # without submittability is an empty victory; the LLM should pivot.
    brain_rejected_callout = ""
    if (success_patterns or []) and brain_rejected_count >= 2:
        total_with_verdict = sum(
            1 for p in (success_patterns or [])[:6]
            if p.get("brain_can_submit") is not None
        )
        if total_with_verdict and brain_rejected_count / total_with_verdict >= 0.5:
            brain_rejected_callout = (
                f"\n**V-22 BRAIN-REJECTION ALERT** — {brain_rejected_count}/"
                f"{total_with_verdict} of the recent IS-PASS patterns were "
                f"REJECTED by BRAIN's submission gate (look for "
                f"[BRAIN_REJECTED:...] tags). High IS sharpe alone doesn't "
                f"mean submittable. To produce alpha that BRAIN actually "
                f"accepts, watch for the rejection reasons (LOW_FITNESS / "
                f"CONCENTRATED_WEIGHT / SELF_CORR / LOW_SUB_UNIVERSE_SHARPE) "
                f"and prefer fields/structures that historically produced "
                f"[BRAIN_OK] patterns.\n"
            )

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

    # Plan v5+ §Phase 1 D2: surface multi-dataset context to the LLM so
    # the "MUST sample from EACH selected_dataset" rule has named targets.
    cross_block = ""
    if selected_datasets and len(selected_datasets) > 1:
        cross_block = (
            f"\n**Phase 1 cross-dataset mode** — selected_datasets="
            f"{selected_datasets}\n"
            f"  available_fields below is the UNION of fields from these datasets.\n"
            f"  Pick AT LEAST 2 promising_fields from EACH dataset (not just anchor).\n"
            f"  rationale must explicitly explain how each dataset contributes.\n"
        )

    # P2 portfolio-aware (2026-05-08): inject submitted-alpha skeletons as
    # soft guidance so LLM avoids re-generating shapes that are already in
    # the user's portfolio (would fail BRAIN self-correlation at submit-time).
    # Loads from JSON cache; refreshed by submit_alpha post-hook + standalone.
    portfolio_block = ""
    try:
        from backend.agents.seed_pool.portfolio_skeletons import get_portfolio_block
        portfolio_block = get_portfolio_block(region)
    except Exception:
        portfolio_block = ""

    # #2 fitness-aware DISABLED (2026-05-08): empirical 2-batch test
    # showed LLM over-generalizes the listed fields (saw
    # 'anl4_adjusted_netincome_ft' → tried 'anl4_ady_mean' which has fit=0.12).
    # Net negative: 308-311 without #2 produced 2 PASS + 2 can_submit;
    # 312-315 + 316-318 with #2 produced 1 PASS / 0 can_submit / mostly 0
    # alpha persisted (75 candidates → 75 FAIL on 316-318). Reverting to
    # P2 portfolio block alone (which empirically nudges LLM to anl4 family
    # naturally without misleading specific-field claims).
    high_fit_block = ""

    # Layer 1 Anti-collapse (2026-05-11): show the LLM a blacklist of
    # skeletons already rejected by pre-simulate dedup gates this run.
    # Without this signal the LLM keeps sampling the same narrow
    # neighborhood (db_duplicate rate ~90% in spike). Empirically this is
    # the highest-ROI fix when cascade is producing 0 new alphas.
    dedup_block = ""
    if dedup_skeletons:
        # Cap at 30 most recent for prompt budget; each skeleton ~80 chars
        recent_skels = list(dedup_skeletons)[-30:]
        bullets = "\n".join(f"  - {sk[:120]}" for sk in recent_skels)
        dedup_block = (
            f"\n**L1 ANTI-COLLAPSE: DO NOT REGENERATE THESE SKELETONS** "
            f"({len(recent_skels)} of {len(dedup_skeletons)} shown)\n"
            f"These skeletons (operator shapes) were already in DB or in your "
            f"submitted portfolio — regenerating wastes BRAIN sim quota and "
            f"produces 0 new alphas. Pick fields and operator chains that "
            f"yield STRUCTURALLY DIFFERENT skeletons:\n"
            f"{bullets}\n"
        )

    # L1 ε-greedy EXPLORE prefix — strongly worded directive at top of
    # prompt so the LLM treats this round as a deliberate break from past
    # patterns. Pairs with success_patterns=[] above (no anchor examples).
    explore_prefix = ""
    if explore_mode:
        explore_prefix = (
            "**L1 EXPLORE MODE (this round)** — historical PASS patterns are "
            "intentionally hidden. This round is part of an explicit ε-greedy "
            "exploration budget designed to escape collapsed search "
            "neighborhoods.\n"
            "  - Bias selection toward fields and operator chains the prior "
            "rounds have NOT exploited.\n"
            "  - Don't try to mimic past shapes; aim for structural novelty.\n"
            "  - Lower-confidence picks are OK — exploration value is in the "
            "diversity, not the expected sharpe.\n\n"
        )

    return f"""{explore_prefix}Dataset (anchor): {dataset_id}
Region: {region}
{cross_block}
Available fields (first 80):
{fields_block}
{dedup_block}
Recent T1 success patterns:
{patterns_block}
{diversity_callout}{brain_rejected_callout}{feedback_block}{portfolio_block}{high_fit_block}
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
    dedup_skeletons: Optional[List[str]] = None,
    explore_mode: bool = False,
) -> str:
    """Compose the T2 user prompt.

    Args:
        seed_expression: The T1 PASS expression to wrap, e.g. "ts_rank(close, 20)".
        seed_metrics: {sharpe, fitness, turnover, returns} from the seed alpha.
        region: BRAIN region.
        dataset_id: Source dataset.
        region_groups: Available group names for this region. CHN drops "sector",
            EUR/ASI add "country". Caller looks this up from settings.REGION_GROUPS.
        dedup_skeletons: Layer 1 Anti-collapse — skeletons rejected by
            pre-simulate dedup gate this run. T2 wrapper space is narrow
            (~5 wrapper × 4 group = 20 combos), so this list breaks the
            LLM's tendency to re-emit the same group_neutralize/group_rank
            combinations once the seed pool stabilizes.
    """
    metrics = seed_metrics or {}
    metric_str = ", ".join(
        f"{k}={metrics[k]:.3f}" if isinstance(metrics.get(k), (int, float)) else f"{k}=n/a"
        for k in ("sharpe", "fitness", "turnover", "returns")
    )
    groups_str = ", ".join(region_groups or ["industry", "subindustry", "sector", "market"])

    dedup_block = ""
    if dedup_skeletons:
        recent_skels = list(dedup_skeletons)[-20:]
        bullets = "\n".join(f"  - {sk[:120]}" for sk in recent_skels)
        dedup_block = (
            f"\n**L1 ANTI-COLLAPSE: DO NOT regenerate wrapper combos that produce "
            f"these skeletons** ({len(recent_skels)} of {len(dedup_skeletons)} shown):\n"
            f"{bullets}\n"
            f"Pick wrapper / group choices that yield structurally different shapes.\n"
        )

    explore_prefix = ""
    if explore_mode:
        explore_prefix = (
            "**L1 EXPLORE MODE (this round)** — pick wrapper / group "
            "combinations the prior T2 rounds have NOT exploited. Bias toward "
            "less-common wrappers (signed_power, winsorize, ts_decay_linear) "
            "and less-common groups (market, country) over the dominant "
            "industry/subindustry × group_neutralize/group_rank pair.\n\n"
        )

    return f"""{explore_prefix}T1 seed expression:
  {seed_expression}

Seed metrics: {metric_str}
Region: {region} (available groups: {groups_str})
Dataset: {dataset_id}
{dedup_block}
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
    dedup_skeletons: Optional[List[str]] = None,
    explore_mode: bool = False,
) -> str:
    """Compose the T3 user prompt.

    Args:
        seed_t2_expression: The T2 PASS expression to wrap.
        seed_metrics: {sharpe, fitness, turnover, returns} from seed.
        region: BRAIN region — controls which templates are available.
        dataset_id: Source dataset.
        dedup_skeletons: Layer 1 Anti-collapse — skeletons rejected this run.
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

    dedup_block = ""
    if dedup_skeletons:
        recent_skels = list(dedup_skeletons)[-15:]
        bullets = "\n".join(f"  - {sk[:120]}" for sk in recent_skels)
        dedup_block = (
            f"\n**L1 ANTI-COLLAPSE: DO NOT regenerate templates that produce "
            f"these skeletons** ({len(recent_skels)} shown):\n"
            f"{bullets}\n"
        )

    explore_prefix = ""
    if explore_mode:
        explore_prefix = (
            "**L1 EXPLORE MODE (this round)** — pick trade_when templates "
            "that prior T3 rounds have NOT exploited. Try less-common "
            "templates (rebound_entry, oversold_entry, vol_spike_entry) over "
            "the dominant high_volume_entry / trend_entry pair.\n\n"
        )

    return f"""{explore_prefix}T2 seed expression:
  {seed_t2_expression}

Seed metrics: {metric_str}
Region: {region} ({note})
Dataset: {dataset_id}
{dedup_block}
Choose 2-5 trade_when templates. Output the T3Strategy JSON.
"""
