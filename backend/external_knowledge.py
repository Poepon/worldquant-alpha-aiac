"""
External Knowledge Integration - Forum Posts and Academic Papers

Features:
1. Sync high-quality forum posts from BRAIN platform
2. Extract alpha patterns from forum discussions
3. Parse academic paper insights (101 Alphas, Alpha-GPT, etc.)
4. Periodic knowledge base updates

This module enriches the knowledge base with external best practices.
"""

import re
import json
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from loguru import logger

from backend.models import KnowledgeEntry
from backend.models.knowledge import compute_pattern_hash
from backend.config import settings


# =============================================================================
# import_batch constants (Phase 0, v1.3 SF-12)
# =============================================================================
# Single source of truth for `meta_data["import_batch"]` markers. All Phase 0
# import paths + backfill scripts + rollback SQL reference these constants
# (NOT the magic string itself) so a typo in one place won't desync rollback.
#
# Future Phase 1+ batches add new constants here following the same pattern:
#   PHASE<N>_<TASK>_IMPORT_BATCH = "phase<N>_<task>_<topic>_<YYYY_MM>"
# =============================================================================

PHASE0_Q1_IMPORT_BATCH = "phase0_q1_kakushadze_2026_05"
PHASE0_Q3_IMPORT_BATCH = "phase0_q3_alpha158_2026_05"
PHASE1_Q2_IMPORT_BATCH = "phase1_q2_openassetpricing_2026_05"
PHASE1_Q6_IMPORT_BATCH = "phase1_q6_alpha191_chn_2026_05"
LEGACY_SEED_IMPORT_BATCH = "legacy_seed"


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class ForumPost:
    """Represents a forum post from BRAIN platform."""
    post_id: str
    title: str
    content: str
    author: str
    likes: int = 0
    views: int = 0
    replies: int = 0
    created_at: datetime = None
    
    # Extracted patterns
    alpha_patterns: List[str] = field(default_factory=list)
    insights: List[str] = field(default_factory=list)
    relevance_score: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "post_id": self.post_id,
            "title": self.title,
            "author": self.author,
            "likes": self.likes,
            "views": self.views,
            "replies": self.replies,
            "alpha_patterns": self.alpha_patterns,
            "insights": self.insights,
            "relevance_score": self.relevance_score,
        }


@dataclass
class ExternalKnowledge:
    """External knowledge entry for import."""
    source: str  # "forum", "paper", "documentation"
    pattern: str
    description: str
    category: str  # Dataset category relevance

    # Quality indicators
    confidence: float = 0.5
    verified: bool = False

    # Metadata
    source_url: str = ""
    source_title: str = ""
    extraction_date: datetime = field(default_factory=datetime.now)

    # Phase 0 Q3 v1.5 — Alpha158 raw-feature flag (None for non-Q3 sources).
    # When True, R8 Hierarchical RAG should DOWN-weight this row when
    # surfacing patterns to the LLM because raw OHLCV ratios (e.g. KMID =
    # (close-open)/open) are ML features, not BRAIN-submittable alphas
    # — feeding them as success exemplars risks LLM generating wrappers-less
    # expressions that always fail BRAIN sim. Phase 0 ingests the flag only.
    raw_feature: Optional[bool] = None
    # Original Qlib expression text (only set for Q3 alpha158 rows; preserved
    # so future role-recategorization or wrapper-injection can trace back).
    qlib_origin: Optional[str] = None

    # Phase 1 Q2 (2026-05-17) — openassetpricing dual-path forward-compat:
    # is_anchor_metadata=True → row is plain-English anchor (NOT a BRAIN-DSL
    # expression), written as entry_type='ANCHOR_METADATA'. RAG SQL MUST
    # exclude this row via meta_data filter (MF-V1.2-6). The remaining
    # openassetpricing-specific fields preserve provenance for future
    # retranslation (1 SQL UPDATE per [[feedback_forward_compat_metadata_hook]]).
    is_anchor_metadata: Optional[bool] = None
    openassetpricing_signal: Optional[str] = None  # e.g. "Predictors/Accruals.py"
    llm_translation_version: Optional[str] = None  # e.g. "claude-opus-4-7-high-v1"
    translation_confidence: Optional[float] = None  # LLM-emit confidence ∈ [0,1]
    translation_notes: Optional[str] = None         # short LLM-emit caveat
    theoretical_anchor: Optional[str] = None        # academic citation, e.g. "Sloan 1996"
    paper_citation: Optional[str] = None            # full reference


