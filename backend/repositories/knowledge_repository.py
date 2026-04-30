"""
Knowledge Repository - Data access for KnowledgeEntry entities

Provides specialized queries for the knowledge base, including
pattern retrieval, scoring updates, and category management.
"""

import logging
from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, and_, or_
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert

from backend.repositories.base_repository import BaseRepository
from backend.protocols.repository_protocol import PaginationParams, PaginatedResult
from backend.models import KnowledgeEntry, KnowledgeEntryType
from backend.models.knowledge import compute_pattern_hash

logger = logging.getLogger("repositories.knowledge")


class KnowledgeRepository(BaseRepository[KnowledgeEntry]):
    """
    Repository for KnowledgeEntry entity with specialized queries.
    
    Provides methods for:
    - Pattern retrieval by category
    - Score updates based on usage
    - Knowledge base maintenance
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db, KnowledgeEntry)

    # =========================================================================
    # W3: Idempotent upsert via pattern_hash UNIQUE
    # =========================================================================

    async def find_by_pattern_text(
        self,
        pattern_text: str,
        region: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ) -> Optional[KnowledgeEntry]:
        """Lookup by frozen pattern_hash (sha256(pattern|region|dataset_id)[:32])."""
        ph = compute_pattern_hash(pattern_text, region, dataset_id)
        stmt = select(KnowledgeEntry).where(KnowledgeEntry.pattern_hash == ph)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_pattern(
        self,
        entry_type: str,
        pattern_text: str,
        description: Optional[str] = None,
        meta_data: Optional[Dict[str, Any]] = None,
        region: Optional[str] = None,
        dataset_id: Optional[str] = None,
        created_by: str = "SYSTEM",
    ) -> KnowledgeEntry:
        """Atomic INSERT ... ON CONFLICT (pattern_hash) DO UPDATE.

        On insert: usage_count=1.
        On conflict (same pattern + region + dataset_id):
          - usage_count += 1
          - is_active forced True (re-enable any disambiguated dups)
          - meta_data fully replaced by `meta_data` arg (caller is
            responsible for read-modify-write of confidence/score —
            see feedback_agent for the bump policy)
          - description coalesced (keeps existing if no new value)

        TOCTOU-safe replacement for the ``_pattern_exists() + create()``
        dance in feedback_agent; multiple concurrent writers can call
        this without producing duplicate rows.
        """
        meta = dict(meta_data or {})
        if region and "region" not in meta:
            meta["region"] = region
        if dataset_id and "dataset_id" not in meta:
            meta["dataset_id"] = dataset_id
        if dataset_id and "dataset" not in meta:
            meta["dataset"] = dataset_id

        ph = compute_pattern_hash(pattern_text, region, dataset_id)

        stmt = pg_insert(KnowledgeEntry).values(
            entry_type=entry_type,
            pattern=pattern_text,
            description=description,
            meta_data=meta,
            pattern_hash=ph,
            usage_count=1,
            is_active=True,
            created_by=created_by,
        )

        excluded = stmt.excluded
        update_set = {
            "usage_count": KnowledgeEntry.usage_count + 1,
            "is_active": True,
            "description": func.coalesce(excluded.description, KnowledgeEntry.description),
            "meta_data": excluded.meta_data,
        }

        stmt = stmt.on_conflict_do_update(
            index_elements=["pattern_hash"],
            set_=update_set,
        ).returning(KnowledgeEntry)

        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.scalar_one()

    # =========================================================================
    # Category-Based Queries
    # =========================================================================
    
    async def get_by_category(
        self,
        category: str,
        min_usage_count: Optional[int] = None,
        only_active: bool = True,
    ) -> List[KnowledgeEntry]:
        """
        Get knowledge entries by category from meta_data.
        
        Args:
            category: The category to filter by
            min_usage_count: Minimum usage count filter
            only_active: Whether to filter for active entries only
            
        Returns:
            List of matching entries
        """
        query = select(KnowledgeEntry)
        
        # Filter by category in meta_data JSONB
        query = query.where(
            KnowledgeEntry.meta_data["category"].astext == category
        )
        
        if only_active:
            query = query.where(KnowledgeEntry.is_active == True)
        
        if min_usage_count is not None:
            query = query.where(KnowledgeEntry.usage_count >= min_usage_count)
        
        query = query.order_by(KnowledgeEntry.usage_count.desc())
        
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    async def get_by_entry_type(
        self,
        entry_type: str,
        only_active: bool = True,
        limit: int = 100,
    ) -> List[KnowledgeEntry]:
        """
        Get knowledge entries by type.
        
        Args:
            entry_type: The entry type (SUCCESS_PATTERN, FAILURE_PITFALL, etc.)
            only_active: Whether to filter for active entries only
            limit: Maximum number of results
            
        Returns:
            List of matching entries
        """
        query = select(KnowledgeEntry).where(KnowledgeEntry.entry_type == entry_type)
        
        if only_active:
            query = query.where(KnowledgeEntry.is_active == True)
        
        query = query.order_by(KnowledgeEntry.usage_count.desc())
        query = query.limit(limit)
        
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    # =========================================================================
    # Pattern Retrieval
    # =========================================================================
    
    async def get_top_patterns(
        self,
        limit: int = 10,
        category: Optional[str] = None,
        entry_type: Optional[str] = None,
    ) -> List[KnowledgeEntry]:
        """
        Get top-performing patterns.
        
        Args:
            limit: Maximum number of results
            category: Optional category filter
            entry_type: Optional entry type filter
            
        Returns:
            List of top patterns ordered by usage count
        """
        query = select(KnowledgeEntry).where(KnowledgeEntry.is_active == True)
        
        if category is not None:
            query = query.where(
                KnowledgeEntry.meta_data["category"].astext == category
            )
        
        if entry_type is not None:
            query = query.where(KnowledgeEntry.entry_type == entry_type)
        
        query = query.order_by(KnowledgeEntry.usage_count.desc())
        query = query.limit(limit)
        
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    async def get_success_patterns(
        self,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[KnowledgeEntry]:
        """
        Get successful alpha patterns.
        
        Args:
            category: Optional category filter
            limit: Maximum number of results
            
        Returns:
            List of success patterns
        """
        return await self.get_by_entry_type(
            entry_type="SUCCESS_PATTERN",
            only_active=True,
            limit=limit,
        )
    
    async def get_failure_pitfalls(
        self,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> List[KnowledgeEntry]:
        """
        Get failure pitfall patterns to avoid.
        
        Args:
            category: Optional category filter
            limit: Maximum number of results
            
        Returns:
            List of failure patterns
        """
        return await self.get_by_entry_type(
            entry_type="FAILURE_PITFALL",
            only_active=True,
            limit=limit,
        )
    
    async def search_patterns(
        self,
        search_term: str,
        limit: int = 20,
    ) -> List[KnowledgeEntry]:
        """
        Search patterns by text in pattern or description.
        
        Args:
            search_term: Text to search for
            limit: Maximum number of results
            
        Returns:
            List of matching entries
        """
        # Using ILIKE for case-insensitive search
        search_pattern = f"%{search_term}%"
        
        query = (
            select(KnowledgeEntry)
            .where(
                and_(
                    KnowledgeEntry.is_active == True,
                    or_(
                        KnowledgeEntry.pattern.ilike(search_pattern),
                        KnowledgeEntry.description.ilike(search_pattern),
                    )
                )
            )
            .order_by(KnowledgeEntry.usage_count.desc())
            .limit(limit)
        )
        
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    # =========================================================================
    # Score Updates
    # =========================================================================
    
    async def update_score(
        self,
        entry_id: int,
        success: bool,
        increment: int = 1,
    ) -> bool:
        """
        Update pattern score based on usage result.
        
        Args:
            entry_id: The entry ID
            success: Whether the usage was successful
            increment: Amount to increment usage_count
            
        Returns:
            True if updated, False if not found
        """
        # Always increment usage count
        stmt = (
            update(KnowledgeEntry)
            .where(KnowledgeEntry.id == entry_id)
            .values(usage_count=KnowledgeEntry.usage_count + increment)
        )
        result = await self.db.execute(stmt)
        return result.rowcount > 0
    
    async def increment_usage(self, entry_id: int) -> bool:
        """
        Increment usage count for an entry.
        
        Args:
            entry_id: The entry ID
            
        Returns:
            True if updated, False if not found
        """
        return await self.update_score(entry_id, success=True, increment=1)
    
    async def bulk_increment_usage(self, entry_ids: List[int]) -> int:
        """
        Increment usage count for multiple entries.
        
        Args:
            entry_ids: List of entry IDs
            
        Returns:
            Number of entries updated
        """
        if not entry_ids:
            return 0
        
        stmt = (
            update(KnowledgeEntry)
            .where(KnowledgeEntry.id.in_(entry_ids))
            .values(usage_count=KnowledgeEntry.usage_count + 1)
        )
        result = await self.db.execute(stmt)
        return result.rowcount
    
    # =========================================================================
    # Maintenance
    # =========================================================================
    
    async def deactivate_entry(self, entry_id: int) -> bool:
        """
        Deactivate a knowledge entry.
        
        Args:
            entry_id: The entry ID
            
        Returns:
            True if updated, False if not found
        """
        return await self.update_by_id(entry_id, {"is_active": False})
    
    async def activate_entry(self, entry_id: int) -> bool:
        """
        Activate a knowledge entry.
        
        Args:
            entry_id: The entry ID
            
        Returns:
            True if updated, False if not found
        """
        return await self.update_by_id(entry_id, {"is_active": True})
    
    async def prune_low_usage(self, min_usage: int = 0, deactivate: bool = True) -> int:
        """
        Prune or deactivate entries with low usage.
        
        Args:
            min_usage: Minimum usage threshold
            deactivate: If True, deactivate; if False, delete
            
        Returns:
            Number of entries affected
        """
        if deactivate:
            stmt = (
                update(KnowledgeEntry)
                .where(KnowledgeEntry.usage_count <= min_usage)
                .values(is_active=False)
            )
        else:
            from sqlalchemy import delete
            stmt = delete(KnowledgeEntry).where(KnowledgeEntry.usage_count <= min_usage)
        
        result = await self.db.execute(stmt)
        return result.rowcount
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    async def get_type_distribution(self) -> Dict[str, int]:
        """
        Get distribution of entries by type.
        
        Returns:
            Dict of entry_type -> count
        """
        query = select(
            KnowledgeEntry.entry_type,
            func.count(KnowledgeEntry.id).label("count")
        ).where(
            KnowledgeEntry.is_active == True
        ).group_by(KnowledgeEntry.entry_type)
        
        result = await self.db.execute(query)
        return {row.entry_type: row.count for row in result.all()}
    
    async def get_stats(self) -> Dict[str, Any]:
        """
        Get overall knowledge base statistics.
        
        Returns:
            Dict with total count, active count, type distribution
        """
        total = await self.count()
        active_count = await self.count_by({"is_active": True})
        type_dist = await self.get_type_distribution()
        
        # Get total and average usage
        usage_query = select(
            func.sum(KnowledgeEntry.usage_count).label("total_usage"),
            func.avg(KnowledgeEntry.usage_count).label("avg_usage"),
        ).where(KnowledgeEntry.is_active == True)
        
        usage_result = await self.db.execute(usage_query)
        usage = usage_result.one_or_none()
        
        return {
            "total_entries": total,
            "active_entries": active_count,
            "inactive_entries": total - active_count,
            "type_distribution": type_dist,
            "total_usage": int(usage.total_usage) if usage and usage.total_usage else 0,
            "avg_usage": float(usage.avg_usage) if usage and usage.avg_usage else 0.0,
        }
