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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from loguru import logger

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "correlation_cache"

# ---------------------------------------------------------------------------
# Two-factor skeleton match (V-26.77 follow-up #4, 2026-05-14)
#
# Skeleton-only matching is lossy — `group_rank(divide(FIELD, FIELD), FIELD)`
# catches every two-field ratio (PE / accrual quality / debt ratio …) even
# though their PnL correlations differ wildly. To kill the over-match we
# require skeleton == AND fields_used set == AND numeric params within ±20%
# before declaring "near-duplicate, skip simulate". Anything weaker re-enters
# the BRAIN simulate queue and the post-simulate PnL gate makes the final
# call.
# ---------------------------------------------------------------------------
_FUNC_RE = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(')
_IDENT_RE = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b')
_NUM_RE = re.compile(r'-?\d+(?:\.\d+)?')

# Group tokens and BRAIN reserved keyword args — never user fields. Kept in
# sync with AlphaSemanticValidator._extract_fields.
_NON_FIELD_TOKENS: FrozenSet[str] = frozenset({
    "true", "false", "nan", "inf",
    "sector", "subindustry", "industry", "exchange", "country", "market",
    "std", "k", "mode", "lag", "rettype", "filter", "scale", "rate",
    "constant", "percentage", "driver", "sigma", "lower", "upper",
    "target", "dest", "event", "sensitivity", "force", "h", "t", "period",
    "stddev", "factor", "usetd", "limit", "gaussian", "uniform", "cauchy",
    "buckets", "range", "nth", "precise", "longscale", "shortscale",
})

# Relative tolerance for "same window family". 20% bands {20,21,22,23,24} as
# one cluster; 60 vs 120 as different. Empirically ts windows 5/20/60/252 are
# the canonical buckets and their inter-bucket gaps are well above 20%.
NUMERIC_REL_TOL: float = 0.2


def _expr_fields_and_numerics(expr: str) -> Tuple[FrozenSet[str], Tuple[float, ...]]:
    """Cheap regex-based fields/numerics extractor.

    Mirrors AlphaSemanticValidator._extract_fields semantics without taking
    a dependency on the validator class (which has a heavier init).
    """
    operators = {m.group(1).lower() for m in _FUNC_RE.finditer(expr)}
    fields = set()
    for m in _IDENT_RE.finditer(expr):
        ident = m.group(1)
        low = ident.lower()
        if low in operators or low in _NON_FIELD_TOKENS:
            continue
        fields.add(ident)
    numerics = tuple(float(m.group(0)) for m in _NUM_RE.finditer(expr))
    return frozenset(fields), numerics


def _numerics_match(a: Tuple[float, ...], b: Tuple[float, ...], rel_tol: float = NUMERIC_REL_TOL) -> bool:
    """Same length, each position within `rel_tol` of the larger magnitude."""
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        scale = max(abs(x), abs(y), 1.0)
        if abs(x - y) / scale > rel_tol:
            return False
    return True


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


def get_portfolio_skeleton_index(
    region: str = "USA",
) -> Dict[str, List[Tuple[FrozenSet[str], Tuple[float, ...], str]]]:
    """Skeleton → list of (fields_set, numerics, alpha_id) for two-factor match.

    Used by node_simulate's pre-simulate gate (V-26.77 follow-up #4). The
    pure skeleton-set lookup over-matches: every `divide(FIELD, FIELD)` shape
    collapses to one bucket, but PE-style and accrual-quality alphas inside
    that bucket have very different PnL. Two-factor refinement (fields set
    equal + numerics within ±20%) drops the false-positive rate without
    losing the cheap O(1) skeleton prefilter.
    """
    portfolio = load_portfolio(region)
    out: Dict[str, List[Tuple[FrozenSet[str], Tuple[float, ...], str]]] = {}
    for entry in portfolio:
        sk = entry.get("skeleton")
        expr = entry.get("expression")
        if not sk or not expr:
            continue
        try:
            fields, numerics = _expr_fields_and_numerics(expr)
        except Exception:
            continue
        out.setdefault(sk, []).append((fields, numerics, entry.get("alpha_id") or ""))
    return out


def find_portfolio_match(
    expression: str,
    skeleton: str,
    index: Dict[str, List[Tuple[FrozenSet[str], Tuple[float, ...], str]]],
) -> Optional[str]:
    """Two-factor match: skeleton + fields set + numerics within rel_tol.

    Returns the matching submitted alpha_id when all three factors agree,
    otherwise None. Caller treats hit as "near-certain duplicate, skip
    simulate"; miss means "let BRAIN simulate decide".
    """
    entries = index.get(skeleton)
    if not entries:
        return None
    cand_fields, cand_numerics = _expr_fields_and_numerics(expression)
    # V-27.143: an empty fields set means the parser failed (or the
    # expression genuinely references no fields). Either way, do NOT let two
    # empty-field expressions match each other — `frozenset() == frozenset()`
    # would otherwise skip a real simulate on a parse-failure coincidence.
    if not cand_fields:
        return None
    for fields, numerics, alpha_id in entries:
        if fields and fields == cand_fields and _numerics_match(cand_numerics, numerics):
            return alpha_id or "unknown"
    return None


def is_skeleton_in_portfolio(expression: str, region: str = "USA") -> bool:
    """True if expression's skeleton matches any submitted-portfolio
    skeleton. Caller should treat True as "high self-corr risk, skip simulate".

    NOTE: this is the legacy single-factor check (skeleton-only). For the
    two-factor (fields + numerics) check used by the simulate gate, see
    `find_portfolio_match`.
    """
    if not expression:
        return False
    try:
        from backend.knowledge_extraction import expression_to_skeleton
        sk = expression_to_skeleton(expression, max_depth=3)
    except Exception:
        return False
    return sk in get_portfolio_skeleton_set(region)