# =============================================================================
# Pattern Extraction
# =============================================================================

# Regex patterns for alpha expression extraction
ALPHA_EXPRESSION_PATTERNS = [
    # Code blocks
    r'```(?:python|alpha)?\s*(.*?)```',
    # Inline code
    r'`([^`]+)`',
    # Expression format: expression = "..."
    r'expression\s*[=:]\s*["\']([^"\']+)["\']',
    # Alpha format
    r'alpha\s*[=:]\s*["\']([^"\']+)["\']',
]

# Keywords indicating alpha-related content
ALPHA_KEYWORDS = [
    "sharpe", "fitness", "turnover", "alpha", "factor",
    "ts_rank", "ts_zscore", "ts_delta", "rank", "group_neutralize",
    "momentum", "reversal", "value", "quality", "sentiment",
    "backtest", "simulation", "delay", "decay", "neutralization",
]

# Operator patterns for validation
# Extended 2026-05-17 (Q1 v1.5 + Q3 v1.5): added power/signed_power/
# ts_covariance/ts_delay/ts_product/ts_scale/ts_arg_max/ts_arg_min/
# ts_regression (Q1); ts_quantile/ts_median/ts_skewness/ts_kurtosis/
# ts_rsquare/ts_decay_exp/ts_regression_residual + boolean/logical
# operators less/greater/less_equal/greater_equal/not_equal/and_op/or_op/
# not_op (Q3). Required for is_likely_alpha_expression sniff to accept
# Kakushadze 101 + Alpha158 patterns.
VALID_OPERATORS = {
    # Time-series rolling
    "ts_rank", "ts_zscore", "ts_mean", "ts_sum", "ts_delta", "ts_std_dev",
    "ts_decay_linear", "ts_decay_exp", "ts_corr", "ts_covariance",
    "ts_max", "ts_min", "ts_argmax", "ts_argmin", "ts_arg_max", "ts_arg_min",
    "ts_delay", "ts_product", "ts_scale", "ts_quantile", "ts_median",
    "ts_skewness", "ts_kurtosis", "ts_regression", "ts_regression_residual",
    "ts_rsquare",
    # Cross-sectional
    "rank", "zscore", "group_rank", "group_zscore", "group_neutralize",
    "vec_sum", "vec_avg", "vec_max", "vec_min",
    # Element-wise math
    "log", "sqrt", "abs", "sign", "power", "signed_power",
    "add", "subtract", "multiply", "divide", "max", "min",
    # Boolean / control flow
    "if_else", "trade_when", "pasteurize",
    "equal", "not_equal", "less", "greater", "less_equal", "greater_equal",
    "and_op", "or_op", "not_op",
}


# Phase 0 v1.5 SF-FC: operator extraction for permanent audit hook.
# Matches BRAIN function-call shape `name(` — pure CPU regex, used by
# import paths + backfill scripts to populate `meta_data["pattern_operators"]`.
# Future role-recategorization needs ZERO pattern text rewrite: one SQL UPDATE
# based on this list flips requires_role for any rows whose operators overlap
# a hardcoded CONSULTANT_ONLY_OPERATORS set (maintained when BRAIN forum or
# sync-diff baseline exposes the actual user-vs-consultant boundary).
_OPERATOR_CALL_RE = re.compile(r'\b([a-zA-Z_]\w*)\s*\(')


def parse_pattern_operators(pattern_text: str) -> List[str]:
    """Extract distinct operator names from a BRAIN alpha expression.

    Greps `name(` function-call shapes from the pattern text and returns
    sorted unique operator names. Datafield identifiers (close, volume,
    returns, …) are NOT matched because they don't have a trailing `(`.
    """
    if not pattern_text:
        return []
    return sorted(set(_OPERATOR_CALL_RE.findall(pattern_text)))


