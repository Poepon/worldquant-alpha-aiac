"""Field-fitness stats — historical fit median per field, used in T1
strategy prompt to nudge LLM toward high-fit field families.

Why: bottleneck in current mining is alpha producing PASS-grade sharpe
but fitness < 1.0 (BRAIN's submission gate). Some BRAIN fields are
structurally fitness-friendly (anl4_adjusted_netincome_ft → med fit
2.22) and some never reach fit=1.0 regardless of op tree. Without
historical stats, LLM picks "interesting" fields that may have low fit
ceiling.

Implementation
--------------
Source: local DB `alphas` table — `fields_used` JSONB column already
populated. For each field, aggregate fit median + count where fit≥1.0.
DB-only refresh, no BRAIN call.

Cache: backend/data/correlation_cache/field_fitness_{region}.json
Refresh hooks:
  - submit_alpha.py post-submit (after each new alpha enters DB)
  - scripts/refresh_corr_cache.py per-region

Filter: matches the same CW-prone patterns as anti-CW filter so the
high-fit list doesn't recommend fields the LLM will never see anyway
(deadlock prevention).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from loguru import logger

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "correlation_cache"

# Mirror the anti-CW filter patterns from strategy_prompts.py — keep in sync.
_CW_PRONE_PATTERNS = (
    "_derivative", "implied_volatility_", "_bbg_", "_pyth_", "_dvd_cash_",
)


def _is_cw_prone(field_id: str) -> bool:
    flow = (field_id or "").lower()
    return any(p in flow for p in _CW_PRONE_PATTERNS)


def _cache_path(region: str) -> Path:
    return CACHE_DIR / f"field_fitness_{region}.json"


def load_high_fit_fields(region: str = "USA") -> List[Dict]:
    path = _cache_path(region)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("fields", [])
    except Exception as e:
        logger.warning(f"[field_fitness] load fail {path}: {e}")
        return []


async def refresh_field_fitness_cache(
    region: str = "USA",
    min_alpha_count: int = 3,
    top_n: int = 20,
) -> int:
    """Compute per-field fit median + cache top-N.

    Filters out CW-prone fields (LLM won't see them due to anti-CW filter)
    and fields with too few alpha for stable median. Returns count saved.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    e = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"
    )
    try:
        async with e.begin() as c:
            r = await c.execute(text("""
                WITH per_field AS (
                    SELECT
                        jsonb_array_elements_text(a.fields_used) AS field_id,
                        (a.metrics->>'fitness')::float AS fit
                    FROM alphas a
                    WHERE a.fields_used IS NOT NULL
                      AND a.region = :region
                      AND a.metrics ? 'fitness'
                      AND (a.metrics->>'fitness')::float IS NOT NULL
                )
                SELECT
                    field_id,
                    COUNT(*) AS n,
                    ROUND(AVG(fit)::numeric, 2) AS avg_fit,
                    ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY fit)::numeric, 2) AS med_fit,
                    ROUND(MAX(fit)::numeric, 2) AS max_fit,
                    COUNT(*) FILTER (WHERE fit >= 1.0) AS n_ge_1
                FROM per_field
                GROUP BY field_id
                HAVING COUNT(*) >= :min_n
                ORDER BY med_fit DESC, n DESC
            """), {"region": region, "min_n": min_alpha_count})
            rows = r.fetchall()
    finally:
        await e.dispose()

    fields = []
    for row in rows:
        fid = row.field_id
        if _is_cw_prone(fid):
            continue
        if (row.med_fit or 0) <= 0:
            continue
        fields.append({
            "field_id": fid,
            "n_alpha": row.n,
            "median_fit": float(row.med_fit) if row.med_fit is not None else 0.0,
            "avg_fit": float(row.avg_fit) if row.avg_fit is not None else 0.0,
            "max_fit": float(row.max_fit) if row.max_fit is not None else 0.0,
            "n_fit_ge_1": row.n_ge_1,
        })
        if len(fields) >= top_n:
            break

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "region": region,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "min_alpha_count": min_alpha_count,
        "fields": fields,
    }
    with _cache_path(region).open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    logger.info(
        f"[field_fitness] {region}: cached {len(fields)} high-fit fields "
        f"(filter CW-prone, min_n={min_alpha_count})"
    )
    return len(fields)


def format_block(fields: List[Dict], min_median: float = 0.7) -> str:
    """Render top high-fit fields as a soft-guidance prompt section.

    Only shows fields with median fit >= min_median to avoid recommending
    mediocre fields. Caller can adjust min_median per round if desired.
    """
    if not fields:
        return ""
    sorted_fields = sorted(
        [f for f in fields if (f.get("median_fit") or 0) >= min_median],
        key=lambda f: -(f.get("median_fit") or 0),
    )
    if not sorted_fields:
        return ""

    lines = []
    for f in sorted_fields[:15]:
        fid = f["field_id"]
        med = f["median_fit"]
        n = f["n_alpha"]
        n_ge_1 = f["n_fit_ge_1"]
        lines.append(f"  - {fid:<55} median_fit={med:.2f}  n={n}  fit≥1.0: {n_ge_1}/{n}")
    body = "\n".join(lines)
    return f"""
HIGH-FITNESS field history (mined alpha aggregated, median fit ≥ {min_median}):
{body}

GUIDANCE: When picking promising_fields, PRIORITIZE field families above —
these have empirically delivered fit ≥ 1.0 in production. The current
fitness gap (PROV alphas often fit 0.6-0.9) is the biggest BRAIN-submit
blocker; high-fit fields are the most efficient way to clear it.
"""


def get_high_fit_block(region: str = "USA") -> str:
    """Convenience: load + format. Used by build_t1_strategy_user_prompt."""
    fields = load_high_fit_fields(region)
    return format_block(fields)
