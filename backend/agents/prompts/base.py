"""
Base components for prompt building.

Contains:
- PromptContext data class
- Helper functions for building context sections
"""

from typing import Dict, List, Optional
from dataclasses import dataclass, field


@dataclass
class PromptContext:
    """Structured context for prompt rendering."""
    dataset_id: str = ""
    dataset_description: str = ""
    dataset_category: str = ""
    region: str = "USA"
    universe: str = "TOP3000"
    
    # Available data (will be JSON serialized)
    fields: List[Dict] = field(default_factory=list)
    operators: List[Dict] = field(default_factory=list)
    
    # Knowledge base context
    success_patterns: List[Dict] = field(default_factory=list)
    failure_pitfalls: List[Dict] = field(default_factory=list)
    
    # Strategy guidance (from StrategyAgent)
    preferred_fields: List[str] = field(default_factory=list)
    avoid_fields: List[str] = field(default_factory=list)
    focus_hypotheses: List[str] = field(default_factory=list)
    avoid_patterns: List[str] = field(default_factory=list)
    
    # Generation parameters
    num_alphas: int = 5
    exploration_weight: float = 0.5  # 0=pure exploitation, 1=pure exploration

    # Plan v5+ §Phase 1 cross-dataset hypothesis. Empty list = legacy
    # single-anchor mode; populated = LLM may pick 1-3 datasets in
    # `selected_datasets` to combine fields across domains.
    available_dataset_pool: List[str] = field(default_factory=list)

    # P2-B (2026-05-15): Five Pillars balance nudge. node_hypothesis sets this
    # to the under-represented pillar when the recent alpha pool is skewed
    # (and ENABLE_PILLAR_AWARE_SELECTION is on). None = no nudge, prompt
    # renders byte-for-byte legacy.
    pillar_hint: Optional[str] = None

    # P2-A (2026-05-16): Macro narratives — RAG-fetched economic mechanism
    # anchors for the current (dataset, region) + top-K focused fields.
    # node_hypothesis fills this when ENABLE_MACRO_NARRATIVE_GUIDANCE is on
    # and MacroNarrativeService.fetch_macro_narratives returns ≥1 row.
    # Empty list = no nudge → build_macro_context_block returns "" →
    # build_hypothesis_prompt renders byte-for-byte legacy (the invariant
    # is field-asserted in test_node_hypothesis_macro.test_flag_off_
    # byte_for_byte_legacy, M8).
    macro_narratives: List[Dict] = field(default_factory=list)

    # P2-C (2026-05-16): Style preset — serialised RegimePreset dict carrying
    # ``regime`` / ``style_label`` / ``style_philosophy`` / ``pillar_bias``.
    # node_hypothesis fills this when ENABLE_STYLE_PRESET_GUIDANCE is on AND
    # mining_agent already injected ``strategy.regime``. None / empty dict →
    # build_style_preset_block returns "" → build_hypothesis_prompt template
    # splice produces the empty string at the insertion point (byte-for-byte
    # legacy invariant, field-asserted in MF4 test_node_hypothesis_regime.
    # test_flag_off_byte_for_byte_legacy).
    style_preset: Optional[Dict] = None

    # G8 Phase A (2026-05-19): cross-task hypothesis-forest reference pool.
    # node_hypothesis fills this when ENABLE_HYPOTHESIS_FOREST_REUSE is on
    # AND HypothesisService.fetch_cross_task_promoted returns ≥1 row.
    # Each entry is a dict carrying {statement, rationale, pillar,
    # sharpe_avg, pass_count, alpha_count, hypothesis_id}. Empty list = no
    # nudge → build_cross_task_hypotheses_block returns "" → template
    # splice produces the empty string at the insertion point (byte-for-byte
    # legacy invariant, mirrors P2-A/B/C/D pattern).
    cross_task_hypotheses: List[Dict] = field(default_factory=list)

    # B5 R8-v3 (Sprint 3, 2026-05-20): selected cognitive-layer block —
    # rendered markdown ready to splice. None / "" → no nudge → template
    # splice produces empty (byte-for-byte legacy). When set, it's the
    # output of cognitive_layer_service.build_cognitive_layer_block(layer)
    # for one of 7 research lenses (macro / behavioral / technical / value
    # / microstructure / cross_sectional / time_series_mean_reversion).
    # node_hypothesis fills this when ENABLE_COGNITIVE_LAYER_PROMPT is on.
    cognitive_layer_block: str = ""
    # Layer id stamped on the resulting alpha.metrics for bandit reward
    # feedback (the orchestrator updates BanditArmStats from this on
    # round end). Empty when no layer fired.
    cognitive_layer_id: str = ""

    # A5.2 G10 PR2 (Sprint 4, 2026-05-20): distilled-logic block —
    # rendered markdown from active distilled_logic_library entries
    # for (region, pillar). Empty string when ENABLE_G10_LOGIC_INJECT
    # is OFF or no rows match → template splice yields empty (byte-
    # for-byte legacy). Mirrors cognitive_layer_block pattern.
    distilled_logic_block: str = ""