def extract_alpha_expressions(text: str) -> List[str]:
    """
    Extract potential alpha expressions from text.
    
    Args:
        text: Forum post or document text
    
    Returns:
        List of extracted expression strings
    """
    expressions = []
    
    for pattern in ALPHA_EXPRESSION_PATTERNS:
        matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
        for match in matches:
            # Clean up the match
            expr = match.strip()
            
            # Validate it looks like an alpha expression
            if is_likely_alpha_expression(expr):
                expressions.append(expr)
    
    # Also look for inline expressions (function calls)
    func_pattern = r'\b(ts_\w+|rank|group_\w+|vec_\w+)\s*\([^)]+\)'
    inline_matches = re.findall(func_pattern, text)
    
    # For inline, we need to extract the full expression
    for match in re.finditer(r'((?:ts_\w+|rank|group_\w+|vec_\w+)\s*\([^)]*(?:\([^)]*\)[^)]*)*\))', text):
        expr = match.group(1).strip()
        if len(expr) > 10 and is_likely_alpha_expression(expr):
            expressions.append(expr)
    
    # Deduplicate
    return list(dict.fromkeys(expressions))


def is_likely_alpha_expression(text: str) -> bool:
    """
    Check if text looks like an alpha expression.
    
    Args:
        text: Potential expression string
    
    Returns:
        True if likely an alpha expression
    """
    if not text or len(text) < 5:
        return False
    
    # Must contain at least one known operator
    has_operator = any(op in text.lower() for op in VALID_OPERATORS)
    if not has_operator:
        return False
    
    # Must have parentheses (function calls)
    if '(' not in text or ')' not in text:
        return False
    
    # Balanced parentheses
    if text.count('(') != text.count(')'):
        return False
    
    # Not too long (avoid code blocks)
    if len(text) > 500:
        return False
    
    # No obvious code artifacts
    code_artifacts = ['import ', 'def ', 'class ', 'return ', 'for ', 'while ']
    if any(artifact in text for artifact in code_artifacts):
        return False
    
    return True


def extract_insights(text: str, min_length: int = 50) -> List[str]:
    """
    Extract actionable insights from text.
    
    Args:
        text: Forum post or document text
        min_length: Minimum insight length
    
    Returns:
        List of insight strings
    """
    insights = []
    
    # Look for insight patterns
    insight_patterns = [
        r'(?:tip|hint|advice|recommendation|key\s+point)[:：]\s*([^\n.]+[.\n])',
        r'(?:I found|we discovered|the trick is|important to)[:：]?\s*([^\n.]+[.\n])',
        r'(?:To improve|For better|To get higher)[:：]?\s*([^\n.]+[.\n])',
        r'(?:Avoid|Don\'t use|Never)[:：]?\s*([^\n.]+[.\n])',
    ]
    
    for pattern in insight_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            insight = match.strip()
            if len(insight) >= min_length:
                insights.append(insight)
    
    return insights[:10]  # Limit to 10 insights


def calculate_relevance_score(post: ForumPost) -> float:
    """
    Calculate relevance score for a forum post.
    
    Higher score = more valuable for knowledge base.
    """
    score = 0.0
    
    # Engagement metrics
    score += min(post.likes / 50, 0.3)  # Max 0.3 from likes
    score += min(post.views / 1000, 0.2)  # Max 0.2 from views
    score += min(post.replies / 20, 0.1)  # Max 0.1 from replies
    
    # Content quality
    text = f"{post.title} {post.content}".lower()
    
    # Keyword presence
    keyword_count = sum(1 for kw in ALPHA_KEYWORDS if kw in text)
    score += min(keyword_count / 10, 0.2)  # Max 0.2 from keywords
    
    # Has extractable patterns
    if post.alpha_patterns:
        score += min(len(post.alpha_patterns) / 3, 0.2)  # Max 0.2 from patterns
    
    return min(score, 1.0)


