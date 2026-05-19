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


# ---------------------------------------------------------------------------
# R8-v2 #2 (2026-05-18): per-layer Redis cache helpers
# ---------------------------------------------------------------------------
#
# Design: loop-aware Redis singleton (same pattern as BrainAdapter
# ``_get_slot_redis``). cache_key = sha256[:16] of ``layer|json(params)``;
# TTL = settings.RAG_HIER_CACHE_TTL_SEC. Stored as JSON ``{succ:[...],
# fail:[...]}`` lists of dicts mirroring RAGEntry fields. Cache miss /
# Redis unreachable → caller bypasses (soft-fail).
#
# No explicit invalidation: KB write rate is 3-50/h vs 5-min TTL → bounded
# 5-min stale window is acceptable per plan §10 GO gate. Avoids SCAN /
# generation-bump complexity.

_rag_redis_client: Optional[Any] = None
_rag_redis_loop_id: Optional[int] = None


async def _get_rag_redis():
    """Return a loop-bound redis.asyncio client; rebuild if loop changed.

    Mirrors :meth:`backend.adapters.brain_adapter.BrainAdapter._get_slot_redis`
    — Celery's per-task loop lifecycle would otherwise leave a stale client
    bound to a closed loop.

    Returns ``None`` on import failure (redis not installed) or connection
    error so callers can soft-fall.
    """
    global _rag_redis_client, _rag_redis_loop_id
    try:
        import asyncio
        import redis.asyncio as _redis
        from backend.config import settings as _stg
    except Exception:
        return None
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    current_loop_id = id(current_loop) if current_loop is not None else None
    loop_changed = (
        _rag_redis_client is not None
        and _rag_redis_loop_id is not None
        and current_loop_id is not None
        and current_loop_id != _rag_redis_loop_id
    )
    loop_dead = (
        _rag_redis_client is not None
        and current_loop is not None
        and current_loop.is_closed()
    )
    if loop_changed or loop_dead:
        _rag_redis_client = None
        _rag_redis_loop_id = None
    if _rag_redis_client is None:
        try:
            _rag_redis_client = _redis.from_url(
                _stg.REDIS_URL, decode_responses=True
            )
            _rag_redis_loop_id = current_loop_id
        except Exception as ex:
            logger.debug(f"[hier_rag cache] redis init failed: {ex}")
            return None
    return _rag_redis_client


def _make_layer_cache_key(layer: str, params: Dict[str, Any]) -> str:
    """Stable sha256[:16] over ``layer|json(sorted(params))``."""
    import json
    blob = layer + "|" + json.dumps(params, sort_keys=True, default=str)
    return "ragcache:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _entry_to_dict(e: "RAGEntry") -> Dict[str, Any]:
    return {
        "pattern_hash": e.pattern_hash,
        "pattern": e.pattern,
        "entry_type": e.entry_type,
        "description": e.description,
        "meta_data": e.meta_data,
        "source_layer": e.source_layer,
        "relevance_score": e.relevance_score,
    }


def _entry_from_dict(d: Dict[str, Any]) -> "RAGEntry":
    return RAGEntry(
        pattern_hash=d.get("pattern_hash", "") or "",
        pattern=d.get("pattern", "") or "",
        entry_type=d.get("entry_type", "") or "",
        description=d.get("description", "") or "",
        meta_data=d.get("meta_data") or {},
        source_layer=d.get("source_layer", "") or "",
        relevance_score=float(d.get("relevance_score", 0.5)),
    )


async def _cache_get(key: str) -> Optional[tuple]:
    """Read cached (succ, fail) lists; return None on miss / error."""
    r = await _get_rag_redis()
    if r is None:
        return None
    try:
        raw = await r.get(key)
        if not raw:
            return None
        import json
        payload = json.loads(raw)
        succ = [_entry_from_dict(d) for d in payload.get("succ", [])]
        fail = [_entry_from_dict(d) for d in payload.get("fail", [])]
        return succ, fail
    except Exception as ex:
        logger.debug(f"[hier_rag cache] get failed: {ex}")
        return None