def build_fields_context(fields: List[Dict], max_fields: int = 30) -> str:
    """Build concise field reference with type info."""
    if not fields:
        return "No fields available."
    
    matrix_fields = []
    vector_fields = []
    other_fields = []
    
    for f in fields[:max_fields]:
        field_id = f.get("id", f.get("name", "unknown"))
        field_type = f.get("type", "MATRIX").upper()
        
        if field_type == "VECTOR":
            vector_fields.append(field_id)
        elif field_type == "MATRIX":
            matrix_fields.append(field_id)
        else:
            other_fields.append(field_id)
    
    lines = []
    
    if matrix_fields:
        sample = ", ".join(matrix_fields[:10])
        if len(matrix_fields) > 10:
            sample += f" ... (+{len(matrix_fields) - 10} more)"
        lines.append(f"- **MATRIX fields** (time-series, use ts_* operators directly): {sample}")
    
    if vector_fields:
        sample = ", ".join(vector_fields[:10])
        if len(vector_fields) > 10:
            sample += f" ... (+{len(vector_fields) - 10} more)"
        lines.append(f"- **VECTOR fields** (MUST use vec_* operators first!): {sample}")
    
    if other_fields:
        sample = ", ".join(other_fields[:5])
        lines.append(f"- Other: {sample}")
    
    return "\n".join(lines)


def build_operators_context(operators: List[Dict], max_ops: int = 120) -> str:
    """Build operator reference grouped by category."""
    if not operators:
        return "Use standard operators."
    
    by_category: Dict[str, List[str]] = {}
    for op in operators[:max_ops]:
        # `or "Other"` (not get default): category column is nullable, so a
        # NULL comes back as None — a None dict key would crash sorted() below
        # once it coexists with str keys.
        cat = op.get("category") or "Other"
        if cat not in by_category:
            by_category[cat] = []
        op_name = op.get("name", op.get("id", "unknown"))
        by_category[cat].append(op_name)

    lines = []
    for cat, op_names in sorted(by_category.items()):
        # Per-category cap of 30 (was 10): callers now pass the full catalog
        # ORDER BY category, name, so an alphabetical [:10] would drop the
        # workhorse Time Series ops (ts_mean/ts_rank/ts_zscore/ts_scale/...
        # all sort after ts_delta). 30 > the largest real category (~24 ops).
        lines.append(f"- {cat}: {', '.join(op_names[:30])}")

    return "\n".join(lines)


def build_patterns_context(patterns: List[Dict], label: str, max_items: int = 5) -> str:
    """Build pattern reference without implying they must be followed."""
    if not patterns:
        return f"No {label} recorded yet."

    lines = []
    for p in patterns[:max_items]:
        pattern = p.get("pattern", p.get("template", ""))
        desc = p.get("description", "")
        if pattern:
            lines.append(f"- `{pattern}`: {desc[:80]}")

    return "\n".join(lines) if lines else f"No {label} recorded yet."


