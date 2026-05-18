"""Phase 3 R8 Hierarchical RAG layers (PR1: schema + L0 + L3 + helpers).

Per plan v1.0 §2-§4: replace single-layer ``RAGService.query()`` with a
4-layer fall-through retriever:
  L0: exact pattern_hash match (high specificity, Q9 decayed filter)
  L1: pillar/theme via infer_pillar (PR2)
  L2: family_signature via family_classifier (PR2)
  L3: field-level current expr × dataset/region availability

PR1 scope: L0 + L3 + helpers + RAGResult dataclass. Orchestrator + L1 + L2
in PR2/PR3 (see plan §7).

Additive overlay: legacy ``RAGService.query()`` preserved; this module is
called via ``query_hierarchical()`` entry (added in PR3 dispatch). PR1's
helpers callable standalone for unit tests.

Soft-fail philosophy: any DB/parse error → return empty result (caller's
orchestrator continues to next layer). Mirrors R5/R6/R9 patterns.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import and_, cast, not_, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.knowledge import KnowledgeEntry, compute_pattern_hash


def _expr_hash_64(expression: str) -> str:
    """sha256[:64] hex of an expression — matches the R1a-hook convention
    in ``backend/agents/graph/nodes/evaluation.py:2631`` so that R8-v2 #3
    L2 ranking can JOIN ``r1a_attribution_log.expression_hash``.

    Note: this is *different* from :func:`compute_pattern_hash` (which is
    sha256[:32] of ``pattern|region|dataset``). The R1a log doesn't carry
    region/dataset on its hash, so we mirror its raw-expression form.
    """
    if not expression:
        return ""
    return hashlib.sha256(expression.encode("utf-8")).hexdigest()[:64]


async def fetch_r5_avg_scores(
    db: AsyncSession,
    expressions: List[str],
    *,
    min_samples: int = 1,
    lookback_days: int = 30,
) -> Dict[str, tuple]:
    """R8-v2 #3 helper: pull mean R5 composite_score per expression_hash.

    Returns ``{expression_hash: (avg_score, sample_count)}`` for the input
    expressions that have any non-NULL ``r5_composite_score`` within the
    lookback window and meet ``min_samples``.

    Soft-fail: SQL or import errors → ``{}`` (caller treats as no signal).
    """
    if not expressions:
        return {}
    try:
        hash_map: Dict[str, str] = {}
        for expr in expressions:
            h = _expr_hash_64(expr or "")
            if h:
                hash_map[h] = expr
        if not hash_map:
            return {}
        hashes = list(hash_map.keys())
        # asyncpg expanding bind via SQLAlchemy ``in_`` requires a
        # textual stmt or a select() — go textual to avoid touching
        # the R1aAttributionLog model import cycle.
        stmt = text(
            "SELECT expression_hash, AVG(r5_composite_score) AS avg_score, "
            "COUNT(*) AS n "
            "FROM r1a_attribution_log "
            "WHERE expression_hash = ANY(:hashes) "
            "  AND r5_composite_score IS NOT NULL "
            "  AND created_at > now() - (:days || ' days')::interval "
            "GROUP BY expression_hash"
        )
        rows = (await db.execute(
            stmt, {"hashes": hashes, "days": str(int(lookback_days))}
        )).all()
        out: Dict[str, tuple] = {}
        for r in rows:
            avg = float(r[1]) if r[1] is not None else None
            n = int(r[2]) if r[2] is not None else 0
            if avg is None or n < min_samples:
                continue
            out[r[0]] = (avg, n)
        return out
    except Exception as ex:
        logger.warning(f"[hier_rag L2 r5-rank] fetch failed (return empty): {ex}")
        return {}

logger = logging.getLogger(__name__)


# Filter constant: SUCCESS-side queries MUST exclude decayed entries
# (Q9 reference set, meta_data['decayed']=True). FAILURE-side queries
# INCLUDE them (they're the "avoid this" hints). Centralized so all
# 4 layers stay consistent (per plan [V1.0-S2] Q9 dual-filter lock).
DECAYED_KEY = "decayed"


# Field-extraction regex — mirrors AlphaSemanticValidator._extract_fields
# without instantiating the full validator per call (plan §3 module-level
# extract_fields_for_rag helper). Identifiers that look like fields
# (lowercase + alphanumeric + underscore, not pure numbers, not known op
# names). Caller filters via operator set.
_FIELD_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9_]*)\b")
_KNOWN_OPS: Set[str] = {
    "rank", "ts_mean", "ts_std_dev", "ts_zscore", "ts_rank", "ts_decay_linear",
    "ts_delay", "ts_arg_max", "ts_arg_min", "ts_max", "ts_min", "ts_av_diff",
    "ts_sum", "ts_returns", "ts_change", "ts_skewness", "ts_corr",
    "group_neutralize", "group_rank", "group_zscore", "group_mean", "group_scale",
    "winsorize", "signed_power", "subtract", "multiply", "divide", "add",
    "abs", "log", "sign", "power", "if_else", "trade_when", "where",
    "scale", "rank_by_side", "vector_neut", "regression_neut", "indneutralize",
    # Common false-positives to exclude
    "true", "false", "none", "null", "and", "or", "not",
}


def extract_fields_for_rag(expression: str) -> List[str]:
    """Pull field-like identifiers out of an alpha expression.

    Module-level helper for hierarchical RAG layers (avoid instantiating
    full AlphaSemanticValidator per call). Returns deduped lowercase
    field names sorted alphabetically.
    """
    if not expression or not isinstance(expression, str):
        return []
    tokens = _FIELD_TOKEN_RE.findall(expression.lower())
    seen: Set[str] = set()
    out: List[str] = []
    for t in tokens:
        if t in _KNOWN_OPS:
            continue
        if t.isdigit():
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return sorted(out)


# ---------------------------------------------------------------------------
# RAGResult dataclass — orchestrator output
# ---------------------------------------------------------------------------

@dataclass
class RAGEntry:
    """Single retrieved KB entry with provenance for orchestrator dedup +
    LLM-prompt bullet formatting."""
    pattern_hash: str
    pattern: str
    entry_type: str   # SUCCESS_PATTERN | FAILURE_PITFALL
    description: str = ""
    meta_data: Dict[str, Any] = field(default_factory=dict)
    source_layer: str = ""  # "L0_exact" | "L1_pillar" | "L2_family" | "L3_field"
    relevance_score: float = 0.5


@dataclass
class RAGResult:
    """Output of hierarchical query — patterns + pitfalls + telemetry."""
    patterns: List[RAGEntry] = field(default_factory=list)
    pitfalls: List[RAGEntry] = field(default_factory=list)
    layer_hits: Dict[str, int] = field(default_factory=lambda: {"L0": 0, "L1": 0, "L2": 0, "L3": 0})
    total_queries: int = 0   # how many SQL queries executed across layers

    def total_bullets(self) -> int:
        return len(self.patterns) + len(self.pitfalls)


# ---------------------------------------------------------------------------
# Layer 0: exact pattern_hash match
# ---------------------------------------------------------------------------

async def layer0_exact_match(
    db: AsyncSession,
    *,
    current_expression: Optional[str],
    region: Optional[str] = None,
    dataset_id: Optional[str] = None,
    budget: int = 5,
) -> tuple[List[RAGEntry], List[RAGEntry]]:
    """RAG#0 (highest specificity): look up exact pattern_hash matches.

    Returns (success_patterns, failure_pitfalls) lists.
    - success_patterns: entry_type=SUCCESS_PATTERN AND NOT decayed
    - failure_pitfalls: entry_type=FAILURE_PITFALL (decayed included
      — they're the explicit "avoid" set)

    Soft-fail: SQL error → ([], [])。

    Per plan §2.5 dual-filter semantics + [V1.0-S2] Q9 lock。
    """
    if not current_expression:
        return [], []
    try:
        phash = compute_pattern_hash(current_expression, region, dataset_id)
        # Same pattern may have multiple entries (different regions) — take all
        rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.pattern_hash == phash)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .limit(budget * 2)  # split between success + failure
        )).scalars().all()
        succ: List[RAGEntry] = []
        fail: List[RAGEntry] = []
        for r in rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            is_decayed = str(md.get(DECAYED_KEY, "")).lower() == "true"
            entry = RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L0_exact",
                relevance_score=1.0,  # exact match → highest
            )
            if r.entry_type == "SUCCESS_PATTERN":
                if not is_decayed:
                    succ.append(entry)
            elif r.entry_type == "FAILURE_PITFALL":
                # Include decayed (they're the avoid list) + non-decayed
                fail.append(entry)
        return succ[:budget], fail[:budget]
    except Exception as ex:
        logger.warning(f"[hier_rag L0] exact_match failed (return empty): {ex}")
        return [], []


# ---------------------------------------------------------------------------
# Layer 3: field-level (current expr fields → KB entries containing them)
# ---------------------------------------------------------------------------

async def layer3_field_level(
    db: AsyncSession,
    *,
    current_expression: Optional[str],
    region: Optional[str] = None,
    universe: Optional[str] = None,
    budget: int = 5,
) -> tuple[List[RAGEntry], List[RAGEntry]]:
    """RAG#3 (lowest specificity fallback): pull KB entries whose pattern
    contains at least one field from current_expression.

    SUCCESS: pattern contains field tokens AND NOT decayed AND region match
    (or "ANY" / NULL meta_data['regions'] interpreted as ANY per
    [V1.0-A1-3] fallback)

    FAILURE: pattern contains field tokens (decayed allowed)

    Soft-fail: parse / SQL error → ([], [])。

    NOTE: this uses simple ILIKE on each field in the pattern text. A more
    performant variant would precompute meta_data['fields_used'] via the
    backfill script (PR1 deliverable) and JOIN — left as PR2/PR3 enhancement.
    """
    if not current_expression:
        return [], []
    fields = extract_fields_for_rag(current_expression)
    if not fields:
        return [], []
    try:
        # Build ILIKE OR clause for field tokens — bounded to top 3 fields
        # to keep query cost predictable (one alpha typically uses 1-3 fields)
        from sqlalchemy import column
        like_clauses = [KnowledgeEntry.pattern.ilike(f"%{f}%") for f in fields[:3]]

        rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(or_(*like_clauses))
            .limit(budget * 4)  # over-fetch then filter by region/decayed in Python
        )).scalars().all()

        succ: List[RAGEntry] = []
        fail: List[RAGEntry] = []
        for r in rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            is_decayed = str(md.get(DECAYED_KEY, "")).lower() == "true"
            # Region filter: meta_data['regions'] is List[str] (per
            # knowledge_seed.py convention) OR meta_data['region'] str.
            # Missing → treat as ANY (per plan [V1.0-A1-3])
            kb_regions = md.get("regions") or ([md["region"]] if md.get("region") else None)
            region_ok = True
            if kb_regions and region:
                region_ok = (region.upper() in [str(x).upper() for x in kb_regions]
                             or "ANY" in [str(x).upper() for x in kb_regions])
            entry = RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L3_field",
                relevance_score=0.5,  # lowest layer
            )
            if r.entry_type == "SUCCESS_PATTERN":
                if not is_decayed and region_ok:
                    succ.append(entry)
                    if len(succ) >= budget:
                        break  # short-circuit when full
            elif r.entry_type == "FAILURE_PITFALL":
                # FAILURE: ignore region constraint (failures are universal),
                # include decayed
                fail.append(entry)
        return succ[:budget], fail[:budget]
    except Exception as ex:
        logger.warning(f"[hier_rag L3] field_level failed (return empty): {ex}")
        return [], []


__all__ = [
    "DECAYED_KEY",
    "extract_fields_for_rag",
    "fetch_r5_avg_scores",
    "RAGEntry",
    "RAGResult",
    "layer0_exact_match",
    "layer1_pillar",
    "layer2_family",
    "layer3_field_level",
    "query_hierarchical",
]


# ---------------------------------------------------------------------------
# Layer 1: pillar/theme (uses backfilled meta_data['pillar_classified'])
# ---------------------------------------------------------------------------

async def layer1_pillar(
    db: AsyncSession,
    *,
    current_expression: Optional[str] = None,
    hypothesis_pillar: Optional[str] = None,
    region: Optional[str] = None,
    budget: int = 5,
) -> tuple[List[RAGEntry], List[RAGEntry]]:
    """RAG#1: pillar/theme JOIN via backfilled meta_data['pillar_classified'].

    Pillar resolution priority (per plan §2.3):
      1. Explicit hypothesis_pillar arg (from LLM hypothesis dict)
      2. infer_pillar(current_expression) fallback

    Returns (success_patterns, failure_pitfalls). Q9 decayed dual-filter
    (SUCCESS excludes, FAILURE includes per plan §2.5). Pillar="other"
    short-circuits to empty (per [V1.0-A2-1]:"other" too broad for L1).
    """
    # Resolve pillar
    pillar = None
    if hypothesis_pillar and isinstance(hypothesis_pillar, str):
        pillar = hypothesis_pillar.strip().lower()
    elif current_expression:
        try:
            from backend.pillar_classifier import infer_pillar
            pillar = infer_pillar(expression=current_expression)
        except Exception as ex:
            logger.debug(f"[hier_rag L1] infer_pillar failed: {ex}")
            return [], []

    if not pillar or pillar == "other":
        # "other" pillar too broad — short-circuit per [V1.0-A2-1]
        return [], []

    try:
        # JOIN on backfilled meta_data->>'pillar_classified' = pillar
        # GIN(jsonb_path_ops) supports @> containment — use that.
        rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.meta_data.op("@>")({"pillar_classified": pillar}))
            .limit(budget * 4)  # over-fetch then filter by decayed/region in Python
        )).scalars().all()

        succ: List[RAGEntry] = []
        fail: List[RAGEntry] = []
        for r in rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            is_decayed = str(md.get(DECAYED_KEY, "")).lower() == "true"
            entry = RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L1_pillar",
                relevance_score=0.75,  # mid-high specificity
            )
            if r.entry_type == "SUCCESS_PATTERN":
                if not is_decayed:
                    succ.append(entry)
                    if len(succ) >= budget:
                        break
            elif r.entry_type == "FAILURE_PITFALL":
                fail.append(entry)
        return succ[:budget], fail[:budget]
    except Exception as ex:
        logger.warning(f"[hier_rag L1] pillar query failed (return empty): {ex}")
        return [], []


# ---------------------------------------------------------------------------
# Layer 2: family_signature (uses backfilled meta_data['family_signature'])
# ---------------------------------------------------------------------------

async def layer2_family(
    db: AsyncSession,
    *,
    current_expression: Optional[str],
    region: Optional[str] = None,
    budget: int = 5,
    enable_r5_ranking: bool = False,
    r5_min_samples: int = 1,
    r5_lookback_days: int = 30,
) -> tuple[List[RAGEntry], List[RAGEntry]]:
    """RAG#2: family JOIN via backfilled meta_data['family_signature'].

    family_signature = sha256[:16] of operator-sequence (per R10
    family_classifier). Two expressions sharing same op pipeline
    (regardless of fields/windows) → same family. R8 L2 JOIN finds
    KB entries with same family_signature → returns SUCCESS_PATTERN
    examples + FAILURE_PITFALL warnings.

    Per [V1.0-S5]: exclude family_capped entries (meta_data['family_capped']
    truthy or coming from r1a R10 marker). Otherwise R10 family-cap purpose
    undermined.

    R8-v2 #3 (2026-05-18): when ``enable_r5_ranking`` is True, over-fetch
    SUCCESS candidates, JOIN ``r1a_attribution_log.r5_composite_score`` AVG
    by ``expression_hash`` (sha256[:64] of pattern) and re-rank SUCCESS by
    historical R5 mean. Zero-sample rows keep default 0.65 relevance.
    Soft-fail: if the R5 JOIN errors out, original order preserved.

    Soft-fail: SQL error → ([], []).
    """
    if not current_expression:
        return [], []
    try:
        from backend.family_classifier import family_signature
        sig = family_signature(current_expression)
    except Exception:
        return [], []

    if not sig or sig == "<empty>":
        return [], []

    try:
        # When R5 ranking is ON we want all candidates first so the re-rank
        # has a chance to surface high-R5 rows beyond the first `budget`
        # raw matches; otherwise keep the original short-circuit behaviour.
        fetch_limit = budget * (8 if enable_r5_ranking else 4)
        rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.meta_data.op("@>")({"family_signature": sig}))
            .limit(fetch_limit)
        )).scalars().all()

        succ: List[RAGEntry] = []
        fail: List[RAGEntry] = []
        for r in rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            is_decayed = str(md.get(DECAYED_KEY, "")).lower() == "true"
            # [V1.0-S5] exclude family_capped — undermines R10 purpose
            is_capped = (
                str(md.get("family_capped", "")).lower() == "true"
                or str(md.get("_r10_family_cap_dropped", "")).lower() == "true"
            )
            if is_capped:
                continue
            entry = RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L2_family",
                relevance_score=0.65,  # mid specificity
            )
            if r.entry_type == "SUCCESS_PATTERN":
                if not is_decayed:
                    succ.append(entry)
                    # When NOT R5-ranking, short-circuit at budget; with
                    # ranking we over-fetch then trim after re-sort.
                    if not enable_r5_ranking and len(succ) >= budget:
                        break
            elif r.entry_type == "FAILURE_PITFALL":
                fail.append(entry)

        # R8-v2 #3 R5 re-ranking (only when enabled and we have multiple
        # SUCCESS candidates worth sorting).
        if enable_r5_ranking and len(succ) > 1:
            score_map = await fetch_r5_avg_scores(
                db, [e.pattern for e in succ],
                min_samples=r5_min_samples,
                lookback_days=r5_lookback_days,
            )
            if score_map:
                for e in succ:
                    h = _expr_hash_64(e.pattern)
                    rec = score_map.get(h)
                    if rec:
                        avg, n = rec
                        # Map avg ∈ [0,1] → relevance ∈ [0.45, 0.85].
                        # Zero-sample rows keep default 0.65 (neutral).
                        e.relevance_score = 0.45 + 0.4 * max(0.0, min(1.0, avg))
                        e.meta_data = {
                            **e.meta_data,
                            "_r5_composite_avg": round(avg, 4),
                            "_r5_sample_count": n,
                        }
                succ.sort(key=lambda x: x.relevance_score, reverse=True)

        return succ[:budget], fail[:budget]
    except Exception as ex:
        logger.warning(f"[hier_rag L2] family query failed (return empty): {ex}")
        return [], []


# ---------------------------------------------------------------------------
# Orchestrator — fall-through L0 → L1 → L2 → L3 (PR3, plan §4)
# ---------------------------------------------------------------------------

async def query_hierarchical(
    db: AsyncSession,
    *,
    current_expression: Optional[str] = None,
    hypothesis_pillar: Optional[str] = None,
    region: Optional[str] = None,
    universe: Optional[str] = None,
    dataset_id: Optional[str] = None,
    max_patterns: int = 20,
    max_pitfalls: int = 10,
    layer_budgets: Optional[Dict[str, int]] = None,
) -> RAGResult:
    """Phase 3 R8 PR3 orchestrator — sequential fall-through L0 → L1 → L2 → L3.

    Per plan v1.0 §4.1 decision lock: sequential (NOT parallel union)
    because:
      1. Cost asymmetry — L0 is O(log N), L1/L2/L3 are JSONB scans;
         parallel-then-discard wastes work
      2. Determinism — LLM sees L0 first → highest specificity → trust
      3. Cache-friendly — L0 filling budget skips L1/L2/L3 entirely

    Algorithm:
      remaining_pat = max_patterns; remaining_fail = max_pitfalls
      seen_hashes: dedupe by pattern_hash across layers
      For each layer in [L0, L1, L2, L3]:
        if remaining_pat <= 0 AND remaining_fail <= 0: break
        succ, fail = await layer(...)
        for e in succ:
          if e.pattern_hash not in seen AND remaining_pat > 0:
            append + decrement + record layer hit
        (same for fail)

    Returns RAGResult with patterns + pitfalls + layer_hits telemetry.
    Soft-fail: any layer error → empty from that layer, orchestrator
    continues. Mirrors the existing layer-level soft-fail philosophy.
    """
    result = RAGResult()
    seen_hashes: Set[str] = set()
    remaining_pat = max_patterns
    remaining_fail = max_pitfalls

    layer_budgets = layer_budgets or {"L0": 5, "L1": 5, "L2": 5, "L3": 5}

    def _consume(succ: List[RAGEntry], fail: List[RAGEntry], layer_key: str) -> None:
        nonlocal remaining_pat, remaining_fail
        for e in succ:
            if remaining_pat <= 0:
                break
            if e.pattern_hash and e.pattern_hash in seen_hashes:
                continue
            seen_hashes.add(e.pattern_hash)
            result.patterns.append(e)
            remaining_pat -= 1
            result.layer_hits[layer_key] = result.layer_hits.get(layer_key, 0) + 1
        for e in fail:
            if remaining_fail <= 0:
                break
            if e.pattern_hash and e.pattern_hash in seen_hashes:
                continue
            seen_hashes.add(e.pattern_hash)
            result.pitfalls.append(e)
            remaining_fail -= 1
            # Don't double-count layer hit (only count patterns for hit-rate
            # telemetry per plan §10 GO gate)

    # L0 — exact pattern_hash match
    if current_expression and (remaining_pat > 0 or remaining_fail > 0):
        s, f = await layer0_exact_match(
            db, current_expression=current_expression, region=region,
            dataset_id=dataset_id, budget=layer_budgets.get("L0", 5),
        )
        result.total_queries += 1
        _consume(s, f, "L0")

    # L1 — pillar/theme
    if (current_expression or hypothesis_pillar) and (remaining_pat > 0 or remaining_fail > 0):
        s, f = await layer1_pillar(
            db, current_expression=current_expression,
            hypothesis_pillar=hypothesis_pillar, region=region,
            budget=layer_budgets.get("L1", 5),
        )
        result.total_queries += 1
        _consume(s, f, "L1")

    # L2 — family_signature (R8-v2 #3 R5 ranking forwarded from settings)
    if current_expression and (remaining_pat > 0 or remaining_fail > 0):
        _r5_rank_on = False
        _r5_min_samples = 1
        _r5_lookback_days = 30
        try:
            from backend.config import settings as _stg
            _r5_rank_on = bool(getattr(_stg, "ENABLE_R5_L2_RANKING", False))
            _r5_min_samples = int(getattr(_stg, "R5_L2_RANKING_MIN_SAMPLES", 1))
            _r5_lookback_days = int(getattr(_stg, "R5_L2_RANKING_LOOKBACK_DAYS", 30))
        except Exception:
            pass
        s, f = await layer2_family(
            db, current_expression=current_expression, region=region,
            budget=layer_budgets.get("L2", 5),
            enable_r5_ranking=_r5_rank_on,
            r5_min_samples=_r5_min_samples,
            r5_lookback_days=_r5_lookback_days,
        )
        result.total_queries += 1
        _consume(s, f, "L2")

    # L3 — field-level
    if current_expression and (remaining_pat > 0 or remaining_fail > 0):
        s, f = await layer3_field_level(
            db, current_expression=current_expression,
            region=region, universe=universe,
            budget=layer_budgets.get("L3", 5),
        )
        result.total_queries += 1
        _consume(s, f, "L3")

    logger.info(
        f"[hier_rag] query complete | patterns={len(result.patterns)} "
        f"pitfalls={len(result.pitfalls)} layer_hits={result.layer_hits} "
        f"sql_queries={result.total_queries}"
    )
    return result