async def _cache_set(key: str, succ: List["RAGEntry"], fail: List["RAGEntry"], ttl: int) -> None:
    """Write cached (succ, fail) lists with TTL; swallow errors."""
    r = await _get_rag_redis()
    if r is None:
        return
    try:
        import json
        payload = json.dumps({
            "succ": [_entry_to_dict(e) for e in succ],
            "fail": [_entry_to_dict(e) for e in fail],
        })
        await r.setex(key, max(1, int(ttl)), payload)
    except Exception as ex:
        logger.debug(f"[hier_rag cache] set failed: {ex}")


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
#
# TODO(M11): sync with AlphaSemanticValidator op registry. That registry
# loads BRAIN operators from DB at startup (see
# backend/alpha_semantic_validator.py OperatorRegistry), which is async +
# requires a session, so we can't reuse it at module import. The
# module-import sanity check below (_warn_on_op_drift) fires once after
# the registry has been populated by a caller and warns if any op the
# validator knows about is missing from _KNOWN_OPS — keeps drift visible
# in logs without raising cold-start cost.
_FIELD_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9_]*)\b")
_KNOWN_OPS: Set[str] = {
    # ---- Real BRAIN operators (alphabetical for readability) ----
    "abs", "add", "arg_max", "arg_min", "bucket", "correlation", "covariance",
    "decay_exponential", "delta", "densify", "divide", "group_mean",
    "group_neutralize", "group_rank", "group_scale", "group_zscore", "hump",
    "if_else", "indneutralize", "last_diff_value", "log", "multiply", "power",
    "quantile", "rank", "rank_by_side", "regression_neut", "scale",
    "sign", "signed_power", "subtract", "trade_when", "ts_arg_max",
    "ts_arg_min", "ts_av_diff", "ts_change", "ts_corr", "ts_decay_linear",
    "ts_delay", "ts_max", "ts_mean", "ts_min", "ts_rank", "ts_returns",
    "ts_skewness", "ts_std_dev", "ts_sum", "ts_zscore", "vector_neut",
    "where", "winsorize",
    # ---- Common false-positives to exclude ----
    "and", "false", "none", "not", "null", "or", "true",
}


_OP_DRIFT_WARNED = False


def _warn_on_op_drift() -> None:
    """One-time sanity check: compare ``_KNOWN_OPS`` to the operators the
    semantic validator has loaded from DB. Warns if validator knows ops
    we'd treat as fields (which would leak BRAIN op names into L3 ILIKE
    searches → spurious matches).

    Cheap: only inspects the already-populated registry — does NOT trigger
    a DB load. Silent no-op when the registry hasn't been loaded yet
    (e.g. cold start in unit tests). Idempotent via ``_OP_DRIFT_WARNED``.
    """
    global _OP_DRIFT_WARNED
    if _OP_DRIFT_WARNED:
        return
    try:
        from backend.alpha_semantic_validator import _operator_registry
        # Access _operators directly to avoid triggering the
        # "no operators loaded" warning emitted by the .operators property.
        registry_ops = set(_operator_registry._operators or set())
        if not registry_ops:
            return  # registry not loaded yet — try again next call
        missing = registry_ops - _KNOWN_OPS
        if missing:
            logger.warning(
                f"[hier_rag M11 drift] _KNOWN_OPS missing "
                f"{len(missing)} validator op(s): {sorted(missing)[:20]}"
            )
        _OP_DRIFT_WARNED = True
    except Exception as ex:
        logger.debug(f"[hier_rag M11 drift] sanity check skipped: {ex}")
        _OP_DRIFT_WARNED = True  # don't retry on import errors


