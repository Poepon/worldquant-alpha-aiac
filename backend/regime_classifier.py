"""P2-C regime classifier (2026-05-16) — pure function module.

来源: docs/alphagbm_skills_research_2026-05-15.md skills `vix-status` +
`duan-analysis`.

This module is dependency-free (no DB, no LLM, no Redis); the consumers
that orchestrate it (services/regime_inference_service +
agents/graph/nodes/evaluation) live elsewhere.

5-bucket regime taxonomy + per-regime threshold multipliers + style preset
dicts. The multipliers are applied in ``apply_regime_multipliers``; the
style preset block is rendered out of this module by
``backend.agents.prompts.base.build_style_preset_block`` (S4 — DRY,
single rendering site).

Key invariants:
    * ``REGIME_ORDER`` and ``REGIME_PRESETS`` MUST have identical key sets
      (N1 assertion at module load).
    * ``apply_regime_multipliers`` is **side-effect free**: it neither writes
      ``_regime_applied`` into the returned dict (S8) nor mutates the input.
    * ``score_optimize`` is **NOT** multiplied (MF6) — the OPTIMIZE lower
      bound stays stable across regimes; only ``score_pass`` shifts.
    * ``apply_ewma_smoothing`` returns "normal" when history has <2 entries
      (cold-start path; downstream tags ``cold_start=True``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class RegimePreset:
    """Static per-regime preset bundle.

    * ``*_multiplier`` fields are applied to base tier thresholds inside
      ``apply_regime_multipliers``.
    * ``style_label`` / ``style_philosophy`` are LLM-facing strings rendered
      by ``build_style_preset_block``.
    * ``pillar_bias`` is a **soft** suggestion (prompt-level only); the
      hard pillar rebalance signal lives in P2-B ``pillar_hint`` and the
      two operate independently (see ``build_style_preset_block`` doc).
    """
    regime: str
    sharpe_multiplier: float
    fitness_multiplier: float
    turnover_multiplier: float
    score_pass_multiplier: float
    style_label: str
    style_philosophy: str
    pillar_bias: Tuple[str, ...]


# Ordinal order (crisis → very_calm) — used by EWMA smoothing rounding.
REGIME_ORDER: List[str] = ["crisis", "elevated", "normal", "calm", "very_calm"]


REGIME_PRESETS: Dict[str, RegimePreset] = {
    "crisis": RegimePreset(
        regime="crisis",
        sharpe_multiplier=0.70,       # T1 1.25 → 0.875
        fitness_multiplier=0.75,
        turnover_multiplier=1.15,     # 0.70 → 0.805 (allow higher turnover)
        score_pass_multiplier=0.85,   # 0.80 → 0.68
        style_label="Risk-Off Defensive",
        style_philosophy=(
            "Capital preservation over alpha hunting. Favour low-beta, "
            "low-turnover, quality and defensive value signals; treat "
            "any high-sharpe candidate as suspicious until proven."
        ),
        pillar_bias=("quality", "value", "volatility"),
    ),
    "elevated": RegimePreset(
        regime="elevated",
        sharpe_multiplier=0.85,
        fitness_multiplier=0.90,
        turnover_multiplier=1.08,
        score_pass_multiplier=0.93,
        style_label="Cautious Tactical",
        style_philosophy=(
            "Stress-test ahead of full conviction. Mix mean-reversion "
            "with selective momentum; require multi-pillar agreement."
        ),
        pillar_bias=("quality", "momentum", "volatility"),
    ),
    "normal": RegimePreset(
        regime="normal",
        sharpe_multiplier=1.00,
        fitness_multiplier=1.00,
        turnover_multiplier=1.00,
        score_pass_multiplier=1.00,
        style_label="Balanced",
        style_philosophy=(
            "Pursue the strongest economic mechanism without a regime "
            "tilt. All five pillars are fair game; let the data decide."
        ),
        pillar_bias=("momentum", "value", "quality"),
    ),
    "calm": RegimePreset(
        regime="calm",
        sharpe_multiplier=1.15,
        fitness_multiplier=1.10,
        turnover_multiplier=0.95,
        score_pass_multiplier=1.05,
        style_label="Constructive",
        style_philosophy=(
            "Lean into positive carry and trend-following. Tighten the "
            "bar — abundant beta means weaker signals will look strong."
        ),
        pillar_bias=("momentum", "sentiment", "value"),
    ),
    "very_calm": RegimePreset(
        regime="very_calm",
        sharpe_multiplier=1.30,
        fitness_multiplier=1.20,
        turnover_multiplier=0.92,
        score_pass_multiplier=1.10,
        style_label="Aggressive Growth",
        style_philosophy=(
            "Overfit risk is highest here. Reach for differentiated "
            "structure (cross-dataset, sentiment, growth)."
        ),
        pillar_bias=("sentiment", "momentum", "quality"),
    ),
}


# N1: invariant. ``REGIME_ORDER`` must mirror ``REGIME_PRESETS`` keys exactly
# so EWMA rounding never lands on a label that has no preset.
assert set(REGIME_ORDER) == set(REGIME_PRESETS.keys()), (
    "REGIME_ORDER must match REGIME_PRESETS keys (P2-C invariant)"
)


def classify_pass_rate_to_regime(pass_rate_7d: float) -> str:
    """Map a 7-day alpha-library PASS-rate to a regime bucket.

    Bucket boundaries are strict ``<`` so 0.05 lands in ``elevated``, not
    ``crisis`` (C2 boundary test). Inputs outside [0, 1] are clamped to the
    nearest boundary by the comparison logic (NaN-safe via the cascade).
    """
    try:
        v = float(pass_rate_7d)
    except (TypeError, ValueError):
        return "normal"
    if v < 0.05:
        return "crisis"
    if v < 0.10:
        return "elevated"
    if v < 0.20:
        return "normal"
    if v < 0.30:
        return "calm"
    return "very_calm"


def apply_ewma_smoothing(
    history: List[str],
    alpha: float = 0.3,
) -> str:
    """Smooth a chronological list of daily regime labels into one regime.

    Args:
        history: oldest-first list of regime labels (must be members of
                 ``REGIME_ORDER``; unknown labels treated as ``normal``).
        alpha: EWMA decay (0..1). Higher = react faster to recent days.

    Returns:
        The rounded ordinal regime label. With fewer than 2 entries we
        return ``"normal"`` (C3 cold-start) — the caller stamps
        ``cold_start=True`` separately.

    Implementation: map each label to its ordinal index in REGIME_ORDER,
    run EWMA on the integer sequence, round to nearest int, clamp to
    [0, len-1], then map back to a label.
    """
    if not history or len(history) < 2:
        return "normal"
    idx_map = {r: i for i, r in enumerate(REGIME_ORDER)}
    series = [idx_map.get(r, idx_map["normal"]) for r in history]
    ewma_val: float = float(series[0])
    a = float(alpha)
    for x in series[1:]:
        ewma_val = a * float(x) + (1.0 - a) * ewma_val
    rounded = int(round(ewma_val))
    rounded = max(0, min(len(REGIME_ORDER) - 1, rounded))
    return REGIME_ORDER[rounded]


def apply_regime_multipliers(
    base_thresholds: Dict,
    regime: Optional[str],
) -> Dict:
    """Return a NEW dict with regime multipliers applied to base thresholds.

    Only the following keys are scaled (MF6 — ``score_optimize`` is
    intentionally NOT multiplied to keep the OPTIMIZE lower bound stable
    across regimes):

        * ``sharpe_min``    — × ``sharpe_multiplier``
        * ``fitness_min``   — × ``fitness_multiplier``
        * ``turnover_max``  — × ``turnover_multiplier``
        * ``score_pass``    — × ``score_pass_multiplier`` (if present)

    The ``provisional`` sub-dict (PROV thresholds — looser bar for KB/GA
    seeds) has the same three fields scaled by the same factors, so the
    PROV band moves with PASS.

    Identity behaviour:
        * ``regime is None`` → returns a shallow copy of ``base_thresholds``
        * ``regime == "normal"`` → multipliers are 1.0 → numerically identical
        * unknown ``regime`` (not in REGIME_PRESETS) → returns a shallow copy

    No ``_regime_applied`` side-effect key is written (S8 — the audit stamp
    lives on ``alpha.metrics`` in node_evaluate, not on the tier_cfg dict).
    """
    if regime is None:
        return dict(base_thresholds)
    preset = REGIME_PRESETS.get(regime)
    if preset is None:
        return dict(base_thresholds)

    out: Dict = dict(base_thresholds)
    if "sharpe_min" in out and out["sharpe_min"] is not None:
        out["sharpe_min"] = float(out["sharpe_min"]) * preset.sharpe_multiplier
    if "fitness_min" in out and out["fitness_min"] is not None:
        out["fitness_min"] = float(out["fitness_min"]) * preset.fitness_multiplier
    if "turnover_max" in out and out["turnover_max"] is not None:
        out["turnover_max"] = float(out["turnover_max"]) * preset.turnover_multiplier
    if "score_pass" in out and out["score_pass"] is not None:
        out["score_pass"] = float(out["score_pass"]) * preset.score_pass_multiplier
    # score_optimize: intentionally not scaled (MF6).

    prov = base_thresholds.get("provisional")
    if isinstance(prov, dict):
        prov_out = dict(prov)
        if "sharpe_min" in prov_out and prov_out["sharpe_min"] is not None:
            prov_out["sharpe_min"] = float(prov_out["sharpe_min"]) * preset.sharpe_multiplier
        if "fitness_min" in prov_out and prov_out["fitness_min"] is not None:
            prov_out["fitness_min"] = float(prov_out["fitness_min"]) * preset.fitness_multiplier
        if "turnover_max" in prov_out and prov_out["turnover_max"] is not None:
            prov_out["turnover_max"] = float(prov_out["turnover_max"]) * preset.turnover_multiplier
        out["provisional"] = prov_out

    return out
