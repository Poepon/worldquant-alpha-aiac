"""Portfolio-aware prompt injection — submitted alpha skeletons.

Goal: stop LLM from re-generating already-submitted shapes. Instead of
hard-banning themes (which would permanently kill productive paths),
inject a "your account already has these patterns" section into the
T1 strategy prompt. LLM uses it as soft guidance — fine to deviate when
a strong signal warrants, but defaults toward unexplored shapes.

Implementation
--------------
Source: local DB `alphas` table, filtered by `date_submitted IS NOT NULL`
and matching region. No BRAIN call needed (we already have these rows).
Skeleton computation: existing `expression_to_skeleton` (depth=3).

Cache file: backend/data/correlation_cache/submitted_portfolio_{region}.json
- Refreshed by `submit_alpha.py` post-success hook
- Refreshed by standalone `refresh_corr_cache.py`
- Lazy-loaded at prompt build time (~10ms file IO)

Why a cache file (vs DB query at prompt build time):
1. `build_t1_strategy_user_prompt` is sync; threading async DB session
   through the factor_generation call chain is invasive.
2. Portfolio changes only on submit (rare); a stale-by-one-submit cache
   is fine — the post-submit hook keeps it fresh.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "correlation_cache"


def _cache_path(region: str) -> Path:
    return CACHE_DIR / f"submitted_portfolio_{region}.json"


def load_portfolio(region: str = "USA") -> List[Dict]:
    """Read cached submitted-portfolio skeletons. Empty list if no cache."""
    path = _cache_path(region)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("portfolio", [])
    except Exception as e:
        logger.warning(f"[portfolio_skeletons] Failed to load {path}: {e}")
        return []


async def refresh_portfolio_from_db(region: str = "USA") -> int:
    """Query DB for region's submitted alpha, compute skeletons, persist.

    Returns: count of rows written.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.knowledge_extraction import expression_to_skeleton

    e = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"
    )
    try:
        async with e.begin() as c:
            r = await c.execute(text("""
                SELECT alpha_id, expression, region,
                       (metrics->>'sharpe')::float AS sharpe,
                       (metrics->>'fitness')::float AS fitness,
                       date_submitted
                FROM alphas
                WHERE date_submitted IS NOT NULL
                  AND alpha_id IS NOT NULL
                  AND region = :region
                ORDER BY date_submitted ASC
            """), {"region": region})
            rows = r.fetchall()
    finally:
        await e.dispose()

    portfolio = []
    for row in rows:
        try:
            skeleton = expression_to_skeleton(row.expression, max_depth=3)
        except Exception:
            skeleton = "UNKNOWN"
        portfolio.append({
            "alpha_id": row.alpha_id,
            "skeleton": skeleton,
            "expression": row.expression,
            "sharpe": row.sharpe,
            "fitness": row.fitness,
            "date_submitted": str(row.date_submitted) if row.date_submitted else None,
        })

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "region": region,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "portfolio": portfolio,
    }
    with _cache_path(region).open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    logger.info(
        f"[portfolio_skeletons] {region} portfolio refreshed: {len(portfolio)} alpha"
    )
    return len(portfolio)


def format_block(portfolio: List[Dict], max_entries: int = 30) -> str:
    """Render portfolio as a soft-guidance prompt section.

    Groups by skeleton (some alpha share skeleton). Caps display to
    avoid blowing up prompt length on accounts with 100+ submissions.
    """
    if not portfolio:
        return ""

    # Group by skeleton; show distinct skeletons + their alpha sharpe range.
    by_skel: Dict[str, List[Dict]] = {}
    for entry in portfolio:
        by_skel.setdefault(entry.get("skeleton", "UNKNOWN"), []).append(entry)

    lines = []
    for skel, entries in sorted(by_skel.items(), key=lambda x: -len(x[1])):
        if len(lines) >= max_entries:
            lines.append(f"  - ... ({len(by_skel) - max_entries} more skeletons truncated)")
            break
        sharpes = [e.get("sharpe", 0) or 0 for e in entries]
        sh_min, sh_max = min(sharpes), max(sharpes)
        sh_range = f"sh={sh_min:.2f}" if sh_min == sh_max else f"sh={sh_min:.2f}-{sh_max:.2f}"
        # Use one canonical example expression (the highest-sharpe entry)
        best = max(entries, key=lambda e: (e.get("sharpe") or 0))
        expr_short = (best.get("expression") or "")[:80]
        lines.append(f"  - {skel}  ({len(entries)}× {sh_range})")
        lines.append(f"    e.g.: {expr_short}")

    body = "\n".join(lines)
    return f"""
Your account ALREADY HAS SUBMITTED alpha on these skeletons (BRAIN
self-correlation gate at submit-time will reject duplicates from a
correlated shape unless new sharpe is >10% better):

{body}

GUIDANCE: For this round, prefer field families and op-tree shapes that
are NOT in the list above. Same fields with novel op trees are fine; same
op tree on same fields is what we want to avoid. The goal is portfolio
diversification, not exclusion of any theme — if a strong signal warrants
revisiting an existing skeleton with substantially better sharpe, do it.
"""


def get_portfolio_block(region: str = "USA") -> str:
    """Convenience: load + format. Used by build_t1_strategy_user_prompt."""
    portfolio = load_portfolio(region)
    return format_block(portfolio)


def get_portfolio_skeleton_set(region: str = "USA") -> set[str]:
    """Return set of submitted-alpha skeletons for fast duplicate check.

    Used by node_simulate as a pre-simulate hard gate: any candidate whose
    skeleton matches the submitted portfolio's skeletons is structurally
    near-duplicate of an already-submitted alpha and would fail BRAIN's
    self-correlation gate at submission. Skip the BRAIN simulate to save
    the API call.
    """
    portfolio = load_portfolio(region)
    return {entry.get("skeleton", "") for entry in portfolio if entry.get("skeleton")}


def is_skeleton_in_portfolio(expression: str, region: str = "USA") -> bool:
    """True if expression's skeleton matches any submitted-portfolio
    skeleton. Caller should treat True as "high self-corr risk, skip simulate"."""
    if not expression:
        return False
    try:
        from backend.knowledge_extraction import expression_to_skeleton
        sk = expression_to_skeleton(expression, max_depth=3)
    except Exception:
        return False
    return sk in get_portfolio_skeleton_set(region)