def extract_fields_for_rag(expression: str) -> List[str]:
    """Pull field-like identifiers out of an alpha expression.

    Module-level helper for hierarchical RAG layers (avoid instantiating
    full AlphaSemanticValidator per call). Returns deduped lowercase
    field names sorted alphabetically.
    """
    if not expression or not isinstance(expression, str):
        return []
    # M11: one-time drift check against AlphaSemanticValidator registry.
    # Cheap (no DB I/O) — see _warn_on_op_drift docstring.
    _warn_on_op_drift()
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
        # N2 SQL-pushdown: split SUCCESS / FAILURE into two queries so the
        # decayed filter can be pushed into WHERE on the SUCCESS path only
        # (FAILURE explicitly INCLUDES decayed per Q9 dual-filter).
        succ_rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.pattern_hash == phash)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            .where(not_(
                KnowledgeEntry.meta_data.op("@>")(
                    cast({DECAYED_KEY: "true"}, JSONB)
                )
            ))
            .order_by(KnowledgeEntry.id.desc())  # newest first — surfaces fresh evidence
            .limit(budget)
        )).scalars().all()
        fail_rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.pattern_hash == phash)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type == "FAILURE_PITFALL")
            .order_by(KnowledgeEntry.id.desc())  # newest first
            .limit(budget)
        )).scalars().all()

        def _to_entry(r) -> RAGEntry:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            return RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L0_exact",
                relevance_score=1.0,  # exact match → highest
            )

        succ = [_to_entry(r) for r in succ_rows]
        fail = [_to_entry(r) for r in fail_rows]
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
        # to keep query cost predictable (one alpha typically uses 1-3 fields).
        # Note: ILIKE on `pattern` text column — no JSONB index helps here,
        # but we still push decayed filter (JSONB) + entry_type into WHERE.
        like_clauses = [KnowledgeEntry.pattern.ilike(f"%{f}%") for f in fields[:3]]

        # N2 SQL-pushdown: split SUCCESS/FAILURE into two queries.
        # SUCCESS path: push decayed filter into WHERE (leverages GIN index).
        # Region filter stays in Python due to list/str ambiguity in meta_data.
        succ_rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            .where(or_(*like_clauses))
            .where(not_(
                KnowledgeEntry.meta_data.op("@>")(
                    cast({DECAYED_KEY: "true"}, JSONB)
                )
            ))
            .order_by(KnowledgeEntry.id.desc())  # newest first — surfaces fresh evidence
            .limit(budget * 2)  # small over-fetch for region filter in Python
        )).scalars().all()
        fail_rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type == "FAILURE_PITFALL")
            .where(or_(*like_clauses))
            .order_by(KnowledgeEntry.id.desc())  # newest first
            .limit(budget)
        )).scalars().all()

        succ: List[RAGEntry] = []
        fail: List[RAGEntry] = []
        for r in succ_rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            # Region filter: meta_data['regions'] is List[str] (per
            # knowledge_seed.py convention) OR meta_data['region'] str.
            # Missing → treat as ANY (per plan [V1.0-A1-3]).
            # Kept in Python due to list/str ambiguity.
            kb_regions = md.get("regions") or ([md["region"]] if md.get("region") else None)
            region_ok = True
            if kb_regions and region:
                region_ok = (region.upper() in [str(x).upper() for x in kb_regions]
                             or "ANY" in [str(x).upper() for x in kb_regions])
            if not region_ok:
                continue
            succ.append(RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L3_field",
                relevance_score=0.5,  # lowest layer
            ))
            if len(succ) >= budget:
                break
        for r in fail_rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            # FAILURE: ignore region constraint (failures are universal),
            # include decayed.
            fail.append(RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L3_field",
                relevance_score=0.5,
            ))
        return succ[:budget], fail[:budget]
    except Exception as ex:
        logger.warning(f"[hier_rag L3] field_level failed (return empty): {ex}")
        return [], []


