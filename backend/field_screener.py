"""Field screener (PR-B): pick an under-explored TARGET field for a cell + build
the generation injection. The steering half of the orthogonal-breadth loop.

``pick_target_field`` queries the PR-A ledger (``datafield_cell_stats`` + the
mining-stat columns) for the cell's least-mined available signal fields, then
proportional-samples ONE via ``field_selector`` (novelty × signal_quality, see
design §0.2). The HG generation node prepends the returned field to its code-gen
field list, steering the LLM off the ~886 crowded fields.

Gated by the scheduler (ENABLE_FIELD_SCREENING) — this module is only reached on
that path; flag-OFF ⇒ never called ⇒ byte-for-byte legacy mining.

Replaces the ``field_screener.py`` deleted in b89b732 (which was a FLAT-era
heuristic with no per-field ledger). 口径 = IS/local (BRAIN OS hidden).
"""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from backend.field_selector import sample_target_field


async def pick_target_field(
    session: Any, *, dataset_id: str, region: str, universe: str, delay: int,
    top_k: int = 50, novelty_floor: float = 0.05,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """Return one under-explored target field dict for the cell, or None.

    Candidate pool = the cell's least-mined ACTIVE signal fields (MATRIX/VECTOR,
    field-hygiene), top_k by times_mined ASC (untouched first). Then sample ∝
    field_score. Returns {field_id, field_name, description, field_type,
    times_mined, signal_p90, band_pass_count, _field_score}.
    """
    rows = (await session.execute(text(
        """
        SELECT df.field_id, df.field_name, df.description, df.field_type,
               COALESCE(c.times_mined, 0) AS times_mined,
               c.signal_p90 AS signal_p90,
               COALESCE(c.band_pass_count, 0) AS band_pass_count
        FROM datafield_cell_stats c
        JOIN datafields df ON df.id = c.datafield_ref
        JOIN datasets d ON d.id = df.dataset_id
        WHERE d.region = :r AND d.dataset_id = :ds
          AND c.universe = :u AND c.delay = :dl AND c.is_active IS TRUE
          AND df.field_type IN ('MATRIX', 'VECTOR')
          AND df.field_id NOT ILIKE '%_time_utc%'
          AND df.field_id NOT ILIKE '%_date_utc%'
          AND df.field_id NOT ILIKE '%iso_code%'
        ORDER BY COALESCE(c.times_mined, 0) ASC, df.field_id
        LIMIT :k
        """
    ), {"r": region, "ds": dataset_id, "u": universe, "dl": int(delay), "k": int(top_k)})).mappings().all()
    candidates: List[Dict[str, Any]] = [dict(r) for r in rows]
    if not candidates:
        return None
    return sample_target_field(candidates, novelty_floor=novelty_floor, rng=rng)


def field_injection_block(field: Dict[str, Any]) -> str:
    """A compact instruction steering code-gen to build its alpha AROUND the
    target field (prepended to the code-gen field roster by the generation node).
    """
    fid = field.get("field_id", "?")
    fname = field.get("field_name") or fid
    desc = (field.get("description") or "")[:160]
    return (
        f"[FIELD-EXPLORE] Build the alpha primarily around the under-explored "
        f"field `{fid}` ({fname}). {desc} "
        f"Use standard operators on it; this targets orthogonal breadth."
    )
