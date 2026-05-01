"""Smart BRAIN simulation settings — choose delay / decay / neutralization /
truncation per alpha based on expression form + tier + field metadata.

Why this exists:
- Default settings (delay=1, decay=4, neutralization=SUBINDUSTRY, truncation=0.08)
  are reasonable for raw T1 ts_op signals.
- T2 wrappers like `group_neutralize(X, industry)` already neutralize at
  the expression level. Simulating with BRAIN's neutralization=SUBINDUSTRY
  on top would double-neutralize and erode signal.
- T3 `trade_when(filter, X, exit)` changes turnover dramatically; the default
  decay=4 fights the entry-filter logic.
- Slow fundamental fields benefit from higher decay (16-32) to keep turnover
  in T2's [0.01, 0.55] band; fast price-volume fields fail with decay > 2.

This module is data-driven and side-effect-free. Callers (node_simulate,
flip-retry block, future genetic mutations) pass an expression + context
and get back a settings dict ready to forward to BrainAdapter.simulate_alpha.

Toggle integration via settings.ENABLE_SMART_SIM_SETTINGS (default False —
the data so far doesn't show settings is the bottleneck; flag exists so the
optimization can be enabled without touching call sites).

Public API:
    smart_simulation_settings(expression, *, tier=None, region="USA",
                              universe="TOP3000", test_period="P2Y0M",
                              field_category=None, overrides=None) -> Dict
    settings_reason(expression, tier=None, field_category=None) -> str

The reason string is meant for telemetry / metadata.sim_settings_reason so
that post-mortem analytics can correlate settings choices with PASS rates.
"""
from __future__ import annotations

from typing import Dict, Optional

from backend.factor_tier_classifier import (
    _is_negation_wrapper,
    _strip_outer_parens,
    _top_level_call,
)


# Wrappers that already perform per-group neutralization in the expression.
# Re-applying BRAIN-side neutralization on top would double-process the
# returns and typically erode the signal.
_INTRINSIC_NEUT_OPS = frozenset({
    "group_neutralize",
    "group_demean",
    "group_zscore",
    "group_rank",
    "group_normalize",
})

# Field-category → recommended decay range. Sourced from quant convention
# and our internal observation. Conservative defaults; if a category's
# distribution argues otherwise, override at call site.
_FIELD_DECAY_HINTS: Dict[str, int] = {
    "fundamental": 32,       # quarterly/annual signals — heavy smoothing OK
    "factor_composite": 8,   # pre-aggregated quality scores — mild smoothing
    "analyst": 8,            # estimate revisions — medium horizon
    "sentiment": 4,          # news / social — fast-decaying
    "pv": 0,                 # price-volume — preserve fine structure
    "macro": 64,             # FOMC / CPI etc — very slow
}


def _tier_defaults(tier: Optional[int]) -> Dict:
    """Tier-aware base settings before structural inspection."""
    return {
        "delay": 1,
        "decay": 4,
        "neutralization": "SUBINDUSTRY",
        "truncation": 0.08,
    }


def smart_simulation_settings(
    expression: str,
    *,
    tier: Optional[int] = None,
    region: str = "USA",
    universe: str = "TOP3000",
    test_period: str = "P2Y0M",
    field_category: Optional[str] = None,
    overrides: Optional[Dict] = None,
) -> Dict:
    """Return BRAIN sim settings dict for an alpha expression.

    Decision flow (each step can override the previous):
      1. Tier base defaults
      2. Structural inspection of the expression's top-level op
         (recurses through `multiply(-1, X)` negation wrappers)
      3. Field category hint
      4. Caller overrides

    Returns a dict with keys:
        region, universe, delay, decay, neutralization, truncation, test_period

    The result can be unpacked directly into BrainAdapter.simulate_alpha:
        await brain.simulate_alpha(expression=expr,
                                    **smart_simulation_settings(expr, tier=2))
    """
    s = _tier_defaults(tier)
    s.update({"region": region, "universe": universe, "test_period": test_period})

    # Step 2 — structural inspection. Negation wrappers are transparent
    # because sign-flip doesn't change which BRAIN settings make sense.
    expr = _strip_outer_parens(expression.strip())
    inner_negated = _is_negation_wrapper(expr)
    if inner_negated is not None:
        expr = _strip_outer_parens(inner_negated)

    parsed = _top_level_call(expr)
    if parsed:
        op = parsed[0]
        if op in _INTRINSIC_NEUT_OPS:
            s["neutralization"] = "NONE"
        elif op == "trade_when":
            s["decay"] = 0
            # T3 trade_when with intrinsic-neut inner — both rules apply
            args = parsed[1]
            if len(args) >= 2:
                inner_t2 = args[1].strip()
                inner_naked = _is_negation_wrapper(inner_t2) or inner_t2
                inner_parsed = _top_level_call(_strip_outer_parens(inner_naked))
                if inner_parsed and inner_parsed[0] in _INTRINSIC_NEUT_OPS:
                    s["neutralization"] = "NONE"

    # Step 3 — field category hint
    if field_category and field_category in _FIELD_DECAY_HINTS:
        s["decay"] = _FIELD_DECAY_HINTS[field_category]

    # Step 4 — explicit overrides win
    if overrides:
        s.update(overrides)

    return s


def settings_reason(
    expression: str,
    tier: Optional[int] = None,
    field_category: Optional[str] = None,
) -> str:
    """One-line explanation of why these settings were chosen.

    Persisted as metadata.sim_settings_reason for downstream auditability
    (post-mortem `which settings combos correlate with PASS?`).
    """
    reasons = []

    expr = _strip_outer_parens(expression.strip())
    inner_negated = _is_negation_wrapper(expr)
    if inner_negated is not None:
        reasons.append("negation-transparent")
        expr = _strip_outer_parens(inner_negated)

    parsed = _top_level_call(expr)
    if parsed:
        op = parsed[0]
        if op in _INTRINSIC_NEUT_OPS:
            reasons.append(f"top-level {op} → neut=NONE")
        elif op == "trade_when":
            reasons.append("trade_when → decay=0")
            args = parsed[1]
            if len(args) >= 2:
                inner_naked = _is_negation_wrapper(args[1].strip()) or args[1].strip()
                inner_parsed = _top_level_call(_strip_outer_parens(inner_naked))
                if inner_parsed and inner_parsed[0] in _INTRINSIC_NEUT_OPS:
                    reasons.append(f"trade_when inner {inner_parsed[0]} → neut=NONE")

    if field_category:
        reasons.append(f"field_category={field_category} → decay={_FIELD_DECAY_HINTS.get(field_category, '?')}")

    if not reasons:
        reasons.append(f"tier={tier} defaults")

    return "; ".join(reasons)