__all__ = [
    "DECAYED_KEY",
    "extract_fields_for_rag",
    "fetch_r5_avg_scores",
    "_make_layer_cache_key",
    "_cache_get",
    "_cache_set",
    "_get_rag_redis",
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
        # N2 SQL-pushdown: split SUCCESS/FAILURE queries so decayed filter
        # is in WHERE on the SUCCESS path only (FAILURE INCLUDES decayed
        # per Q9 dual-filter — they're the "avoid this" hints).
        pillar_match = cast({"pillar_classified": pillar}, JSONB)
        decayed_match = cast({DECAYED_KEY: "true"}, JSONB)

        # M10 fix: over-fetch SUCCESS so post-SQL region filter still has
        # room to surface `budget` matching rows (mirrors L3 pattern).
        # Region filter stays in Python due to list/str ambiguity in meta_data
        # (knowledge_seed convention uses meta_data['regions']: List[str]
        # OR meta_data['region']: str; missing → treat as ANY per [V1.0-A1-3]).
        succ_rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            .where(KnowledgeEntry.meta_data.op("@>")(pillar_match))
            .where(not_(KnowledgeEntry.meta_data.op("@>")(decayed_match)))
            .order_by(KnowledgeEntry.id.desc())  # newest first — surfaces fresh evidence
            .limit(budget * 2 if region else budget)
        )).scalars().all()
        fail_rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type == "FAILURE_PITFALL")
            .where(KnowledgeEntry.meta_data.op("@>")(pillar_match))
            .order_by(KnowledgeEntry.id.desc())  # newest first
            .limit(budget)
        )).scalars().all()

        def _region_ok(md: Dict[str, Any]) -> bool:
            """M10: region availability check — same logic L3 uses.

            meta_data['regions'] is List[str] OR meta_data['region'] str.
            Missing → treat as ANY (per plan [V1.0-A1-3]).
            """
            if not region:
                return True
            kb_regions = md.get("regions") or (
                [md["region"]] if md.get("region") else None
            )
            if not kb_regions:
                return True
            normalized = [str(x).upper() for x in kb_regions]
            return region.upper() in normalized or "ANY" in normalized

        succ: List[RAGEntry] = []
        for r in succ_rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            if not _region_ok(md):
                continue
            succ.append(RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L1_pillar",
                relevance_score=0.75,  # mid-high specificity
            ))
            if len(succ) >= budget:
                break
        # FAILURE: per L3 convention, ignore region constraint (failures are
        # universal "avoid this" hints).
        fail: List[RAGEntry] = []
        for r in fail_rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            fail.append(RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L1_pillar",
                relevance_score=0.75,
            ))
        return succ[:budget], fail[:budget]
    except Exception as ex:
        logger.warning(f"[hier_rag L1] pillar query failed (return empty): {ex}")
        return [], []


# ---------------------------------------------------------------------------
# Layer 2: family_signature (uses backfilled meta_data['family_signature'])
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# R1b.3b (2026-05-18): R8 L2 elevation for failure_tree-bearing FAILURE_PITFALL
# ---------------------------------------------------------------------------
# Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §7.3
# [V1.0-A2-5]: R1b.3 does NOT modify R8 SQL or function signatures. R1b.3a's
# record_failure_tree writes JSONB; this helper reads it back from
# layer2_family's already-fetched fail rows and elevates relevance for
# entries whose root_statement is semantically close to the current
# hypothesis (Jaccard token similarity).


_R1B_FAILURE_TREE_TOKEN_RE = re.compile(r"\b([a-z][a-z0-9_]*)\b")
_R1B_FAILURE_TREE_STOPWORDS: Set[str] = {
    "the", "a", "an", "is", "in", "on", "of", "for", "and", "or", "not",
    "with", "by", "to", "from", "as", "at", "this", "that", "these", "those",
    "be", "are", "was", "were", "has", "have", "had", "do", "does", "did",
    "rank", "ts_rank", "close", "open", "high", "low", "volume", "vwap",
}


def _r1b_tokens(text: str) -> Set[str]:
    """Lowercased word tokens minus common stopwords + OHLCV field names."""
    if not text:
        return set()
    toks = _R1B_FAILURE_TREE_TOKEN_RE.findall(text.lower())
    return {t for t in toks if t not in _R1B_FAILURE_TREE_STOPWORDS and len(t) > 2}


def _r1b_jaccard_distance(a: Set[str], b: Set[str]) -> float:
    """Jaccard distance = 1 - |A∩B| / |A∪B|. Empty sets → distance 1.0."""
    if not a or not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return 1.0 - len(a & b) / len(union)


