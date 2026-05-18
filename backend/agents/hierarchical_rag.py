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

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.knowledge import KnowledgeEntry, compute_pattern_hash

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
    "RAGEntry",
    "RAGResult",
    "layer0_exact_match",
    "layer3_field_level",
]