# =============================================================================
# External Knowledge Syncer
# =============================================================================

class ExternalKnowledgeSyncer:
    """
    Syncs external knowledge from forums and other sources.
    
    Usage:
        syncer = ExternalKnowledgeSyncer(db, mcp_client)
        
        # Sync forum posts
        new_entries = await syncer.sync_forum_posts(search_terms=["high sharpe", "momentum"])
        
        # Import curated knowledge
        await syncer.import_curated_patterns(patterns)
    """
    
    def __init__(self, db: AsyncSession, mcp_client: Any = None):
        self.db = db
        self.mcp_client = mcp_client  # MCP client for BRAIN API
        
        # Cache of processed post IDs
        self.processed_posts: set = set()
        
    async def sync_forum_posts(
        self,
        search_terms: List[str] = None,
        max_posts: int = 50,
        min_likes: int = 5
    ) -> int:
        """
        Sync high-quality forum posts to knowledge base.
        
        Args:
            search_terms: Search terms for forum
            max_posts: Maximum posts to process
            min_likes: Minimum likes for consideration
        
        Returns:
            Number of new knowledge entries added
        """
        if not self.mcp_client:
            logger.warning("[ExternalKnowledge] No MCP client available for forum sync")
            return 0
        
        search_terms = search_terms or [
            "high sharpe tips",
            "alpha improvement",
            "momentum factor",
            "turnover reduction",
            "best practices",
        ]
        
        new_entries = 0
        
        for term in search_terms:
            try:
                # Search forum via MCP
                posts = await self._search_forum(term, max_posts // len(search_terms))
                
                for post in posts:
                    if post.likes < min_likes:
                        continue
                    
                    if post.post_id in self.processed_posts:
                        continue
                    
                    # Extract patterns and insights
                    post.alpha_patterns = extract_alpha_expressions(post.content)
                    post.insights = extract_insights(post.content)
                    post.relevance_score = calculate_relevance_score(post)
                    
                    # Add to knowledge base if high quality
                    if post.relevance_score >= 0.4:
                        added = await self._add_forum_knowledge(post)
                        new_entries += added
                    
                    self.processed_posts.add(post.post_id)
                    
            except Exception as e:
                logger.warning(f"[ExternalKnowledge] Forum search failed for '{term}': {e}")
        
        logger.info(f"[ExternalKnowledge] Forum sync complete | new_entries={new_entries}")
        return new_entries
    
    async def _search_forum(self, query: str, limit: int) -> List[ForumPost]:
        """Search forum via MCP client."""
        try:
            # Call MCP search_forum_posts tool
            result = await self.mcp_client.call_tool(
                server="user-worldquant-brain-platform",
                tool_name="search_forum_posts",
                arguments={"query": query, "limit": limit}
            )
            
            posts = []
            for item in result.get("posts", []):
                posts.append(ForumPost(
                    post_id=str(item.get("id", "")),
                    title=item.get("title", ""),
                    content=item.get("content", item.get("body", "")),
                    author=item.get("author", ""),
                    likes=item.get("likes", 0),
                    views=item.get("views", 0),
                    replies=item.get("replies", 0),
                ))
            
            return posts
            
        except Exception as e:
            logger.warning(f"[ExternalKnowledge] MCP forum search failed: {e}")
            return []
    
    async def _add_forum_knowledge(self, post: ForumPost) -> int:
        """Add forum post knowledge to database."""
        added = 0
        
        # Add alpha patterns
        for pattern in post.alpha_patterns[:5]:  # Limit to 5 per post
            exists = await self._pattern_exists(pattern)
            if not exists:
                entry = KnowledgeEntry(
                    entry_type='SUCCESS_PATTERN',
                    pattern=pattern,
                    description=f"From forum: {post.title[:100]}",
                    meta_data={
                        'source': 'forum',
                        'source_title': post.title,
                        'source_author': post.author,
                        'post_id': post.post_id,
                        'likes': post.likes,
                        'relevance_score': post.relevance_score,
                        'score': min(0.9, 0.5 + post.relevance_score * 0.4),
                        'verified': False,
                        'extracted_at': datetime.now().isoformat(),
                    },
                    usage_count=0,
                    is_active=True,
                    created_by='FORUM_SYNC'
                )
                self.db.add(entry)
                added += 1
        
        # Add insights as tips
        for insight in post.insights[:3]:  # Limit to 3 per post
            insight_hash = hash(insight) % 10000
            insight_pattern = f"INSIGHT:{insight_hash}:{insight[:50]}"
            
            exists = await self._pattern_exists(insight_pattern)
            if not exists:
                entry = KnowledgeEntry(
                    entry_type='SUCCESS_PATTERN',
                    pattern=insight_pattern,
                    description=insight,
                    meta_data={
                        'source': 'forum_insight',
                        'source_title': post.title,
                        'post_id': post.post_id,
                        'pattern_type': 'insight',
                        'score': 0.6,
                    },
                    usage_count=0,
                    is_active=True,
                    created_by='FORUM_SYNC'
                )
                self.db.add(entry)
                added += 1
        
        if added > 0:
            await self.db.commit()
        
        return added
    
    async def _pattern_exists(self, pattern: str) -> bool:
        """Check if pattern already exists in knowledge base (legacy API)."""
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.pattern == pattern,
            KnowledgeEntry.is_active == True
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none() is not None

    async def _pattern_hash_exists(self, pattern_hash: str) -> bool:
        """Check if pattern_hash already exists in knowledge base.

        v1.3 Q1 MF-8: dedupe via UNIQUE index ix_kb_pattern_hash. Required
        because the legacy `_pattern_exists(pattern)` does string-equality
        lookup which would re-import semantically identical rows whose
        existing pattern_hash column happens to be NULL (older import paths
        never set it — see backfill_kb_requires_role.py for the one-shot fix).
        """
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.pattern_hash == pattern_hash,
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none() is not None

    async def import_curated_patterns(
        self,
        patterns: List[ExternalKnowledge],
        *,
        batch_id: Optional[str] = None,
        requires_role: str = "both",
    ) -> int:
        """Import curated patterns from external sources.

        Args:
            patterns: List of ExternalKnowledge entries.
            batch_id: Phase 0 v1.3 SF-12 import batch marker. When provided,
                written to ``meta_data["import_batch"]`` for precise rollback
                via ``WHERE meta_data->>'import_batch' = :batch_id``. Use the
                ``PHASE0_*_IMPORT_BATCH`` constants at the top of this module.
            requires_role: Phase 0 v1.5 forward-compat marker. Defaults to
                ``"both"`` — write all rows as universally available. Future
                ``CONSULTANT_ONLY_OPERATORS``-aware audit can flip rows based
                on ``meta_data["pattern_operators"]`` without rewriting any
                alpha expression text.

        Returns:
            Number of patterns imported (existing pattern_hash rows are
            skipped via _pattern_hash_exists; the previously latent
            missing-hash bug is also fixed here — every new row carries
            pattern_hash, populated by compute_pattern_hash(text, None, None)).
        """
        imported = 0

        for ext in patterns:
            phash = compute_pattern_hash(ext.pattern, None, None)
            if await self._pattern_hash_exists(phash):
                continue

            meta_data: Dict[str, Any] = {
                'source': ext.source,
                'source_url': ext.source_url,
                'source_title': ext.source_title,
                'dataset_category': ext.category,
                'confidence': ext.confidence,
                'verified': ext.verified,
                'score': ext.confidence,
                'extracted_at': ext.extraction_date.isoformat(),
                # v1.5 SF-FC forward-compat fields
                'requires_role': requires_role,
                'pattern_operators': parse_pattern_operators(ext.pattern),
            }
            if batch_id is not None:
                # v1.3 SF-12: precise rollback marker, optional for
                # backward compat with legacy callers that don't tag batches
                meta_data['import_batch'] = batch_id
            # Q3 Alpha158 optional fields — only written when set on ext.
            # raw_feature is a bool, so check explicitly against None.
            if ext.raw_feature is not None:
                meta_data['raw_feature'] = ext.raw_feature
            if ext.qlib_origin is not None:
                meta_data['qlib_origin'] = ext.qlib_origin
            # Phase 1 Q2 (2026-05-17) — dual-path openassetpricing fields
            if ext.is_anchor_metadata is not None:
                meta_data['is_anchor_metadata'] = ext.is_anchor_metadata
            if ext.openassetpricing_signal is not None:
                meta_data['openassetpricing_signal'] = ext.openassetpricing_signal
            if ext.llm_translation_version is not None:
                meta_data['llm_translation_version'] = ext.llm_translation_version
            if ext.translation_confidence is not None:
                meta_data['translation_confidence'] = ext.translation_confidence
            if ext.translation_notes is not None:
                meta_data['translation_notes'] = ext.translation_notes
            if ext.theoretical_anchor is not None:
                meta_data['theoretical_anchor'] = ext.theoretical_anchor
            if ext.paper_citation is not None:
                meta_data['paper_citation'] = ext.paper_citation

            # Q2 dual-path: ANCHOR_METADATA entries are plain-English signal
            # documentation, NOT BRAIN-DSL — RAG SQL must filter them out.
            # All other entries default to SUCCESS_PATTERN (legacy behavior).
            row_entry_type = (
                'ANCHOR_METADATA' if ext.is_anchor_metadata is True
                else 'SUCCESS_PATTERN'
            )

            entry = KnowledgeEntry(
                entry_type=row_entry_type,
                pattern=ext.pattern,
                pattern_hash=phash,  # MF-8 fix: was silently NULL prior to v1.3
                description=ext.description,
                meta_data=meta_data,
                usage_count=0,
                is_active=True,
                created_by='CURATED_IMPORT'
            )
            self.db.add(entry)
            imported += 1

        if imported > 0:
            await self.db.commit()

        logger.info(f"[ExternalKnowledge] Imported {imported} curated patterns"
                    + (f" (batch={batch_id})" if batch_id else ""))
        return imported
    
    async def get_sync_stats(self) -> Dict[str, Any]:
        """Get statistics about external knowledge."""
        query = select(
            KnowledgeEntry.created_by,
            func.count(KnowledgeEntry.id).label('count')
        ).where(
            KnowledgeEntry.is_active == True
        ).group_by(KnowledgeEntry.created_by)
        
        result = await self.db.execute(query)
        by_source = dict(result.fetchall())
        
        return {
            "by_source": by_source,
            "processed_posts": len(self.processed_posts),
            "total_external": sum(
                v for k, v in by_source.items()
                if k in ['FORUM_SYNC', 'CURATED_IMPORT']
            ),
        }


# =============================================================================
# Pre-defined External Knowledge
# =============================================================================

# Base curated patterns hand-written in this module (5 rows, Phase 0 pre-Q1).
# Full ACADEMIC_PATTERNS list below extends this with eager-merged JSON files
# (Q1 alpha101_kakushadze.json + Q3 alpha158_qlib.json) when those files
# land — see _load_external_patterns_json + ACADEMIC_PATTERNS assignment.
_BASE_ACADEMIC_PATTERNS: List[ExternalKnowledge] = [
    ExternalKnowledge(
        source="paper",
        pattern="rank(ts_argmax(signed_power(if_else(returns > 0, ts_std_dev(returns, 20), close), 2), 5))",
        description="Alpha#1 from 101 Formulaic Alphas - Volatility clustering momentum",
        category="pv",
        confidence=0.9,
        verified=True,
        source_title="101 Formulaic Alphas (Kakushadze, 2016)",
    ),
    ExternalKnowledge(
        source="paper",
        pattern="-1 * ts_corr(rank(ts_delta(log(volume), 2)), rank(divide(close - open, open)), 6)",
        description="Alpha#2 from 101 Formulaic Alphas - Volume-price divergence",
        category="pv",
        confidence=0.9,
        verified=True,
        source_title="101 Formulaic Alphas (Kakushadze, 2016)",
    ),
    ExternalKnowledge(
        source="paper",
        pattern="rank(subtract(open, divide(ts_sum(vwap, 10), 10))) * -1 * abs(rank(subtract(close, vwap)))",
        description="Alpha#5 from 101 Formulaic Alphas - VWAP deviation momentum",
        category="pv",
        confidence=0.9,
        verified=True,
        source_title="101 Formulaic Alphas (Kakushadze, 2016)",
    ),
    ExternalKnowledge(
        source="paper",
        pattern="ts_decay_linear(rank(ts_corr(close, volume, 10)), 5)",
        description="Price-volume correlation with decay for stability",
        category="pv",
        confidence=0.8,
        verified=True,
        source_title="Alpha-GPT Best Practices",
    ),
    ExternalKnowledge(
        source="paper",
        pattern="group_neutralize(ts_rank(ts_delta(eps_est, 20), 10), sector)",
        description="Earnings estimate revision momentum with sector neutralization",
        category="analyst",
        confidence=0.85,
        verified=True,
        source_title="Quantitative Factor Research",
    ),
]


def _load_external_patterns_json(filename: str) -> List[ExternalKnowledge]:
    """Load curated patterns from a JSON file under backend/data/.

    Returns [] if the file is absent so module import doesn't fail before
    Q1/Q3 JSON files are committed. v1.5 SF-7 fix: module-level eager merge
    (called once at import time, not lazy property) — `from backend
    .external_knowledge import ACADEMIC_PATTERNS` always observes the full
    merged list.
    """
    path = Path(__file__).parent / "data" / filename
    if not path.exists():
        logger.debug(f"[ExternalKnowledge] {filename} not yet committed; merging []")
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError) as ex:
        logger.warning(f"[ExternalKnowledge] failed to load {filename}: {ex}; merging []")
        return []

    out: List[ExternalKnowledge] = []
    for item in raw:
        try:
            out.append(ExternalKnowledge(
                source=item.get("source", "paper"),
                pattern=item["pattern"],
                description=item.get("description", ""),
                category=item.get("category", "pv"),
                confidence=item.get("confidence", 0.85),
                verified=item.get("verified", True),
                source_title=item.get("source_title", ""),
                source_url=item.get("source_url", ""),
                # Q3 Alpha158 optional fields (None for other sources)
                raw_feature=item.get("raw_feature"),
                qlib_origin=item.get("qlib_origin"),
                # Q2 openassetpricing dual-path fields (None for other sources)
                is_anchor_metadata=item.get("is_anchor_metadata"),
                openassetpricing_signal=item.get("openassetpricing_signal"),
                llm_translation_version=item.get("llm_translation_version"),
                translation_confidence=item.get("translation_confidence"),
                translation_notes=item.get("translation_notes"),
                theoretical_anchor=item.get("theoretical_anchor"),
                paper_citation=item.get("paper_citation"),
            ))
        except KeyError as ex:
            logger.warning(f"[ExternalKnowledge] {filename} item missing key {ex}; skipping")
            continue
    return out