def _elevate_failure_tree_pitfalls(
    fail: List["RAGEntry"], current_hypothesis: str, *,
    jaccard_max: float = 0.4, bonus: float = 0.20,
) -> List["RAGEntry"]:
    """Bump relevance_score of FAILURE_PITFALL entries whose
    ``meta_data['failure_tree']['statement']`` is semantically close to
    ``current_hypothesis`` (Jaccard distance ≤ ``jaccard_max``).

    No-op when flag OFF, current_hypothesis empty, or no entry carries a
    failure_tree. Re-sorts ``fail`` by relevance_score DESC so the caller
    can budget-cap.
    """
    if not current_hypothesis:
        return fail
    h_tokens = _r1b_tokens(current_hypothesis)
    if not h_tokens:
        return fail
    bumped = False
    for e in fail:
        md = e.meta_data or {}
        tree = md.get("failure_tree") if isinstance(md, dict) else None
        if not tree or not isinstance(tree, dict):
            continue
        root_statement = str(tree.get("statement", "") or "")
        if not root_statement:
            continue
        dist = _r1b_jaccard_distance(h_tokens, _r1b_tokens(root_statement))
        if dist > jaccard_max:
            continue
        e.relevance_score = min(1.0, (e.relevance_score or 0.5) + bonus)
        e.meta_data = {
            **md,
            "_r1b_failure_tree_match_jaccard": round(dist, 4),
            "_r1b_failure_tree_bonus_applied": bonus,
        }
        bumped = True
    if bumped:
        fail.sort(key=lambda x: x.relevance_score, reverse=True)
    return fail