def build_dual_channel_patterns_block(
    success_patterns: List[Dict],
    failure_pitfalls: List[Dict],
    *,
    dual_channel: bool = False,
    max_items: int = 5,
) -> str:
    """Phase 1 R4' (2026-05-17): render Historical Patterns block in either
    legacy single-section form OR dual-channel (Channel A ✓ / Channel B ⛔)
    visual-separated form.

    OFF (dual_channel=False) returns byte-for-byte legacy block — same lines
    that hypothesis.py emitted pre-R4'. Test
    ``test_dual_channel_off_byte_for_byte_legacy`` enforces this.

    ON (dual_channel=True) splits into two clearly-marked channels so the LLM
    treats positive vs negative evidence as orthogonal signals rather than
    merging both into a single "patterns" stream. P2-D negative nudge stays
    intact (rendered separately by build_negative_knowledge_nudge_block).

    Returns the full block including the section header and trailing note —
    the caller splices it as a single `{block}` placeholder.
    """
    if not dual_channel:
        # Legacy form — preserve exact whitespace / wording from hypothesis.py
        # pre-R4' rendering (lines 250-258 of the f-string template).
        return (
            "## Historical Patterns (For Reference Only)\n"
            "\n"
            "**Approaches that have worked in similar contexts**:\n"
            f"{build_patterns_context(success_patterns, 'patterns', max_items=max_items)}\n"
            "\n"
            "**Approaches that have not worked**:\n"
            f"{build_patterns_context(failure_pitfalls, 'pitfalls', max_items=max_items)}\n"
            "\n"
            "Note: These are observations, not rules. What failed before may work in different contexts."
        )

    # Dual-channel form — visual markers + explicit channel framing so the
    # LLM doesn't conflate positive and negative evidence streams.
    success_lines = build_patterns_context(success_patterns, "patterns", max_items=max_items)
    failure_lines = build_patterns_context(failure_pitfalls, "pitfalls", max_items=max_items)
    return (
        "## Historical Patterns — Dual Channel (For Reference Only)\n"
        "\n"
        "### ✓ Channel A — Approaches that HAVE WORKED in similar contexts\n"
        "\n"
        "Treat this channel as positive evidence. Lean toward these patterns "
        "when the current research question echoes prior success.\n"
        "\n"
        f"{success_lines}\n"
        "\n"
        "### ⛔ Channel B — Approaches that have NOT worked / produced pitfalls\n"
        "\n"
        "Treat this channel as negative evidence. Avoid mirroring these "
        "pitfalls unless your hypothesis explicitly addresses why the prior "
        "failure mode no longer applies.\n"
        "\n"
        f"{failure_lines}\n"
        "\n"
        "Note: Channel A and Channel B are orthogonal evidence streams. A "
        "pattern's absence from Channel A does NOT make it bad; a pitfall in "
        "Channel B may still work in a different context. Reason explicitly "
        "about which channel informs each hypothesis."
    )