async def import_openassetpricing_knowledge(db: AsyncSession) -> int:
    """Import openassetpricing translations (Phase 1 Q2, 2026-05-17).

    Reads ``backend/data/openassetpricing_translations.json`` (produced by
    ``scripts/translate_openassetpricing.py`` on the openassetpricing
    Predictors directory). Dual-path:

    - Entries with ``is_anchor_metadata=False`` (or unset) and a non-empty
      BRAIN-DSL ``pattern`` → SUCCESS_PATTERN row.
    - Entries with ``is_anchor_metadata=True`` → ANCHOR_METADATA row;
      ``pattern`` field carries the plain-English signal description.

    Every row gets ``meta_data["import_batch"] = PHASE1_Q2_IMPORT_BATCH``
    for precise rollback (UPDATE knowledge_entries SET is_active=false WHERE
    meta_data->>'import_batch' = :batch).
    """
    patterns = _load_external_patterns_json("openassetpricing_translations.json")
    if not patterns:
        logger.warning(
            "[Q2] openassetpricing_translations.json missing or empty; "
            "run scripts/translate_openassetpricing.py first"
        )
        return 0
    syncer = ExternalKnowledgeSyncer(db)
    return await syncer.import_curated_patterns(
        patterns, batch_id=PHASE1_Q2_IMPORT_BATCH
    )