async def layer2_family(
    db: AsyncSession,
    *,
    current_expression: Optional[str],
    region: Optional[str] = None,
    budget: int = 5,
    enable_r5_ranking: bool = False,
    r5_min_samples: int = 1,
    r5_lookback_days: int = 30,
    current_hypothesis: Optional[str] = None,
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
        # N2 SQL-pushdown: split SUCCESS/FAILURE and push family_signature,
        # decayed (SUCCESS path), and family_capped exclusions into WHERE.
        # GIN(jsonb_path_ops) on meta_data backs the @> containment fast.
        # When R5 ranking is ON we over-fetch SUCCESS so the re-rank has a
        # chance to surface high-R5 rows beyond the first `budget`.
        family_match = cast({"family_signature": sig}, JSONB)
        decayed_match = cast({DECAYED_KEY: "true"}, JSONB)
        capped_match = cast({"family_capped": "true"}, JSONB)
        r10_capped_match = cast({"_r10_family_cap_dropped": "true"}, JSONB)

        succ_limit = budget * 2 if enable_r5_ranking else budget
        succ_rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            .where(KnowledgeEntry.meta_data.op("@>")(family_match))
            .where(not_(KnowledgeEntry.meta_data.op("@>")(decayed_match)))
            .where(not_(KnowledgeEntry.meta_data.op("@>")(capped_match)))
            .where(not_(KnowledgeEntry.meta_data.op("@>")(r10_capped_match)))
            .order_by(KnowledgeEntry.id.desc())  # newest first — surfaces fresh evidence
            .limit(succ_limit)
        )).scalars().all()
        fail_rows = (await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
            .where(KnowledgeEntry.entry_type == "FAILURE_PITFALL")
            .where(KnowledgeEntry.meta_data.op("@>")(family_match))
            .where(not_(KnowledgeEntry.meta_data.op("@>")(capped_match)))
            .where(not_(KnowledgeEntry.meta_data.op("@>")(r10_capped_match)))
            .order_by(KnowledgeEntry.id.desc())  # newest first
            .limit(budget)
        )).scalars().all()

        def _to_entry(r) -> RAGEntry:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            return RAGEntry(
                pattern_hash=r.pattern_hash or "",
                pattern=r.pattern or "",
                entry_type=r.entry_type or "",
                description=r.description or "",
                meta_data=md,
                source_layer="L2_family",
                relevance_score=0.65,  # mid specificity
            )

        succ = [_to_entry(r) for r in succ_rows]
        fail = [_to_entry(r) for r in fail_rows]

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

        # R1b.3b (2026-05-18): elevate FAILURE_PITFALL entries that carry a
        # failure_tree matching the current hypothesis. Flag-gated;
        # default OFF so legacy fail ordering preserved.
        try:
            from backend.config import settings as _stg
            if getattr(_stg, "ENABLE_R1B_FAILURE_TREE", False) and current_hypothesis:
                fail = _elevate_failure_tree_pitfalls(fail, current_hypothesis)
        except Exception as _ft_ex:
            logger.debug(
                f"[hier_rag L2] failure_tree elevation failed (skip): {_ft_ex}"
            )

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
    current_hypothesis: Optional[str] = None,
    task_id: Optional[int] = None,
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

    # R8-v2 #2: per-layer Redis cache always on (retired ENABLE_HIERARCHICAL_RAG_CACHE
    # 2026-05-19 — subsumed into the main ENABLE_HIERARCHICAL_RAG switch).
    # Redis unreachable → _layer_call soft-falls direct fetcher call.
    from backend.config import settings as _stg
    _cache_ttl = int(getattr(_stg, "RAG_HIER_CACHE_TTL_SEC", 300))

    # R8 cache-hit telemetry (2026-05-18 follow-up): track per-layer cache
    # hits so the r8_query_log row at end-of-query knows whether ANY layer
    # served from cache. cache_hit=True semantic = "at least one layer
    # short-circuited via Redis" (the schema is a single bool, not
    # per-layer). Closure-captured counter avoids changing _layer_call
    # signature.
    _cache_hits_in_query: List[int] = [0]

    async def _layer_call(layer_name: str, cache_params: Dict[str, Any], fetcher):
        """Wrap a layer call with Redis cache. ``fetcher`` is an async no-arg
        callable returning ``(succ, fail)``. Cache miss → call fetcher +
        write-through. Redis unreachable → _cache_get/_set soft-fall and
        fetcher still runs."""
        key = _make_layer_cache_key(layer_name, cache_params)
        cached = await _cache_get(key)
        if cached is not None:
            _cache_hits_in_query[0] += 1
            return cached
        s_, f_ = await fetcher()
        await _cache_set(key, s_, f_, _cache_ttl)
        return s_, f_

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
    # Phase 4 PR0.5 (Sprint 0, 2026-05-19): ENABLE_R8_L0 sub-flag — when
    # R12 LLM_MODE=assistant sentinel ON, this flag flips False globally so
    # the L0 (exact-expression-hash match) layer is skipped, letting the
    # assistant-mode-generated hypotheses fall through to L1 pillar / L2
    # family / L3 field. The other 3 layers stay LIVE.
    _r8_l0_on = bool(getattr(_stg, "ENABLE_R8_L0", True))
    if (
        _r8_l0_on
        and current_expression
        and (remaining_pat > 0 or remaining_fail > 0)
    ):
        _l0_budget = layer_budgets.get("L0", 5)
        s, f = await _layer_call(
            "L0",
            {"expr": current_expression, "region": region,
             "dataset": dataset_id, "budget": _l0_budget},
            lambda: layer0_exact_match(
                db, current_expression=current_expression, region=region,
                dataset_id=dataset_id, budget=_l0_budget,
            ),
        )
        result.total_queries += 1
        _consume(s, f, "L0")

    # L1 — pillar/theme
    if (current_expression or hypothesis_pillar) and (remaining_pat > 0 or remaining_fail > 0):
        _l1_budget = layer_budgets.get("L1", 5)
        s, f = await _layer_call(
            "L1",
            {"expr": current_expression, "pillar": hypothesis_pillar,
             "region": region, "budget": _l1_budget},
            lambda: layer1_pillar(
                db, current_expression=current_expression,
                hypothesis_pillar=hypothesis_pillar, region=region,
                budget=_l1_budget,
            ),
        )
        result.total_queries += 1
        _consume(s, f, "L1")

    # L2 — family_signature (R8-v2 #3 R5 ranking always on — retired
    # ENABLE_R5_L2_RANKING 2026-05-19, subsumed into main hierarchical switch).
    if current_expression and (remaining_pat > 0 or remaining_fail > 0):
        _r5_min_samples = int(getattr(_stg, "R5_L2_RANKING_MIN_SAMPLES", 1))
        _r5_lookback_days = int(getattr(_stg, "R5_L2_RANKING_LOOKBACK_DAYS", 30))
        _l2_budget = layer_budgets.get("L2", 5)
        s, f = await _layer_call(
            "L2",
            {"expr": current_expression, "region": region,
             "budget": _l2_budget,
             "r5_min": _r5_min_samples, "r5_lb": _r5_lookback_days,
             "hyp": current_hypothesis or ""},
            lambda: layer2_family(
                db, current_expression=current_expression, region=region,
                budget=_l2_budget,
                enable_r5_ranking=True,
                r5_min_samples=_r5_min_samples,
                r5_lookback_days=_r5_lookback_days,
                current_hypothesis=current_hypothesis,
            ),
        )
        result.total_queries += 1
        _consume(s, f, "L2")

    # L3 — field-level
    if current_expression and (remaining_pat > 0 or remaining_fail > 0):
        _l3_budget = layer_budgets.get("L3", 5)
        s, f = await _layer_call(
            "L3",
            {"expr": current_expression, "region": region,
             "universe": universe, "budget": _l3_budget},
            lambda: layer3_field_level(
                db, current_expression=current_expression,
                region=region, universe=universe,
                budget=_l3_budget,
            ),
        )
        result.total_queries += 1
        _consume(s, f, "L3")

    logger.info(
        f"[hier_rag] query complete | patterns={len(result.patterns)} "
        f"pitfalls={len(result.pitfalls)} layer_hits={result.layer_hits} "
        f"sql_queries={result.total_queries}"
    )

    # R8 follow-up (2026-05-18): per-query telemetry INSERT — flag-gated,
    # soft-fail, dedicated session. Operator enables ENABLE_R8_QUERY_LOG
    # during 7d obs window after promoting ENABLE_HIERARCHICAL_RAG to
    # measure runtime layer fall-through patterns. Zero overhead when OFF.
    try:
        from backend.config import settings as _r8q_stg
        if bool(getattr(_r8q_stg, "ENABLE_R8_QUERY_LOG", False)):
            # R1b.3-v2 elevation signal — scan result.pitfalls for the meta
            # marker that _elevate_failure_tree_pitfalls stamps. Cheap
            # (max_pitfalls is small) and avoids changing the L2 return shape.
            _had_elevation = any(
                isinstance(e.meta_data, dict)
                and "_r1b_failure_tree_bonus_applied" in e.meta_data
                for e in (result.pitfalls or [])
            )
            await _write_r8_query_log(
                task_id=task_id,
                region=region,
                dataset_id=dataset_id,
                current_expression=current_expression,
                layer_hits=dict(result.layer_hits or {}),
                total_queries=int(result.total_queries or 0),
                cache_hit=bool(_cache_hits_in_query[0] > 0),
                had_failure_tree_elevation=_had_elevation,
            )
    except Exception as _r8q_ex:
        logger.warning(f"[hier_rag] R8 query_log write skipped (round unaffected): {_r8q_ex}")

    return result


