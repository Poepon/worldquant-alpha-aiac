"""
Knowledge Service - Business logic for knowledge base management

Provides methods for:
- Knowledge entry CRUD
- Pattern retrieval (success patterns, failure pitfalls)
- Field blacklist management
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass, field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from backend.services.base import BaseService
from backend.repositories.knowledge_repository import KnowledgeRepository
from backend.models import KnowledgeEntry

logger = logging.getLogger("services.knowledge")


@dataclass
class KnowledgeEntryInfo:
    """Knowledge entry information for responses."""
    id: int
    entry_type: str
    pattern: Optional[str]
    description: Optional[str]
    meta_data: Dict[str, Any]
    usage_count: int
    is_active: bool
    created_by: str
    created_at: datetime
    updated_at: Optional[datetime]
    factor_tier: Optional[int] = None  # PR3: surface tier from KB row


@dataclass
class KnowledgeCreateData:
    """Data for creating a knowledge entry."""
    entry_type: str
    pattern: Optional[str] = None
    description: Optional[str] = None
    meta_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeUpdateData:
    """Data for updating a knowledge entry."""
    pattern: Optional[str] = None
    description: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


@dataclass
class KnowledgeListFilters:
    """Filters for listing knowledge entries."""
    entry_type: Optional[str] = None
    is_active: Optional[bool] = None
    factor_tier: Optional[int] = None  # PR2/PR3: tier-aware filter (1/2/3)
    region: Optional[str] = None       # PR3: filter via meta_data->>'region'
    created_by: Optional[str] = None   # PR3: SYSTEM / HITL / USER
    limit: int = 50
    offset: int = 0


class KnowledgeService(BaseService):
    """
    Service for knowledge base operations.
    
    Provides a clean interface for knowledge management,
    abstracting database operations from routers.
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self.knowledge_repo = KnowledgeRepository(db)
    
    # =========================================================================
    # List Operations
    # =========================================================================
    
    async def list_entries(
        self,
        filters: KnowledgeListFilters,
    ) -> List[KnowledgeEntryInfo]:
        """
        List knowledge entries with optional filtering.
        
        Args:
            filters: List filters
            
        Returns:
            List of KnowledgeEntryInfo
        """
        query = select(KnowledgeEntry).order_by(KnowledgeEntry.usage_count.desc())

        if filters.entry_type:
            query = query.where(KnowledgeEntry.entry_type == filters.entry_type)
        if filters.is_active is not None:
            query = query.where(KnowledgeEntry.is_active == filters.is_active)
        # PR3 — tier filter. Special value 0 means "explicitly NULL" (entries
        # not in the tier hierarchy, e.g. multi-field arithmetic patterns).
        if filters.factor_tier is not None:
            if filters.factor_tier == 0:
                query = query.where(KnowledgeEntry.factor_tier.is_(None))
            else:
                query = query.where(KnowledgeEntry.factor_tier == filters.factor_tier)
        if filters.created_by:
            query = query.where(KnowledgeEntry.created_by == filters.created_by)
        if filters.region:
            # meta_data->>'region' is a JSONB text accessor; works on Postgres.
            query = query.where(
                KnowledgeEntry.meta_data["region"].astext == filters.region
            )

        query = query.limit(filters.limit).offset(filters.offset)
        
        result = await self.db.execute(query)
        entries = result.scalars().all()
        
        return [self._to_entry_info(e) for e in entries]
    
    def _to_entry_info(self, e: KnowledgeEntry) -> KnowledgeEntryInfo:
        """Convert KnowledgeEntry to KnowledgeEntryInfo."""
        return KnowledgeEntryInfo(
            id=e.id,
            entry_type=e.entry_type,
            pattern=e.pattern,
            description=e.description,
            meta_data=e.meta_data or {},
            factor_tier=e.factor_tier,
            usage_count=e.usage_count,
            is_active=e.is_active,
            created_by=e.created_by,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
    
    # =========================================================================
    # Specialized Queries
    # =========================================================================
    
    async def get_success_patterns(self, limit: int = 20) -> List[KnowledgeEntryInfo]:
        """
        Get successful alpha patterns for RAG retrieval.
        
        Args:
            limit: Maximum results
            
        Returns:
            List of success pattern entries
        """
        query = (
            select(KnowledgeEntry)
            .where(
                KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
                KnowledgeEntry.is_active == True,
            )
            .order_by(KnowledgeEntry.usage_count.desc())
            .limit(limit)
        )
        
        result = await self.db.execute(query)
        entries = result.scalars().all()
        
        return [self._to_entry_info(e) for e in entries]
    
    async def get_failure_pitfalls(self, limit: int = 50) -> List[KnowledgeEntryInfo]:
        """
        Get failure pitfalls for the feedback loop.
        
        Args:
            limit: Maximum results
            
        Returns:
            List of failure pitfall entries
        """
        query = (
            select(KnowledgeEntry)
            .where(
                KnowledgeEntry.entry_type == "FAILURE_PITFALL",
                KnowledgeEntry.is_active == True,
            )
            .order_by(KnowledgeEntry.created_at.desc())
            .limit(limit)
        )
        
        result = await self.db.execute(query)
        entries = result.scalars().all()
        
        return [self._to_entry_info(e) for e in entries]
    
    async def get_field_blacklist(
        self,
        region: Optional[str] = None,
    ) -> List[KnowledgeEntryInfo]:
        """
        Get blacklisted fields.
        
        Args:
            region: Optional region filter
            
        Returns:
            List of blacklisted field entries
        """
        query = (
            select(KnowledgeEntry)
            .where(
                KnowledgeEntry.entry_type == "FIELD_BLACKLIST",
                KnowledgeEntry.is_active == True,
            )
        )
        
        result = await self.db.execute(query)
        entries = result.scalars().all()
        
        # Filter by region if specified (in-memory filter for meta_data)
        if region:
            entries = [
                e for e in entries 
                if e.meta_data and e.meta_data.get("region") == region
            ]
        
        return [self._to_entry_info(e) for e in entries]
    
    # =========================================================================
    # CRUD Operations
    # =========================================================================
    
    async def create_entry(
        self,
        data: KnowledgeCreateData,
    ) -> KnowledgeEntryInfo:
        """
        Create a new knowledge entry.
        
        Args:
            data: Entry creation data
            
        Returns:
            Created KnowledgeEntryInfo
        """
        entry = KnowledgeEntry(
            entry_type=data.entry_type,
            pattern=data.pattern,
            description=data.description,
            meta_data=data.meta_data,
            created_by="USER",
        )
        
        created = await self.knowledge_repo.create(entry)
        await self.commit()
        
        return self._to_entry_info(created)
    
    async def get_entry(self, entry_id: int) -> Optional[KnowledgeEntryInfo]:
        """
        Get a knowledge entry by ID.
        
        Args:
            entry_id: Entry ID
            
        Returns:
            KnowledgeEntryInfo or None
        """
        entry = await self.knowledge_repo.get_by_id(entry_id)
        if not entry:
            return None
        return self._to_entry_info(entry)
    
    async def update_entry(
        self,
        entry_id: int,
        data: KnowledgeUpdateData,
    ) -> KnowledgeEntryInfo:
        """
        Update a knowledge entry.
        
        Args:
            entry_id: Entry ID
            data: Update data
            
        Returns:
            Updated KnowledgeEntryInfo
            
        Raises:
            ValueError if entry not found
        """
        entry = await self.knowledge_repo.get_by_id(entry_id)
        if not entry:
            raise ValueError(f"Knowledge entry {entry_id} not found")
        
        update_data = {}
        if data.pattern is not None:
            update_data["pattern"] = data.pattern
        if data.description is not None:
            update_data["description"] = data.description
        if data.meta_data is not None:
            update_data["meta_data"] = data.meta_data
        if data.is_active is not None:
            update_data["is_active"] = data.is_active
        
        if update_data:
            await self.knowledge_repo.update_by_id(entry_id, update_data)
            await self.commit()
            # Refresh entry
            entry = await self.knowledge_repo.get_by_id(entry_id)
        
        return self._to_entry_info(entry)
    
    async def delete_entry(self, entry_id: int) -> bool:
        """
        Soft delete a knowledge entry (deactivate).
        
        Args:
            entry_id: Entry ID
            
        Returns:
            True if deleted
            
        Raises:
            ValueError if entry not found
        """
        entry = await self.knowledge_repo.get_by_id(entry_id)
        if not entry:
            raise ValueError(f"Knowledge entry {entry_id} not found")
        
        await self.knowledge_repo.update_by_id(entry_id, {"is_active": False})
        await self.commit()
        
        return True
