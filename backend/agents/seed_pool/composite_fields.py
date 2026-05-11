"""V-22.6 (2026-05-12) — Composite field loader + ts_op enumeration.

Loads composite_fields.yaml and produces ready-to-validate T1 candidates that
fuse multi-field arithmetic synthesis with the standard single-input ts_op
enumeration BRAIN expects.

The pipeline this module implements (V-22.6 design):

    1. Author defines arithmetic composite (this loader's input):
           divide(ebit, enterprise_value)        # earnings_yield
    2. Wrap with preprocess to handle sparse-NaN fundamentals + outliers:
           winsorize(ts_backfill(<composite>, 120), std=4)
    3. Enumerate ts_op × window over the preprocessed composite:
           ts_rank(<wrapped>, 20)
           ts_zscore(<wrapped>, 60)
           ...

Callers (factor_generation.expand_t1_strategy) get back a list of candidate
dicts shaped to be appended directly to the candidate pool that goes through
_dedup_and_validate.

Usage:
    from backend.agents.seed_pool.composite_fields import (
        generate_composite_t1_candidates,
    )
    candidates = generate_composite_t1_candidates(
        ts_ops=strategy.preferred_ts_ops,
        windows=windows,
        available_fields=strategy.promising_fields,
        region="USA",
        max_per_composite=2,
    )
"""
from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import yaml

logger = logging.getLogger("agents.composite_fields")

_YAML_PATH = Path(__file__).resolve().parent / "composite_fields.yaml"
_LOADED: Optional[List[Dict[str, Any]]] = None

# Fields that are present in every BRAIN region we currently mine. Composites
# requiring only these don't need LLM-curated promising_fields support.
# Source: confirmed against datafields for USA TOP3000 + universally available
# on CHN / EUR / ASI / GLB (all standard PV).
UNIVERSAL_PV_FIELDS: Set[str] = {
    "close", "open", "high", "low", "volume", "cap", "vwap", "returns",
}

# Region-specific field guards. If a composite requires any field in this set
# for the given region, skip the composite for that region. Conservative: only
# populate when we've verified absence (avoid silent simulation failures).
REGION_BLOCKED_FIELDS: Dict[str, Set[str]] = {
    # USA TOP3000 — all 25 ingredients confirmed present (2026-05-12 probe).
    "USA": set(),
    # Other regions: leave empty pending dedicated field audit.
    "CHN": set(),
    "EUR": set(),
    "ASI": set(),
    "GLB": set(),
}

# Preprocess parameters. Match the V-22.6 design: ts_backfill 120 days +
# winsorize ±4 σ. Surface as constants so tests / callers can override.
DEFAULT_BACKFILL_WINDOW = 120
DEFAULT_WINSORIZE_STD = 4


def _load() -> List[Dict[str, Any]]:
    global _LOADED
    if _LOADED is not None:
        return _LOADED
    try:
        with _YAML_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        if not isinstance(data, list):
            logger.error(
                f"[composite_fields] YAML root must be a list, got {type(data).__name__}"
            )
            _LOADED = []
            return _LOADED
        _LOADED = [c for c in data if isinstance(c, dict) and c.get("name")]
        logger.info(
            f"[composite_fields] loaded {len(_LOADED)} composites from {_YAML_PATH.name}"
        )
    except FileNotFoundError:
        logger.warning(f"[composite_fields] YAML not found at {_YAML_PATH}; using empty pool")
        _LOADED = []
    except Exception as e:
        logger.error(f"[composite_fields] failed to load YAML: {e}")
        _LOADED = []
    return _LOADED


def reload() -> None:
    """Force reload from disk (test / hot-reload helper)."""
    global _LOADED
    _LOADED = None
    _load()


def list_composites() -> List[Dict[str, Any]]:
    """Return all composite definitions (immutable copy)."""
    return [dict(c) for c in _load()]