def build_strategy_constraints(ctx: PromptContext) -> str:
    """Build strategy-driven constraints without being prescriptive."""
    constraints = []
    
    if ctx.avoid_fields:
        constraints.append(
            f"Fields with recent issues (consider alternatives): {', '.join(ctx.avoid_fields[:5])}"
        )
    
    if ctx.avoid_patterns:
        constraints.append(
            f"Patterns that underperformed recently: {'; '.join(ctx.avoid_patterns[:3])}"
        )
    
    # CRITICAL TYPE CONSTRAINTS
    constraints.append(
        "**VECTOR FIELD RULE**: VECTOR-type fields MUST be processed with vec_* operators "
        "(vec_sum, vec_avg, vec_max, vec_min, vec_count, vec_range, vec_stddev, etc.) "
        "BEFORE using ts_* operators. Example: ts_rank(vec_sum(vector_field), 20) - NOT ts_rank(vector_field, 20)"
    )
    constraints.append(
        "**MATRIX FIELD RULE**: MATRIX-type fields can use ts_* operators directly. "
        "Example: ts_rank(matrix_field, 20)"
    )
    # CANONICAL STRUCTURE (plan a-streamed-wren, 2026-05-21): a raw signal on
    # fundamental/price/volume fields carries market & sector beta and rarely
    # survives evaluation on its own. Teach the canonical alpha shape so
    # neutralization is a first-class instruction rather than something that
    # only leaks in via retrieved RAG patterns.
    constraints.append(
        "**CROSS-SECTIONAL NEUTRALIZATION RULE**: A raw time-series signal on "
        "fundamental/price/volume fields carries market & sector beta and rarely "
        "survives evaluation alone. Wrap the signal in a cross-sectional "
        "normalizer and, when a grouping is meaningful, neutralize it. Canonical "
        "shape: group_neutralize( normalize( ts_signal(fields) ), <group> ) "
        "where normalize is one of {rank, zscore, scale, normalize} and <group> "
        "is one of {market, sector, industry, subindustry}. "
        "Example: group_neutralize(rank(ts_zscore(<a listed field>, 60)), industry). "
        "Prefer at least one cross-sectional op (rank / zscore / scale / normalize "
        "/ group_*) in the outer layers unless the signal is already "
        "cross-sectionally comparable (a pure time-series factor-composite signal "
        "can pass on its own — see the FACTOR_COMPOSITE example above)."
    )

    # Syntax constraints (always apply)
    constraints.extend([
        "Lookback windows must be positive integers",
        "Maximum 3 distinct fields per expression",
        "Maximum 8 operators per expression",
        "Ensure no look-ahead bias (no future data access)"
    ])

    return "\n".join(f"- {c}" for c in constraints)


