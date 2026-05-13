"""
RAG Service - Enhanced Knowledge Base Retrieval for Mining Patterns

Features:
1. Dataset category-aware pattern retrieval
2. Region-specific pattern filtering
3. Intelligent fallback to generic patterns
4. Success/failure pattern recording with proper categorization
5. Pattern usage tracking and scoring
"""

from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime
import re
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, desc
from sqlalchemy.dialects.postgresql import JSONB
from loguru import logger

from backend.models import KnowledgeEntry, DatasetMetadata
# V-26.34 (2026-05-13): module-level binding for the regex-driven operator
# extractor. extract_operator_chain itself relies on Python's internal
# re-pattern cache so we don't need to pre-compile here — hoisting the
# import is enough to keep _filter_hallucinated tight in the hot path.
from backend.knowledge_extraction import extract_operator_chain as _extract_operator_chain


# Dataset category mapping for intelligent pattern matching
DATASET_CATEGORY_MAPPING = {
    "pv": ["pv", "price", "volume", "trade", "ohlc", "vwap"],
    "analyst": ["analyst", "anl", "estimate", "forecast", "recommendation", "eps", "target"],
    "fundamental": ["fundamental", "fnd", "fin", "balance", "income", "cash", "ratio", "margin"],
    "news": ["news", "sentiment", "headline", "article", "media", "social", "oth635"],
    "other": ["other", "oth", "misc", "alternative"],
}


# =============================================================================
# Layer 1 Anti-collapse helpers (2026-05-11) — diversity-aware retrieval
# =============================================================================

# Top-level wrapper op names that we treat as a "wrapper signature" for the
# purpose of diversity selection. Two patterns sharing the same wrapper +
# family are considered redundant.
_WRAPPER_OPS = (
    "group_neutralize", "group_rank", "group_zscore", "group_mean",
    "group_scale", "trade_when", "subtract", "rank", "zscore", "normalize",
    "quantile", "winsorize", "scale", "signed_power", "multiply",
    "ts_decay_linear", "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev",
)


def _extract_wrapper_signature(pattern: str) -> str:
    """Return the top-level wrapper op name of a pattern, or "raw" if none.

    Examples:
        "group_neutralize(rank(returns), industry)" → "group_neutralize"
        "rank(returns)"                             → "rank"
        "ts_rank(close, 20)"                        → "ts_rank"
        "T2 wrap of seed alphas with ..."           → "raw" (NL pattern)
    """
    if not pattern:
        return "raw"
    text = pattern.strip()
    # Match leading identifier followed by `(`
    m = re.match(r"^\s*([a-zA-Z_][\w]*)\s*\(", text)
    if not m:
        return "raw"
    op = m.group(1).lower()
    return op if op in _WRAPPER_OPS else "other_op"


def _classify_pattern_family(text: str) -> str:
    """Classify a pattern by field family — mirrors strategy_prompts._classify_family.

    Kept locally so RAG retrieval doesn't import the prompts module
    (otherwise circular imports).
    """
    t = (text or "").lower()
    if "returns" in t or "ret_" in t:
        return "RETURNS"
    if "fnd" in t:
        return "FUNDAMENTAL"
    if "anl" in t or "fam_" in t or "est" in t:
        return "ANALYST"
    if "snt" in t or "news" in t or "social" in t:
        return "SENTIMENT"
    if "fscore" in t or "model_" in t or "mdl" in t or "composite" in t or "_score_" in t:
        return "FACTOR_COMPOSITE"
    if "opt" in t or "implied_vol" in t:
        return "OPTION"
    if any(w in t for w in ("close", "open", "high", "low", "vwap", "volume", "amount", "cap")):
        return "PRICE_PV"
    return "OTHER"


def infer_dataset_category(dataset_id: str) -> str:
    """
    Infer the category of a dataset from its ID.
    
    Args:
        dataset_id: Dataset identifier (e.g., "analyst15", "pv6", "other635")
    
    Returns:
        Category string (pv, analyst, fundamental, news, other)
    """
    if not dataset_id:
        return "other"
    
    dataset_lower = dataset_id.lower()
    
    for category, keywords in DATASET_CATEGORY_MAPPING.items():
        for keyword in keywords:
            if keyword in dataset_lower:
                return category
    
    return "other"


class RAGResult:
    """RAG query result container with enhanced metadata."""
    
    def __init__(
        self,
        patterns: List[Dict] = None,
        pitfalls: List[Dict] = None,
        dataset_info: Optional[Dict] = None,
        category: str = "other",
        region: str = None
    ):
        self.patterns = patterns or []
        self.pitfalls = pitfalls or []
        self.dataset_info = dataset_info
        self.category = category
        self.region = region
    
    def to_dict(self) -> Dict:
        return {
            "patterns": self.patterns,
            "pitfalls": self.pitfalls,
            "dataset_info": self.dataset_info,
            "category": self.category,
            "region": self.region
        }
    
    def get_few_shot_text(self) -> str:
        """Format patterns as few-shot examples for prompts."""
        if not self.patterns:
            return "暂无成功模式参考"
        
        lines = []
        for p in self.patterns:
            pattern = p.get('pattern', '')
            desc = p.get('description', '')
            sharpe = p.get('metadata', {}).get('expected_sharpe', '')
            sharpe_str = f" [Expected Sharpe: {sharpe}]" if sharpe else ""
            lines.append(f"- {pattern}: {desc}{sharpe_str}")
        
        return "\n".join(lines)
    
    def get_constraints_text(self) -> str:
        """Format pitfalls as negative constraints for prompts."""
        if not self.pitfalls:
            return "暂无特殊限制"
        
        lines = []
        for p in self.pitfalls:
            pattern = p.get('pattern', '')
            desc = p.get('description', '')
            err_type = p.get('error_type', '')
            err_str = f" [{err_type}]" if err_type else ""
            lines.append(f"- 避免: {pattern}{err_str} (原因: {desc})")
        
        return "\n".join(lines)