# Phase 0 eager merge — SF-7 v1.2: module-level, not lazy, so
# `len(ACADEMIC_PATTERNS)` GO gate verify (plan §7) sees the full list
# immediately after `from backend.external_knowledge import ACADEMIC_PATTERNS`.
ACADEMIC_PATTERNS: List[ExternalKnowledge] = (
    _BASE_ACADEMIC_PATTERNS
    + _load_external_patterns_json("alpha101_kakushadze.json")
    + _load_external_patterns_json("alpha158_qlib.json")
)


async def import_academic_knowledge(db: AsyncSession) -> int:
    """Import pre-defined academic patterns."""
    syncer = ExternalKnowledgeSyncer(db)
    return await syncer.import_curated_patterns(ACADEMIC_PATTERNS)


async def import_alpha158_knowledge(db: AsyncSession) -> int:
    """Import Qlib Alpha158 patterns (Phase 0 Q3, v1.5 single-version).

    Reads backend/data/alpha158_qlib.json (produced by qlib_translator on
    alpha158_qlib_raw.json), tags every row with PHASE0_Q3_IMPORT_BATCH +
    requires_role='both' + raw_feature flag (passed through from JSON) +
    qlib_origin (preserved for future audit / wrapper injection).
    """
    patterns = _load_external_patterns_json("alpha158_qlib.json")
    if not patterns:
        logger.warning("[Alpha158] alpha158_qlib.json missing or empty; nothing to import")
        return 0
    syncer = ExternalKnowledgeSyncer(db)
    return await syncer.import_curated_patterns(
        patterns, batch_id=PHASE0_Q3_IMPORT_BATCH
    )