def wrap_with_preprocess(
    composite_expr: str,
    backfill_window: int = DEFAULT_BACKFILL_WINDOW,
    winsorize_std: int = DEFAULT_WINSORIZE_STD,
) -> str:
    """Wrap a raw composite expression with backfill + winsorize.

    Output shape: winsorize(ts_backfill(<expr>, <window>), std=<std>)

    Rationale: composites built from fundamentals (eps, ebit, cfo, ...) carry
    significant point-in-time NaN gaps between quarterly reports. ts_backfill
    forward-fills the latest valid observation up to N days. winsorize trims
    pathological ratios (e.g. divide by near-zero denominator).
    """
    return f"winsorize(ts_backfill({composite_expr}, {backfill_window}), std={winsorize_std})"


def _composite_is_eligible(
    composite: Dict[str, Any],
    available_set: Set[str],
    region: str,
) -> bool:
    """A composite is eligible if every required_field is either universally
    available OR present in the caller-supplied available_fields set, AND no
    required_field is region-blocked.
    """
    required = set(composite.get("required_fields") or [])
    if not required:
        return False
    blocked = REGION_BLOCKED_FIELDS.get(region, set())
    if required & blocked:
        return False
    return required.issubset(UNIVERSAL_PV_FIELDS | available_set)


def generate_composite_t1_candidates(
    ts_ops: Iterable[str],
    windows: Iterable[int],
    available_fields: Iterable[str],
    region: str = "USA",
    max_per_composite: int = 2,
    backfill_window: int = DEFAULT_BACKFILL_WINDOW,
    winsorize_std: int = DEFAULT_WINSORIZE_STD,
) -> List[Dict[str, Any]]:
    """Build T1 candidate dicts from composite fields × ts_ops × windows.

    Args:
        ts_ops: Strategy-selected ts_* operators (e.g. ["ts_rank", "ts_zscore"]).
        windows: Strategy-selected windows (e.g. [20, 60]).
        available_fields: Promising fields from T1Strategy — used to gate
            composites that depend on non-universal fundamental fields.
        region: BRAIN region — drives REGION_BLOCKED_FIELDS guards.
        max_per_composite: Sample cap per composite. With 8 ts_ops × 5 windows
            = 40 raw combos per composite, capping at 2 keeps the candidate
            pool balanced across composites instead of flooding it with one.
        backfill_window: ts_backfill window in the preprocess wrapper.
        winsorize_std: winsorize std bound in the preprocess wrapper.

    Returns:
        List of candidate dicts shaped for _dedup_and_validate:
            {expression, field, op, window}
        where `field` encodes the composite name (so stratified_sample can
        balance across composites) and `op` is the outer ts_op.

    The returned list is NOT validated — caller (expand_t1_strategy) hands it
    to _dedup_and_validate alongside its other candidates so all tier-1
    classification + region semantic checks run uniformly.
    """
    composites = _load()
    if not composites:
        return []

    ts_op_list = list(ts_ops)
    window_list = list(windows)
    available_set = set(available_fields or [])

    if not ts_op_list or not window_list:
        return []

    out: List[Dict[str, Any]] = []
    skipped_unavailable = 0
    for composite in composites:
        if not _composite_is_eligible(composite, available_set, region):
            skipped_unavailable += 1
            continue

        name = composite["name"]
        wrapped = wrap_with_preprocess(
            composite["composite_expr"],
            backfill_window=backfill_window,
            winsorize_std=winsorize_std,
        )

        combos = [
            {
                "expression": f"{op}({wrapped}, {w})",
                # Encode composite name into `field` for traceability.
                "field": f"_composite_{name}",
                # Prefix the outer op with "composite_" so stratified_sample's
                # `by="op"` bucketing gives composites their own bucket family
                # parallel to raw ts_op buckets — without this composites and
                # bare ts_op single-field candidates share buckets and the
                # numerically dominant single-field side crowds them out.
                "op": f"composite_{op}",
                "window": w,
            }
            for op in ts_op_list
            for w in window_list
        ]
        random.shuffle(combos)
        out.extend(combos[:max_per_composite])

    if skipped_unavailable:
        logger.debug(
            f"[composite_fields] region={region} skipped {skipped_unavailable}/{len(composites)} "
            f"composites (required fields not in available pool)"
        )
    if out:
        logger.info(
            f"[composite_fields] generated {len(out)} composite candidates "
            f"({len(out)//max_per_composite} composites × {max_per_composite} each)"
        )
    return out


def total_composite_count() -> int:
    """Total composites defined in YAML (loaded)."""
    return len(_load())
