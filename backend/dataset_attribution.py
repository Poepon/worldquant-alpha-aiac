"""Derive an alpha's dataset_id from its fields (FLAT path leaves it NULL).

The hypothesis-driven FLAT path mines a cross-dataset field union, so the
legacy single-value ``MiningState.dataset_id`` (designed for the ONESHOT
single-dataset model) stays empty — persistence then stamps ``alpha.dataset_id``
NULL (verified 2026-05-22: 100% of FLAT-task alphas have dataset_id IS NULL,
which makes the dataset dimension invisible to dataset-level steering /
field-screening / the bandit). The dataset is recoverable per-alpha from
``fields_used`` via the datafields catalog (field_id → DatasetMetadata.dataset_id
NAME, e.g. "fundamental2"/"pv1", matching what ONESHOT stores).

Empirically ~95% of FLAT alphas use a single dataset; for the rare cross-dataset
alpha we attribute the DOMINANT dataset (most contributing fields), with an
alphabetical tie-break for determinism.

Used by:
  - persistence._incremental_save_alphas — stamp at INSERT when dataset_id empty (B)
  - scripts/backfill_alpha_dataset_id.py — retroactive fill of NULL rows (A)
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Dict, Optional, Sequence

# (region, universe) -> (built_ts, {field_id_lower: dataset_name}). TTL-cached so
# a per-round persist doesn't re-query the ~6k-row datafields catalog every
# batch. The map is small and stable within a session.
_MAP_CACHE: Dict[tuple, tuple] = {}
_MAP_TTL_S = 1800  # 30 min


def _clear_cache() -> None:  # test helper
    _MAP_CACHE.clear()


async def build_field_dataset_map(db, region: str, universe: str) -> Dict[str, str]:
    """Return ``{field_id_lower: dataset_name}`` for (region, universe).

    dataset_name = ``DatasetMetadata.dataset_id`` (the human name like
    "fundamental2"), NOT ``DataField.dataset_id`` (the metadata-row FK int) —
    so derived values match what the ONESHOT path stores in alphas.dataset_id.

    Soft-fails to ``{}`` so callers degrade to leaving dataset_id NULL — this
    must never break persistence. TTL-cached per (region, universe).
    """
    key = (region, universe)
    cached = _MAP_CACHE.get(key)
    if cached and (time.time() - cached[0]) < _MAP_TTL_S:
        return cached[1]
    try:
        from sqlalchemy import select

        from backend.models import DataField, DatasetMetadata

        stmt = (
            select(DataField.field_id, DatasetMetadata.dataset_id)
            .join(DatasetMetadata, DataField.dataset_id == DatasetMetadata.id)
            .where(
                DatasetMetadata.region == region,
                DatasetMetadata.universe == universe,
            )
        )
        rows = (await db.execute(stmt)).all()
        m: Dict[str, str] = {}
        for fid, dsname in rows:
            if fid and dsname:
                m.setdefault(str(fid).lower(), dsname)
        _MAP_CACHE[key] = (time.time(), m)
        return m
    except Exception:
        return {}


def derive_dataset_id(
    field_ids: Optional[Sequence[str]], field_map: Dict[str, str]
) -> Optional[str]:
    """Dominant dataset (most contributing fields) for an alpha's fields.

    Returns None when no field maps (caller keeps dataset_id NULL). Ties are
    broken alphabetically for deterministic, reproducible attribution.
    """
    if not field_ids or not field_map:
        return None
    counts: Counter = Counter()
    for f in field_ids:
        ds = field_map.get(str(f).lower())
        if ds:
            counts[ds] += 1
    if not counts:
        return None
    best_n = max(counts.values())
    return sorted(d for d, n in counts.items() if n == best_n)[0]


def resolve_dataset_id(
    field_ids: Optional[Sequence[str]],
    field_map: Dict[str, str],
    anchor: Optional[str] = None,
) -> Optional[str]:
    """Attribute an alpha to the dataset of its ACTUAL fields, falling back to
    the FLAT/ONESHOT ``anchor`` only when no field resolves.

    a-fix 2026-05-23: a cross-dataset hypothesis (HYPOTHESIS_CENTRIC) anchors on
    one dataset but the LLM may generate fields from another; stamping the anchor
    mis-attributes the alpha and corrupts the dataset bandit's per-dataset reward.
    Fields are ground truth → derive wins. For ONESHOT (anchor == mined dataset)
    derive returns the same value, so the anchor fallback is a no-op there; it
    only kicks in on a catalog gap / fieldless expression.
    """
    return derive_dataset_id(field_ids, field_map) or anchor