def build_macro_context_block(narratives: List[Dict]) -> str:
    """P2-A (2026-05-16): render a Macro Context block from narrative dicts.

    Each narrative dict carries the meta_data shape produced by
    ``backend.macro_narratives.narrative_to_kb_payload`` (or the LLM-batch
    upsert path in macro_narrative_extract). Empty input returns the empty
    string so build_hypothesis_prompt can splice it byte-for-byte unchanged
    (the P2-A flag-off invariant). Caps at 5 entries to bound prompt size.
    """
    if not narratives:
        return ""
    lines = ["## Macro Context — Economic Mechanism Anchors", ""]
    for n in narratives[:5]:
        scope = n.get("scope", "")
        if scope == "field":
            label = f"field `{n.get('field_id')}`"
        elif scope == "category":
            label = f"category `{n.get('dataset_category')}`"
        else:
            label = f"dataset `{n.get('dataset_id', '?')}`"
        try:
            conf = float(n.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        mechanism = (n.get("mechanism", "") or "")[:200]
        transmission = (n.get("transmission_channel", "") or "")[:200]
        hint = n.get("expected_signal_hint", "?")
        lines.append(
            f"- **{label}** ({hint}, conf={conf:.2f}): {mechanism}\n"
            f"    transmission: {transmission}"
        )
    return "\n".join(lines)


def build_cross_task_hypotheses_block(hypotheses: List[Dict]) -> str:
    """G8 Phase A (2026-05-19): render a cross-task hypothesis-forest reference
    block. Each entry should carry the fields produced by
    ``HypothesisService.fetch_cross_task_promoted``:
    ``statement / rationale / pillar / sharpe_avg / pass_count / alpha_count``.

    Empty input returns "" so build_hypothesis_prompt can splice it
    byte-for-byte unchanged when the flag is OFF or no rows qualify
    (G8 flag-off invariant — mirrors P2-A/B/C/D contract). Caps at 5
    entries to bound prompt size; if caller passes more, only top-5 render.
    """
    if not hypotheses:
        return ""
    lines = [
        "## Cross-task Hypothesis Forest — Reference (proven in same region)",
        "",
        "These hypotheses have ≥2 PASS alphas with sharpe_avg ≥ 1.0 in this "
        "region. Consider extending one of them (set `parent_hypothesis_id` in "
        "your statement narrative) when your idea aligns; or propose a fresh "
        "direction if none fit. Do NOT verbatim copy — adapt to the current "
        "dataset / pillar / regime.",
        "",
    ]
    for h in hypotheses[:5]:
        statement = (h.get("statement") or "")[:200]
        pillar = h.get("pillar") or "?"
        sharpe = h.get("sharpe_avg")
        passes = int(h.get("pass_count") or 0)
        attempts = int(h.get("alpha_count") or 0)
        hid = h.get("hypothesis_id") or h.get("id")
        sharpe_str = f"{float(sharpe):.2f}" if isinstance(sharpe, (int, float)) else "?"
        lines.append(
            f"- **H{hid}** (pillar={pillar}, sharpe_avg={sharpe_str}, "
            f"pass={passes}/{attempts}): {statement}"
        )
    return "\n".join(lines)


def build_style_preset_block(preset: Optional[Dict]) -> str:
    """P2-C (2026-05-16): render the Investment Philosophy block.

    Single rendering site for the regime-aware style preset (S4 — the
    regime_classifier module deliberately does NOT define a duplicate
    ``regime_to_prompt_block`` helper). Empty / None / falsy preset
    returns ``""`` so the caller's leading-newline splice collapses to
    the empty string and the prompt renders byte-for-byte legacy (the
    MF4 invariant verified by ``test_flag_off_byte_for_byte_legacy``).

    Pillar semantics (S6):
        * ``pillar_bias`` is a **soft** suggestion at the prompt-text
          level — pure narrative guidance to the LLM.
        * P2-B ``pillar_hint`` is a **hard** rebalance signal based on
          historical alpha-pool skew; it lives in PromptContext as a
          separate field and renders its own block.
        * The two are independent and may both be active. When both fire,
          P2-B ``pillar_hint`` (empirical / hard) takes precedence over
          P2-C ``pillar_bias`` (macro / soft) — but this is enforced at
          the LLM-reading layer, not by suppressing this block.
        * ``ENABLE_STYLE_PRESET_GUIDANCE=True`` +
          ``ENABLE_PILLAR_AWARE_SELECTION=False`` is a legal state:
          pillar_bias text appears in the prompt but no P2-B stamp is
          written. Documented and accepted.

    Args:
        preset: serialised RegimePreset dict with keys
            ``regime``, ``style_label``, ``style_philosophy``,
            ``pillar_bias`` (list[str]). Missing fields are rendered
            defensively ("?" / "" / "no bias") without crashing.

    Returns:
        Multi-line markdown string, or ``""`` for empty/None preset.
    """
    if not preset:
        return ""
    regime = preset.get("regime", "?")
    label = preset.get("style_label", "") or ""
    philosophy = (preset.get("style_philosophy", "") or "")[:200]
    pillars_raw = preset.get("pillar_bias") or []
    if isinstance(pillars_raw, (list, tuple)):
        pillars = ", ".join(str(p) for p in list(pillars_raw)[:5]) or "no bias"
    else:
        pillars = "no bias"
    return (
        f"## Investment Philosophy — Current Regime: {regime}\n\n"
        f"**Style**: {label}\n"
        f"**Philosophy**: {philosophy}\n"
        f"**Pillar bias** (prefer hypotheses in these pillars): {pillars}\n"
        f"\nWhen you write the `rationale` field, briefly reflect how the "
        f"hypothesis aligns with (or deliberately diverges from) this style."
    )