# =============================================================================
# Scheduled Sync Job
# =============================================================================

async def run_scheduled_sync(
    db: AsyncSession,
    mcp_client: Any = None,
    force: bool = False
) -> Dict[str, Any]:
    """
    Run scheduled knowledge sync.
    
    Should be called periodically (e.g., daily) to update knowledge base.
    
    Args:
        db: Database session
        mcp_client: MCP client for BRAIN API
        force: Force sync even if recently synced
    
    Returns:
        Sync statistics
    """
    results = {
        "timestamp": datetime.now().isoformat(),
        "forum_entries": 0,
        "academic_entries": 0,
        "errors": [],
    }
    
    # Check last sync time (if not force)
    if not force:
        query = select(KnowledgeEntry).where(
            KnowledgeEntry.created_by == 'FORUM_SYNC'
        ).order_by(KnowledgeEntry.created_at.desc()).limit(1)
        
        result = await db.execute(query)
        last_sync = result.scalar_one_or_none()
        
        if last_sync and (datetime.now() - last_sync.created_at) < timedelta(hours=24):
            logger.info("[ExternalKnowledge] Skipping sync - synced within 24h")
            results["skipped"] = True
            return results
    
    try:
        # Import academic knowledge (idempotent)
        academic = await import_academic_knowledge(db)
        results["academic_entries"] = academic
    except Exception as e:
        logger.error(f"[ExternalKnowledge] Academic import failed: {e}")
        results["errors"].append(f"Academic: {str(e)}")
    
    # Forum sync (if MCP available)
    if mcp_client:
        try:
            syncer = ExternalKnowledgeSyncer(db, mcp_client)
            forum = await syncer.sync_forum_posts()
            results["forum_entries"] = forum
        except Exception as e:
            logger.error(f"[ExternalKnowledge] Forum sync failed: {e}")
            results["errors"].append(f"Forum: {str(e)}")
    
    logger.info(
        f"[ExternalKnowledge] Scheduled sync complete | "
        f"academic={results['academic_entries']} forum={results['forum_entries']}"
    )
    
    return results