async def _write_r8_query_log(
    *,
    task_id: Optional[int] = None,
    region: Optional[str],
    dataset_id: Optional[str],
    current_expression: Optional[str],
    layer_hits: Dict[str, int],
    total_queries: int,
    cache_hit: bool,
    had_failure_tree_elevation: bool,
) -> None:
    """R8 follow-up (2026-05-18) — soft-fail INSERT one row per
    query_hierarchical call. NEVER raises (caller wraps in try/except too).

    Dedicated AsyncSessionLocal so DB issues don't poison the RAG caller's
    session. Mirrors _write_r1b_retry_log_rows pattern.
    """
    try:
        from backend.database import AsyncSessionLocal
        from backend.models.r8_query_log import R8QueryLog
        from backend.alpha_semantic_validator import compute_expression_hash
    except Exception as ex:
        logger.debug(f"[hier_rag log] deps unavailable ({ex})")
        return
    expr_hash = None
    if current_expression:
        try:
            expr_hash = compute_expression_hash(current_expression)[:64]
        except Exception:
            expr_hash = None
    try:
        async with AsyncSessionLocal() as db:
            db.add(R8QueryLog(
                task_id=task_id,
                region=region,
                dataset_id=dataset_id,
                current_expression_hash=expr_hash,
                layer_hits=layer_hits,
                total_queries=total_queries,
                cache_hit=cache_hit,
                had_failure_tree_elevation=had_failure_tree_elevation,
            ))
            await db.commit()
    except Exception as ex:
        logger.warning(f"[hier_rag log] write failed (round unaffected): {ex}")