class RAGService:
    """
    Enhanced Knowledge Base Retrieval Service.
    
    Features:
    - Category-aware success pattern retrieval
    - Region-specific pattern filtering
    - Intelligent fallback to generic patterns
    - Failure pitfall retrieval with severity ranking
    - Pattern usage tracking
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
        # V-24.C (2026-05-13): cache of active BRAIN op names for the
        # retrieve-side hallucination filter. Lazy-loaded on first
        # _filter_hallucinated call within this RAGService instance;
        # instances are usually per-request so the cache effectively
        # refreshes each mining round. V-22.8 daily beat still handles
        # long-term cleanup of bad rows.
        self._valid_ops_cache: Optional[Set[str]] = None
        self._retrieve_hallucinated_skipped: int = 0
        # V-26.35 (2026-05-13): failure counter for the valid_ops load
        # path. Pre-fix the load silently fell back to empty-set cache
        # forever after the first failure, leaving every subsequent
        # retrieve unfiltered. Now we don't poison the cache on failure
        # — next call re-tries — and surface the failure as ERROR + a
        # counter so dashboards / alerts can pick it up.
        self._valid_ops_load_failures: int = 0

    async def _get_valid_ops(self) -> Set[str]:
        """Load active operator whitelist from DB (cached per instance).

        V-26.35: on load failure, leave the cache as None so a subsequent
        call re-attempts. The caller (`_filter_hallucinated`) treats an
        empty set as "filter unavailable" and falls open intentionally —
        we don't want a single transient DB blip to disable the filter
        for the rest of this RAGService instance's lifetime.
        """
        if self._valid_ops_cache is not None:
            return self._valid_ops_cache
        from backend.models.metadata import Operator
        try:
            rows = (
                await self.db.execute(
                    select(Operator.name).where(Operator.is_active == True)  # noqa: E712
                )
            ).all()
            self._valid_ops_cache = {r[0] for r in rows}
        except Exception as e:
            # V-26.35: ERROR level so monitoring catches it, do NOT cache
            # the failure (set stays None → next call retries).
            self._valid_ops_load_failures += 1
            logger.error(
                f"[RAGService] V-24.C valid_ops load failed "
                f"(attempt #{self._valid_ops_load_failures}, filter disabled this call): {e}"
            )
            return set()
        return self._valid_ops_cache

    # Skeleton placeholders that extract_operator_chain regex picks up but
    # are NOT operators — exclude from whitelist comparison. Same set as
    # tasks/llm_op_monitor.py V-22.8 sweep.
    _SKELETON_PLACEHOLDERS: Set[str] = {"field", "num"}

    async def _filter_hallucinated(self, entries: List) -> List:
        """V-24.C — proactive op-whitelist filter on retrieve side.

        For each KB entry, extract ops from pattern + meta_data.template;
        if any op isn't in the active BRAIN registry (excluding skeleton
        placeholders), drop the entry from this round's retrieve result.
        V-22.8 daily sweep still soft-deactivates these rows; this filter
        just stops them from reaching the LLM between sweep runs and
        catches entries written via paths that bypass V-22.3 canonicalize.

        V-26.34 (2026-05-13): import hoisted out of the per-call body
        (the Python module cache makes it cheap, but the lookup still
        showed up in profiles on KB scans of 5k+ rows). The placeholder
        set is captured once per call instead of an attribute lookup
        per op. Each entry now scans pattern + template ops once and
        short-circuits on the first miss — unchanged but documented.
        """
        if not entries:
            return entries
        valid_ops = await self._get_valid_ops()
        if not valid_ops:
            return entries  # whitelist unavailable → fail-open (V-26.35)

        placeholders = self._SKELETON_PLACEHOLDERS
        _extract = _extract_operator_chain

        def _bad(text: str) -> bool:
            if not text:
                return False
            ops = _extract(text) or []
            for o in ops:
                ol = o.lower()
                if ol in placeholders:
                    continue
                if ol not in valid_ops:
                    return True
            return False

        kept = []
        for entry in entries:
            md = entry.meta_data or {}
            if _bad(entry.pattern) or _bad(md.get("template") or ""):
                self._retrieve_hallucinated_skipped += 1
                logger.debug(
                    f"[RAGService] V-24.C skip hallucinated entry id={entry.id} "
                    f"pattern={(entry.pattern or '')[:60]!r}"
                )
                continue
            kept.append(entry)
        return kept

    async def _track_retrieval_hit(self, entry_ids: List[int]) -> None:
        """V-24.D (2026-05-13) — pattern hit tracking.

        Bumps usage_count + updated_at on the entries that were actually
        selected for return to the LLM (post-filter / post-diversity-greedy).
        Best-effort: caller swallows exceptions so retrieval never fails.

        Combined with the existing record-time +1 on record_success_pattern
        and record_failure_pattern, usage_count becomes a unified "pattern
        activity" metric. updated_at doubles as "last activity time" — the
        kb_hit_audit.py script flags cold patterns (no activity in 30+
        days) as pruning candidates.

        L1 anti-collapse (line ~622 in get_recent_pass_examples) already
        penalises usage_count >= 5, so adding retrieve increments here
        won't recreate the pre-V-22 collapse loop.

        V-26.11 (2026-05-13): writes go through an isolated AsyncSession.
        Pre-fix this used self.db and committed mid-retrieve, which dragged
        the caller's in-flight transaction across the line — if the alpha
        INSERT that triggered this retrieve later rolled back, the hit-
        track was already on disk. Isolated session keeps the bookkeeping
        write decoupled from the caller's transaction lifetime.
        """
        if not entry_ids:
            return
        from backend.database import AsyncSessionLocal
        from backend.repositories.knowledge_repository import KnowledgeRepository
        try:
            async with AsyncSessionLocal() as kb_db:
                repo = KnowledgeRepository(kb_db)
                await repo.bulk_increment_usage(entry_ids)
                await kb_db.commit()
        except Exception as e:
            logger.warning(f"[RAGService] V-24.D hit-track failed (non-fatal): {e}")

    async def query(
        self,
        dataset_id: str = None,
        region: str = None,
        max_patterns: int = 5,
        max_pitfalls: int = 10,
        hypothesis_id: int = None,
    ) -> RAGResult:
        """
        Query knowledge base for relevant patterns and pitfalls.

        Enhanced with category-aware retrieval:
        1. First try dataset-specific patterns
        2. Then category-specific patterns
        3. Finally fall back to generic patterns

        Args:
            dataset_id: Optional dataset to filter by
            region: Optional region to filter by
            max_patterns: Maximum success patterns to return
            max_pitfalls: Maximum failure pitfalls to return
            hypothesis_id: V-26.12 (2026-05-13) — when set, KB entries whose
                meta_data.hypothesis_ids contains this id receive a score
                boost (same-family preference). Closes the Phase 2 B8 write
                vs. read asymmetry: the write path
                (record_success_pattern / record_failure_pattern) already
                tags entries with hypothesis_id, but the retrieve path
                ignored the tag entirely. This is a soft preference, not a
                hard filter — passing it does not collapse the retrieval
                pool to same-family-only.

        Returns:
            RAGResult with patterns, pitfalls, and dataset info
        """
        # Infer category from dataset_id
        category = infer_dataset_category(dataset_id) if dataset_id else "other"

        logger.debug(
            f"[RAGService] Query | dataset={dataset_id} region={region} "
            f"category={category} hypothesis_id={hypothesis_id}"
        )

        # Get success patterns with category awareness
        patterns = await self._get_success_patterns_enhanced(
            dataset_id=dataset_id,
            category=category,
            region=region,
            limit=max_patterns,
            hypothesis_id=hypothesis_id,
        )

        # Get failure pitfalls
        pitfalls = await self._get_failure_pitfalls_enhanced(
            dataset_id=dataset_id,
            category=category,
            region=region,
            limit=max_pitfalls,
            hypothesis_id=hypothesis_id,
        )
        
        # Get dataset info
        dataset_info = None
        if dataset_id:
            dataset_info = await self._get_dataset_info(dataset_id)
        
        logger.info(
            f"[RAGService] Query complete | "
            f"category={category} patterns={len(patterns)} pitfalls={len(pitfalls)}"
        )
        
        return RAGResult(
            patterns=patterns,
            pitfalls=pitfalls,
            dataset_info=dataset_info,
            category=category,
            region=region
        )
    
    async def _get_success_patterns_enhanced(
        self,
        dataset_id: str = None,
        category: str = "other",
        region: str = None,
        limit: int = 5,
        hypothesis_id: int = None,
    ) -> List[Dict]:
        """
        Get success patterns with intelligent category matching.
        
        Priority order:
        1. Exact dataset match
        2. Category match
        3. Region match
        4. Generic patterns (sorted by usage/score)
        """
        patterns = []

        # V-26.8 (2026-05-13): SUCCESS_PATTERN pool currently ~180 rows
        # (audit 2026-05-13). Cap at 800 so this query stays bounded as
        # the KB grows, and ORDER BY id DESC so newly-recorded patterns
        # are always in the candidate window for scoring. The Python-side
        # scorer below ranks across the bounded set; older rows fall out
        # of the window only when the KB exceeds 800 active SUCCESS rows,
        # at which point the per-instance retrieve cost was already
        # dominating mining time.
        query = (
            select(KnowledgeEntry)
            .where(
                KnowledgeEntry.entry_type == 'SUCCESS_PATTERN',
                KnowledgeEntry.is_active == True
            )
            .order_by(KnowledgeEntry.id.desc())
            .limit(800)
        )

        result = await self.db.execute(query)
        entries = list(result.scalars().all())

        # V-24.C: pre-score op-whitelist filter
        entries = await self._filter_hallucinated(entries)

        # Score and sort patterns
        scored_patterns = []
        for entry in entries:
            metadata = entry.meta_data or {}

            # Skip region config entries (they're metadata, not patterns)
            if metadata.get('pattern_type') == 'region_config':
                continue
            
            score = 0.0
            
            # 1. Dataset match (highest priority)
            entry_dataset = metadata.get('dataset', metadata.get('dataset_id', ''))
            if dataset_id and entry_dataset:
                if entry_dataset.lower() == dataset_id.lower():
                    score += 100.0
            
            # 2. Category match
            entry_categories = metadata.get('dataset_categories', [])
            entry_category = metadata.get('dataset_category', '')
            
            if category:
                if category in entry_categories:
                    score += 50.0
                elif entry_category == category:
                    score += 50.0
                elif category in str(entry_category).lower():
                    score += 30.0
            
            # 3. Region match
            entry_regions = metadata.get('regions', [])
            if region:
                if region in entry_regions:
                    score += 20.0
                elif not entry_regions:  # Generic pattern
                    score += 5.0
            
            # 4. Base score from metadata
            base_score = metadata.get('score', 0.5)
            expected_sharpe = metadata.get('expected_sharpe', 1.0)
            score += base_score * 10.0
            score += min(expected_sharpe, 2.0) * 5.0
            
            # 5. Usage count bonus (popular patterns)
            score += min(entry.usage_count or 0, 10) * 0.5

            # 6. V-26.12 (2026-05-13) — hypothesis-family preference. The
            # write path tags KB rows with hypothesis_ids; surface that
            # tag here as a soft score boost. Same-family pattern beats
            # generic dataset/category matches but doesn't dominate them
            # — caller still gets cross-hypothesis diversity if there
            # aren't enough same-family rows.
            if hypothesis_id is not None:
                hids = metadata.get('hypothesis_ids') or []
                # Tolerate single-int legacy field
                if not isinstance(hids, list):
                    hids = [hids]
                if hypothesis_id in hids:
                    score += 40.0

            scored_patterns.append({
                'entry': entry,
                'metadata': metadata,
                'score': score
            })
        
        # Sort by score descending
        scored_patterns.sort(key=lambda x: x['score'], reverse=True)
        
        # Build result list
        selected_ids: List[int] = []
        for sp in scored_patterns[:limit]:
            entry = sp['entry']
            metadata = sp['metadata']
            selected_ids.append(entry.id)
            patterns.append({
                'pattern': entry.pattern,
                'description': entry.description,
                'usage_count': entry.usage_count,
                'metadata': metadata,
                'match_score': sp['score']
            })

        # V-24.D: hit-track only the entries actually returned to the LLM
        await self._track_retrieval_hit(selected_ids)

        # Log pattern sources for debugging
        if patterns:
            sources = [p.get('metadata', {}).get('source', 'unknown') for p in patterns]
            logger.debug(f"[RAGService] Pattern sources: {sources}")

        return patterns
    
    async def _get_failure_pitfalls_enhanced(
        self,
        dataset_id: str = None,
        category: str = "other",
        region: str = None,
        limit: int = 10,
        hypothesis_id: int = None,
    ) -> List[Dict]:
        """
        Get failure pitfalls with severity-based ranking.
        
        Priority:
        1. High severity errors first
        2. Category-relevant pitfalls
        3. Recent pitfalls
        """
        # V-26.8 (2026-05-13): FAILURE_PITFALL pool currently ~1660 rows
        # and growing as feedback accumulates. Pulling the whole table
        # every retrieve put steady CPU/memory pressure on each mining
        # round (Python-side scoring across ~1660 dicts × every alpha).
        # Cap at 800 ORDER BY id DESC so we always see the freshest
        # mistakes (which are also the ones most likely repeated by the
        # next LLM batch) while keeping the per-call cost bounded.
        query = (
            select(KnowledgeEntry)
            .where(
                KnowledgeEntry.entry_type == 'FAILURE_PITFALL',
                KnowledgeEntry.is_active == True
            )
            .order_by(KnowledgeEntry.id.desc())
            .limit(800)
        )

        result = await self.db.execute(query)
        entries = list(result.scalars().all())

        # V-24.C: pre-score op-whitelist filter
        entries = await self._filter_hallucinated(entries)

        # Score pitfalls
        scored_pitfalls = []
        severity_weights = {'high': 30, 'medium': 20, 'low': 10}
        
        for entry in entries:
            metadata = entry.meta_data or {}
            score = 0.0
            
            # Severity weight
            severity = metadata.get('severity', 'medium')
            score += severity_weights.get(severity, 15)
            
            # Category relevance
            pitfall_category = metadata.get('dataset_category', '')
            if category and pitfall_category:
                if category == pitfall_category:
                    score += 20.0
            
            # Error type relevance
            error_type = metadata.get('error_type', '')
            # Prioritize type errors and syntax errors
            if error_type in ['TYPE_ERROR', 'SYNTAX_ERROR', 'SEMANTIC_ERROR']:
                score += 15.0

            # V-26.12 (2026-05-13) — hypothesis-family soft preference,
            # symmetric with _get_success_patterns_enhanced. Same-family
            # pitfalls beat generic ones so the LLM is reminded of mistakes
            # made under THIS hypothesis specifically (avoiding
            # ImplementationFault repetition).
            if hypothesis_id is not None:
                hids = metadata.get('hypothesis_ids') or []
                if not isinstance(hids, list):
                    hids = [hids]
                if hypothesis_id in hids:
                    score += 25.0

            scored_pitfalls.append({
                'entry': entry,
                'metadata': metadata,
                'score': score
            })
        
        # Sort by score
        scored_pitfalls.sort(key=lambda x: x['score'], reverse=True)
        
        # Build result
        pitfalls = []
        selected_ids: List[int] = []
        for sp in scored_pitfalls[:limit]:
            entry = sp['entry']
            metadata = sp['metadata']
            selected_ids.append(entry.id)
            pitfalls.append({
                'pattern': entry.pattern,
                'description': entry.description,
                'error_type': metadata.get('error_type'),
                'severity': metadata.get('severity'),
                'metadata': metadata
            })

        # V-24.D: hit-track returned pitfalls
        await self._track_retrieval_hit(selected_ids)

        return pitfalls
    
    # Legacy method for backward compatibility
    async def _get_success_patterns(
        self,
        dataset_id: str = None,
        region: str = None,
        limit: int = 5
    ) -> List[Dict]:
        """Legacy method - redirects to enhanced version."""
        category = infer_dataset_category(dataset_id) if dataset_id else "other"
        return await self._get_success_patterns_enhanced(
            dataset_id=dataset_id,
            category=category,
            region=region,
            limit=limit
        )
    
    # Legacy method for backward compatibility
    async def _get_failure_pitfalls(
        self,
        dataset_id: str = None,
        region: str = None,
        limit: int = 10
    ) -> List[Dict]:
        """Legacy method - redirects to enhanced version."""
        category = infer_dataset_category(dataset_id) if dataset_id else "other"
        return await self._get_failure_pitfalls_enhanced(
            dataset_id=dataset_id,
            category=category,
            region=region,
            limit=limit
        )
    
    async def get_field_blacklist(self, region: str = None) -> List[str]:
        """Get list of blacklisted fields."""
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.entry_type == 'FIELD_BLACKLIST',
            KnowledgeEntry.is_active == True
        )
        
        result = await self.db.execute(query)
        entries = result.scalars().all()
        
        blacklist = []
        for entry in entries:
            metadata = entry.meta_data or {}
            if region and metadata.get('region') and metadata['region'] != region:
                continue
            
            field_name = metadata.get('field') or entry.pattern
            if field_name:
                blacklist.append(field_name)
        
        return blacklist
    
    async def get_recent_pass_examples(
        self,
        region: Optional[str] = None,
        dataset_id: Optional[str] = None,
        limit: int = 5,
        days_window: int = 7,
        prefer_hitl: bool = True,
        hitl_min_count: int = 5,
        factor_tier: Optional[int] = None,
        hypothesis_id: Optional[int] = None,
        experiment_variant: Optional[str] = None,
    ) -> List[Dict]:
        """W6 (revised post-T9): rolling few-shot pool with dataset HARD filter.

        Changes vs. v1:
          - Dataset mismatch is now a HARD filter (not a -0.2 score). Mismatched
            patterns are dropped instead of ranked lower. T9 confirmed that a
            single mismatched HITL sample (fnd6) ranked #1 on a fundamental2 task
            and produced no learning.
          - HITL bonus only kicks in when global HITL count >= `hitl_min_count`
            (default 5). One-off HITL signals are too noisy to bias prompts.

        Region remains soft-match (+0.2 score) to allow cross-region transfer.

        PR2 — tier filter:
          - `factor_tier` (1/2/3) restricts results to that tier; T1 task should
            pass factor_tier=1 to avoid contaminating LLM context with T2/T3
            wrappers.
          - Cold-start fallback: when factor_tier=1 returns < 3 rows, the method
            pulls historical T2 PASS patterns and strips one wrapper layer to
            synthesize T1 kernels (marked `is_synthesized=True`). Caller's prompt
            should warn the LLM these aren't real T1 PASS examples.
        """
        from datetime import datetime, timedelta
        from sqlalchemy import func

        cutoff = datetime.utcnow() - timedelta(days=days_window)

        # Count global HITL samples to decide whether to apply HITL bonus
        hitl_count_stmt = select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
            KnowledgeEntry.is_active == True,
            KnowledgeEntry.created_by == "HITL",
        )
        hitl_count = (await self.db.execute(hitl_count_stmt)).scalar() or 0
        apply_hitl_bonus = prefer_hitl and hitl_count >= hitl_min_count

        stmt = (
            select(KnowledgeEntry)
            .where(
                KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
                KnowledgeEntry.is_active == True,
                KnowledgeEntry.updated_at >= cutoff,
            )
            .order_by(KnowledgeEntry.usage_count.desc())
            .limit(limit * 6)  # over-fetch since dataset filter may drop many
        )
        if factor_tier is not None:
            stmt = stmt.where(KnowledgeEntry.factor_tier == factor_tier)
        result = await self.db.execute(stmt)
        rows = list(result.scalars().all())
        if not rows:
            return []

        # V-24.C: pre-everything op-whitelist filter so hallucinated rows
        # don't waste downstream scoring / diversity-selection budget.
        rows = await self._filter_hallucinated(rows)
        if not rows:
            return []

        # HARD dataset filter: only keep entries whose metadata.dataset matches
        # (or entries with NO dataset metadata — those are region-generic).
        if dataset_id:
            filtered = []
            for e in rows:
                md = e.meta_data or {}
                entry_ds = md.get("dataset_id") or md.get("dataset")
                if entry_ds is None or entry_ds == "":
                    filtered.append(e)  # generic patterns OK
                elif str(entry_ds).lower() == str(dataset_id).lower():
                    filtered.append(e)
                # else: drop (hard filter)
            dropped = len(rows) - len(filtered)
            rows = filtered
            if dropped:
                logger.info(
                    f"[RAGService] few-shot dataset filter | dropped={dropped} "
                    f"(target={dataset_id})"
                )

        # Plan v5+ §B8 — hypothesis-keyed retrieval. When the caller knows the
        # active hypothesis_id, prefer patterns that were produced by that
        # hypothesis (or its lineage) over generic ones. Soft filter: matching
        # entries are kept first; non-matching entries pass only if matching
        # set is empty (avoids starvation in the early lifecycle when KB is
        # cold).
        if hypothesis_id is not None:
            matching = []
            others = []
            for e in rows:
                md = e.meta_data or {}
                hids = md.get("hypothesis_ids") or []
                primary_hid = md.get("hypothesis_id")
                if hypothesis_id in hids or hypothesis_id == primary_hid:
                    matching.append(e)
                else:
                    others.append(e)
            rows = matching if matching else others
            if matching:
                logger.info(
                    f"[RAGService] few-shot hypothesis filter | matched={len(matching)} "
                    f"others_dropped={len(others)} (hypothesis_id={hypothesis_id})"
                )

        # Plan v5+ §F-5 — variant isolation. During Phase gate灰度 we want
        # hypotheses + their KB entries kept apart between variants so the
        # legacy / Phase 2 comparison stays clean.
        if experiment_variant is not None:
            filtered = []
            for e in rows:
                md = e.meta_data or {}
                entry_variant = md.get("experiment_variant")
                # Allow entries with no variant (cold-start / migration) and
                # entries that match the request.
                if entry_variant is None or str(entry_variant) == str(experiment_variant):
                    filtered.append(e)
            dropped = len(rows) - len(filtered)
            rows = filtered
            if dropped:
                logger.info(
                    f"[RAGService] few-shot variant filter | dropped={dropped} "
                    f"(variant={experiment_variant})"
                )

        if not rows:
            return []

        # L1 Anti-collapse (2026-05-11): score function + LRU penalty.
        # The previous logic capped usage_count at +0.2 BONUS — patterns the
        # LLM had already seen many times kept getting recommended, locking
        # the search neighborhood. Flip to LRU-style penalty: usage>=5 gets
        # -0.5, deterring over-fed patterns and giving fresher KB entries a
        # chance to surface. Diversity selection (below) is the real fix —
        # this is just to break ties in the candidate pool.
        def score(e: KnowledgeEntry) -> float:
            md = e.meta_data or {}
            conf = float(md.get("confidence", 0.5) or 0.5)
            is_hitl = e.created_by == "HITL" or md.get("source") == "hitl"
            hitl_bonus = 0.5 if (apply_hitl_bonus and is_hitl) else 0.0
            region_match = 0.2 if region and (md.get("region") == region or region in (md.get("regions") or [])) else 0.0
            uc = e.usage_count or 0
            if uc >= 5:
                lru = -0.5  # over-fed → deprioritize
            elif uc >= 1:
                lru = 0.05 * uc  # mild boost for recently-validated patterns
            else:
                lru = 0.0
            return conf + hitl_bonus + region_match + lru

        rows.sort(key=score, reverse=True)

        # L1 Anti-collapse: diversity-aware greedy selection.
        # Without this, the top-K result set is dominated by whichever
        # (family, wrapper) tuple has the most entries. Greedy walk picks
        # one entry per (family, wrapper) until limit; if exhausted before
        # limit, fill the rest by score order.
        candidate_pool = rows[: max(limit * 3, 12)]
        chosen_keys: set = set()
        diverse_pick: list = []
        leftover: list = []
        for entry in candidate_pool:
            text = entry.pattern or entry.description or ""
            fam = _classify_pattern_family(text)
            wrap = _extract_wrapper_signature(entry.pattern or "")
            key = (fam, wrap)
            if key not in chosen_keys and len(diverse_pick) < limit:
                chosen_keys.add(key)
                diverse_pick.append(entry)
            else:
                leftover.append(entry)
        # Top-up by score order if we ran out of unique (family, wrapper)
        if len(diverse_pick) < limit:
            need = limit - len(diverse_pick)
            diverse_pick.extend(leftover[:need])

        # V-24.D: hit-track final selected entries (post-diversity-greedy)
        await self._track_retrieval_hit([e.id for e in diverse_pick])

        out = []
        for entry in diverse_pick:
            md = entry.meta_data or {}
            out.append({
                "pattern": entry.pattern,
                "description": entry.description or "",
                "expected_sharpe": md.get("expected_sharpe"),
                "expected_fitness": md.get("expected_fitness"),
                "confidence": md.get("confidence", 0.5),
                "source": entry.created_by,
                "usage_count": entry.usage_count,
                "factor_tier": entry.factor_tier,
                # V-22 (2026-05-10): surface BRAIN /check verdict so prompt
                # can show "BRAIN rejected on X" — gives the LLM real
                # feedback on submittability, not just IS PASS rate.
                "brain_can_submit": md.get("brain_can_submit"),
                "brain_failed_checks": md.get("brain_failed_checks") or [],
            })

        # T1 cold-start fallback (PR2): when T1 KB has < 3 entries, synthesize
        # T1 kernels by stripping one wrapper layer from historical T2 KB rows.
        # The LLM sees these flagged as is_synthesized=True so the prompt can
        # warn against treating them as proven PASS examples.
        if factor_tier == 1 and len(out) < 3:
            from backend.factor_tier_classifier import extract_tier1_seed, is_t1_expression

            t2_stmt = (
                select(KnowledgeEntry)
                .where(
                    KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
                    KnowledgeEntry.is_active == True,
                    KnowledgeEntry.factor_tier == 2,
                )
                .order_by(KnowledgeEntry.usage_count.desc())
                .limit(10)
            )
            t2_rows = (await self.db.execute(t2_stmt)).scalars().all()
            synthesized = []
            for t2 in t2_rows:
                kernel = extract_tier1_seed(t2.pattern or "")
                if kernel and is_t1_expression(kernel):
                    md = t2.meta_data or {}
                    synthesized.append({
                        "pattern": kernel,
                        "description": (t2.description or "") + " (synthesized T1 kernel from T2)",
                        "expected_sharpe": md.get("expected_sharpe"),
                        "expected_fitness": md.get("expected_fitness"),
                        "confidence": (md.get("confidence", 0.5) or 0.5) * 0.5,  # lower trust
                        "source": "synthesized",
                        "usage_count": t2.usage_count,
                        "factor_tier": 1,
                        "is_synthesized": True,
                    })
            out.extend(synthesized[: max(0, 5 - len(out))])
            logger.info(
                f"[RAGService] T1 cold-start fallback synthesized {len(synthesized)} "
                f"kernels from T2 KB (out_total={len(out)})"
            )

        logger.info(
            f"[RAGService] few-shot pool | region={region} dataset={dataset_id} "
            f"tier={factor_tier} returned={len(out)} "
            f"(HITL={sum(1 for e in out if e.get('source')=='HITL')}, "
            f"hitl_bonus_active={apply_hitl_bonus}, global_hitl={hitl_count})"
        )
        return out

    async def _get_dataset_info(self, dataset_id: str) -> Optional[Dict]:
        """Get dataset metadata."""
        query = select(DatasetMetadata).where(
            DatasetMetadata.dataset_id == dataset_id
        ).limit(1)
        result = await self.db.execute(query)
        dataset = result.scalars().first()
        
        if not dataset:
            return None
        
        return {
            'dataset_id': dataset.dataset_id,
            'region': dataset.region,
            'category': dataset.category,
            'subcategory': dataset.subcategory,
            'description': dataset.description,
            'field_count': dataset.field_count,
            'mining_weight': dataset.mining_weight
        }
    
    async def update_pattern_brain_status(
        self,
        expression: str,
        can_submit: Optional[bool],
        failed_checks: Optional[List[Dict]] = None,
    ) -> bool:
        """V-22 (2026-05-10): write BRAIN /check verdict back into the
        SUCCESS_PATTERN entry whose skeleton matches `expression`. Called by
        refresh_can_submit_for_alpha after BRAIN sub-check completes.

        This closes the LLM feedback loop: the pattern was recorded as
        SUCCESS at IS-PASS time, now we tag it with the BRAIN-side verdict
        so future few-shot retrieval can surface "this looked great but
        BRAIN rejected on fitness/CW" — the LLM steers away.
        """
        from backend.knowledge_extraction import expression_to_skeleton
        from sqlalchemy.orm.attributes import flag_modified

        if not expression:
            return False
        try:
            skeleton = expression_to_skeleton(expression)
        except Exception:
            return False

        stmt = select(KnowledgeEntry).where(
            KnowledgeEntry.pattern == skeleton,
            KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
            KnowledgeEntry.is_active == True,
        )
        result = await self.db.execute(stmt)
        entry = result.scalar_one_or_none()
        if entry is None:
            return False

        md = entry.meta_data or {}
        md["brain_can_submit"] = can_submit
        # Keep only check name to avoid bloating meta_data
        md["brain_failed_checks"] = [
            {"name": c.get("name"), "result": c.get("result")}
            for c in (failed_checks or [])
            if c.get("name")
        ]
        md["brain_check_at"] = datetime.now().isoformat()
        entry.meta_data = md
        flag_modified(entry, "meta_data")
        await self.db.commit()
        logger.info(
            f"[RAGService] V-22 brain_status updated | skeleton={skeleton[:50]} "
            f"can_submit={can_submit} fails={len(md['brain_failed_checks'])}"
        )
        return True

    async def increment_pattern_usage(self, pattern: str) -> bool:
        """Increment usage count for a pattern (called on successful use)."""
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.pattern == pattern,
            KnowledgeEntry.is_active == True
        )
        result = await self.db.execute(query)
        entry = result.scalar_one_or_none()
        
        if entry:
            entry.usage_count += 1
            logger.debug(f"[RAGService] Incremented usage | pattern={pattern}")
            return True
        
        return False
    
    # =========================================================================
    # P0-fix-1: Knowledge Feedback Loop - Write patterns back to KB
    # =========================================================================
    
    async def record_failure_pattern(
        self,
        expression: str,
        error_type: str,
        metrics: Dict = None,
        region: str = None,
        dataset_id: str = None,
        hypothesis_id: Optional[int] = None,
        experiment_variant: Optional[str] = None,
    ) -> bool:
        """
        Record a failure pattern to the knowledge base.
        
        This is the KEY feedback loop that enables learning from failures.
        Called after evaluation identifies a failed alpha.
        """
        from backend.knowledge_extraction import expression_to_skeleton, extract_operator_chain
        
        try:
            # Extract pattern skeleton (structural, not specific)
            skeleton = expression_to_skeleton(expression)
            op_chain = extract_operator_chain(expression)
            
            # Infer category from dataset_id
            category = infer_dataset_category(dataset_id) if dataset_id else "other"
            
            # Check if similar pattern already exists
            existing = await self._find_similar_pitfall(skeleton, region)
            
            if existing:
                # Update existing pattern's failure count
                existing.meta_data = existing.meta_data or {}
                existing.meta_data['failure_count'] = existing.meta_data.get('failure_count', 0) + 1
                existing.meta_data['last_failure'] = datetime.now().isoformat()
                if metrics:
                    existing.meta_data['avg_sharpe'] = metrics.get('sharpe', 0)
                # Plan v5+ §B8: track every hypothesis that hit this pattern.
                # Use a deduped list so RAG retrieval can do "patterns this
                # hypothesis family has tripped" queries.
                if hypothesis_id is not None:
                    hids = list(existing.meta_data.get('hypothesis_ids') or [])
                    if hypothesis_id not in hids:
                        hids.append(hypothesis_id)
                    existing.meta_data['hypothesis_ids'] = hids
                # F-5: variant tag preserved as-is on first record (don't
                # overwrite — different variants get different KB entries
                # via _find_similar_pitfall already returning per-skeleton).
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(existing, 'meta_data')
                logger.debug(f"[RAGService] Updated existing pitfall | skeleton={skeleton[:50]}")
            else:
                # Create new pitfall entry
                description = self._generate_pitfall_description(error_type, metrics, op_chain)
                
                # Determine severity based on error type
                severity = 'medium'
                if error_type in ['TYPE_ERROR', 'SYNTAX_ERROR', 'SEMANTIC_ERROR']:
                    severity = 'high'
                elif error_type in ['LOW_SHARPE', 'HIGH_TURNOVER']:
                    severity = 'medium'
                elif error_type == 'NEGATIVE_SIGNAL':
                    severity = 'low'  # Can be fixed by sign flip
                # P0: BRAIN-side checks 是 submit 前的硬门槛，触发即等价于"不可提交"
                elif error_type in ['CONCENTRATED_WEIGHT', 'LOW_SUB_UNIVERSE_SHARPE',
                                    'HIGH_PROD_CORRELATION', 'HIGH_SELF_CORRELATION']:
                    severity = 'high'
                
                new_entry = KnowledgeEntry(
                    pattern=skeleton,
                    description=description,
                    entry_type='FAILURE_PITFALL',
                    is_active=True,
                    usage_count=0,
                    meta_data={
                        'source': 'feedback_loop',
                        'region': region,
                        'dataset': dataset_id,
                        'dataset_category': category,
                        'error_type': error_type,
                        'severity': severity,
                        'operator_chain': op_chain[:5] if op_chain else [],
                        'example_expression': expression[:200],
                        'failure_count': 1,
                        'sharpe': metrics.get('sharpe', 0) if metrics else 0,
                        'fitness': metrics.get('fitness', 0) if metrics else 0,
                        'turnover': metrics.get('turnover', 0) if metrics else 0,
                        'created_at': datetime.now().isoformat(),
                        # Plan v5+ §B8: typed Hypothesis reference for KB
                        # learning unit upgrade (alpha,hypothesis,...) instead
                        # of (alpha,dataset,...).
                        'hypothesis_id': hypothesis_id,
                        'hypothesis_ids': [hypothesis_id] if hypothesis_id is not None else [],
                        'experiment_variant': experiment_variant,
                    }
                )
                self.db.add(new_entry)
                logger.info(f"[RAGService] Created new pitfall | skeleton={skeleton[:50]} error={error_type} category={category}")
            
            await self.db.commit()
            return True
            
        except Exception as e:
            logger.error(f"[RAGService] Failed to record pitfall | error={e}")
            await self.db.rollback()
            return False
    
    async def record_success_pattern(
        self,
        expression: str,
        metrics: Dict,
        region: str = None,
        dataset_id: str = None,
        alpha_id: str = None,
        hypothesis_id: Optional[int] = None,
        experiment_variant: Optional[str] = None,
    ) -> bool:
        """
        Record a success pattern to the knowledge base.
        
        Called when an alpha passes all quality thresholds.
        """
        from backend.knowledge_extraction import expression_to_skeleton, extract_operator_chain
        
        try:
            skeleton = expression_to_skeleton(expression)
            op_chain = extract_operator_chain(expression)
            
            # Infer category from dataset_id
            category = infer_dataset_category(dataset_id) if dataset_id else "other"
            
            # Check if similar pattern exists
            existing = await self._find_similar_success(skeleton, region)
            
            if existing:
                # Update existing pattern
                existing.usage_count += 1
                existing.meta_data = existing.meta_data or {}
                existing.meta_data['success_count'] = existing.meta_data.get('success_count', 0) + 1
                existing.meta_data['last_success'] = datetime.now().isoformat()
                # Update running average metrics
                n = existing.meta_data.get('success_count', 1)
                old_sharpe = existing.meta_data.get('avg_sharpe', 0)
                existing.meta_data['avg_sharpe'] = (old_sharpe * (n-1) + metrics.get('sharpe', 0)) / n
                # Plan v5+ §B8: append every hypothesis that produced this pattern
                if hypothesis_id is not None:
                    hids = list(existing.meta_data.get('hypothesis_ids') or [])
                    if hypothesis_id not in hids:
                        hids.append(hypothesis_id)
                    existing.meta_data['hypothesis_ids'] = hids
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(existing, 'meta_data')
                logger.info(f"[RAGService] Updated success pattern | skeleton={skeleton[:50]}")
            else:
                # Create new success pattern with full category info
                sharpe = metrics.get('sharpe', 0)
                fitness = metrics.get('fitness', 0)
                turnover = metrics.get('turnover', 0)
                
                description = f"Sharpe: {sharpe:.2f}, Fitness: {fitness:.2f}, Turnover: {turnover:.2f}"
                
                # Calculate a quality score
                score = min(1.0, (sharpe / 2.0) * 0.6 + (fitness / 1.5) * 0.3 + max(0, (0.7 - turnover)) * 0.1)
                
                new_entry = KnowledgeEntry(
                    pattern=skeleton,
                    description=description,
                    entry_type='SUCCESS_PATTERN',
                    is_active=True,
                    usage_count=1,
                    meta_data={
                        'source': 'feedback_loop',
                        'region': region,
                        'regions': [region] if region else [],
                        'dataset': dataset_id,
                        'dataset_category': category,
                        'dataset_categories': [category],
                        'operator_chain': op_chain[:5] if op_chain else [],
                        'example_expression': expression[:200],
                        'alpha_id': alpha_id,
                        'success_count': 1,
                        'avg_sharpe': sharpe,
                        'avg_fitness': fitness,
                        'avg_turnover': turnover,
                        'expected_sharpe': sharpe,
                        'score': score,
                        'created_at': datetime.now().isoformat(),
                        # Plan v5+ §B8: typed Hypothesis reference + variant
                        # tag for KB learning unit upgrade.
                        'hypothesis_id': hypothesis_id,
                        'hypothesis_ids': [hypothesis_id] if hypothesis_id is not None else [],
                        'experiment_variant': experiment_variant,
                        # V-22 (2026-05-10) BRAIN feedback to LLM. The pattern
                        # is recorded at IS-PASS time; refresh_can_submit_for_
                        # alpha (30s countdown) later updates these fields with
                        # the BRAIN /check verdict. Retrieval surfaces them in
                        # the prompt so the LLM can see "this skeleton looked
                        # great on IS but BRAIN rejected it on fitness/CW/etc."
                        # and steer away.
                        'brain_can_submit': None,        # True / False / None (pending)
                        'brain_failed_checks': [],       # list of {name, ...} from BRAIN
                        'brain_check_at': None,          # ISO timestamp of last refresh
                    }
                )
                self.db.add(new_entry)
                logger.info(f"[RAGService] Created new success pattern | skeleton={skeleton[:50]} sharpe={sharpe:.2f} category={category}")
            
            await self.db.commit()
            return True
            
        except Exception as e:
            logger.error(f"[RAGService] Failed to record success | error={e}")
            await self.db.rollback()
            return False
    
    async def _find_similar_pitfall(self, skeleton: str, region: str = None) -> Optional[KnowledgeEntry]:
        """Find existing pitfall with similar skeleton"""
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.entry_type == 'FAILURE_PITFALL',
            KnowledgeEntry.pattern == skeleton,
            KnowledgeEntry.is_active == True
        )
        if region:
            # Also match patterns without region (global)
            pass  # We'll match exact skeleton first
        
        result = await self.db.execute(query)
        return result.scalar_one_or_none()
    
    async def _find_similar_success(self, skeleton: str, region: str = None) -> Optional[KnowledgeEntry]:
        """Find existing success pattern with similar skeleton"""
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.entry_type == 'SUCCESS_PATTERN',
            KnowledgeEntry.pattern == skeleton,
            KnowledgeEntry.is_active == True
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()
    
    def _generate_pitfall_description(self, error_type: str, metrics: Dict, op_chain: List) -> str:
        """Generate human-readable pitfall description"""
        parts = []
        
        if error_type == 'LOW_SHARPE':
            sharpe = metrics.get('sharpe', 0) if metrics else 0
            parts.append(f"低Sharpe ({sharpe:.2f})")
        elif error_type == 'LOW_FITNESS':
            fitness = metrics.get('fitness', 0) if metrics else 0
            parts.append(f"低Fitness ({fitness:.2f})")
        elif error_type == 'HIGH_TURNOVER':
            turnover = metrics.get('turnover', 0) if metrics else 0
            parts.append(f"高Turnover ({turnover:.2f})")
        elif error_type == 'HIGH_CORRELATION':
            parts.append("高相关性 - 与现有alpha重复")
        elif error_type == 'NEGATIVE_SIGNAL':
            parts.append("负信号 - 方向相反")
        # P0: BRAIN-side check FAIL — 专项归因带 settings/结构修法建议
        elif error_type == 'CONCENTRATED_WEIGHT':
            parts.append(
                "BRAIN集中度FAIL - 单股某日仓位>10%；"
                "修法：truncation降至0.04-0.06、改更细粒度neutralization(SUBINDUSTRY)、加winsorize/zscore截尾"
            )
        elif error_type == 'LOW_SUB_UNIVERSE_SHARPE':
            parts.append(
                "BRAIN子样本sharpe过低 - 信号在小池表现差，过拟合大池；"
                "修法：减小窗口/降低decay、加rank/quantile让信号在小样本更稳、避免过深嵌套"
            )
        elif error_type == 'HIGH_PROD_CORRELATION':
            parts.append(
                "BRAIN与已上线alpha相关性过高(>0.7)；"
                "修法：换字段、改算子链结构、加交互项让信号正交化"
            )
        elif error_type == 'HIGH_SELF_CORRELATION':
            parts.append(
                "BRAIN与本人已提交alpha相关性过高；"
                "修法：换字段或加trade_when择时让 PnL 路径分化"
            )
        else:
            parts.append(f"失败类型: {error_type}")
        
        if op_chain:
            parts.append(f"算子链: {' → '.join(op_chain[:3])}")
        
        return "; ".join(parts)
    
    async def get_region_config(self, region: str) -> Optional[Dict]:
        """
        Get recommended configuration for a region from knowledge base.
        
        Args:
            region: Region code (USA, KOR, ASI, etc.)
        
        Returns:
            Dict with recommended settings or None if not found
        """
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.entry_type == 'SUCCESS_PATTERN',
            KnowledgeEntry.pattern == f"REGION_CONFIG:{region.upper()}",
            KnowledgeEntry.is_active == True
        )
        
        result = await self.db.execute(query)
        entry = result.scalar_one_or_none()
        
        if entry and entry.meta_data:
            return {
                'region': region.upper(),
                'recommended_universe': entry.meta_data.get('recommended_universe'),
                'recommended_decay': entry.meta_data.get('recommended_decay'),
                'recommended_neutralization': entry.meta_data.get('recommended_neutralization'),
                'sharpe_adjustment': entry.meta_data.get('sharpe_adjustment', 1.0),
                'notes': entry.description
            }
        
        # Fallback to default USA settings if not found
        return {
            'region': region.upper(),
            'recommended_universe': 'TOP3000',
            'recommended_decay': 4,
            'recommended_neutralization': 'SUBINDUSTRY',
            'sharpe_adjustment': 1.0,
            'notes': 'Default settings'
        }
    
    async def get_patterns_by_category(
        self,
        category: str,
        region: str = None,
        limit: int = 10
    ) -> List[Dict]:
        """
        Get success patterns for a specific dataset category.
        
        Args:
            category: Dataset category (pv, analyst, fundamental, news, other)
            region: Optional region filter
            limit: Maximum patterns to return
        
        Returns:
            List of pattern dictionaries
        """
        return await self._get_success_patterns_enhanced(
            dataset_id=None,
            category=category,
            region=region,
            limit=limit
        )
