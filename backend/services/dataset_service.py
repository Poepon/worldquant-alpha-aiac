"""
Dataset Service - Business logic for dataset management

Provides methods for:
- Dataset listing with filters
- Dataset field queries
- Dataset sync operations
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass, field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, and_

from backend.services.base import BaseService
from backend.models import (
    DatasetMetadata, DatasetCellStats, DataField, DataFieldCellStats,
)

logger = logging.getLogger("services.dataset")


@dataclass
class DatasetInfo:
    """Dataset information for responses."""
    dataset_id: str
    name: Optional[str]
    region: str
    universe: str
    category: Optional[str]
    subcategory: Optional[str]
    description: Optional[str]
    field_count: int
    alpha_success_count: int
    alpha_fail_count: int
    mining_weight: float
    last_synced_at: Optional[datetime]
    # Extended fields
    date_coverage: Optional[float] = None
    themes: Optional[List[Any]] = None
    resources: Optional[List[Any]] = None
    value_score: Optional[int] = None
    alpha_count: Optional[int] = None
    pyramid_multiplier: Optional[float] = None
    coverage: Optional[float] = None


@dataclass
class DataFieldInfo:
    """
    Data field information for responses.
    
    Matches real BRAIN API structure from get_datafields.
    """
    field_id: str
    field_name: str
    description: Optional[str]
    dataset_id: Optional[str]
    region: str
    universe: str
    delay: int
    is_active: bool
    # Extended fields from API
    field_type: Optional[str] = None  # MATRIX, VECTOR, GROUP
    date_coverage: Optional[float] = None
    coverage: Optional[float] = None
    pyramid_multiplier: Optional[float] = None
    user_count: Optional[int] = None
    alpha_count: Optional[int] = None
    # Category info (API returns nested objects)
    category: Optional[str] = None
    category_name: Optional[str] = None
    subcategory: Optional[str] = None
    subcategory_name: Optional[str] = None
    themes: Optional[List[Any]] = None


@dataclass
class PaginatedResult:
    """Paginated result container."""
    total: int
    items: List[Any] = field(default_factory=list)


@dataclass
class DatasetListFilters:
    """Filters for listing datasets."""
    region: Optional[str] = None
    category: Optional[str] = None
    search: Optional[str] = None
    limit: int = 50
    offset: int = 0
    # Which (universe, delay) cell's stats to surface for each dataset def.
    # Defaults to the canonical TOP3000/delay=1 cell (cell-stats normalization).
    universe: str = "TOP3000"
    delay: int = 1


class DatasetService(BaseService):
    """
    Service for dataset-related operations.
    
    Provides a clean interface for dataset management,
    abstracting database operations from routers.
    """
    
    # =========================================================================
    # List Operations
    # =========================================================================
    
    async def list_datasets(
        self,
        filters: DatasetListFilters,
    ) -> PaginatedResult:
        """
        List datasets with optional filtering.
        
        Args:
            filters: Dataset list filters
            
        Returns:
            PaginatedResult with DatasetInfo items
        """
        # Build base query — cell-stats normalization: a dataset def joins its
        # per-(universe, delay) cell for the display stats. LEFT JOIN so a def
        # without a cell for the requested universe still lists (with None stats).
        cell_on = and_(
            DatasetCellStats.dataset_ref == DatasetMetadata.id,
            DatasetCellStats.universe == filters.universe,
            DatasetCellStats.delay == filters.delay,
        )
        query = (
            select(DatasetMetadata, DatasetCellStats)
            .select_from(DatasetMetadata)
            .outerjoin(DatasetCellStats, cell_on)
        )

        if filters.region:
            query = query.where(DatasetMetadata.region == filters.region)

        if filters.category:
            query = query.where(DatasetMetadata.category == filters.category)

        if filters.search:
            search_term = f"%{filters.search}%"
            query = query.where(or_(
                DatasetMetadata.dataset_id.ilike(search_term),
                DatasetMetadata.description.ilike(search_term)
            ))

        # Get total count
        count_stmt = select(func.count()).select_from(query.subquery())
        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar_one()

        # Apply pagination
        query = query.order_by(func.coalesce(DatasetCellStats.mining_weight, 1.0).desc())
        query = query.limit(filters.limit).offset(filters.offset)

        result = await self.db.execute(query)
        rows = result.all()

        items = [self._to_dataset_info(d, cell, filters.universe) for (d, cell) in rows]

        return PaginatedResult(total=total, items=items)

    def _to_dataset_info(
        self,
        d: DatasetMetadata,
        cell: Optional[DatasetCellStats],
        universe: str,
    ) -> DatasetInfo:
        """Convert a (dataset def, cell) pair to DatasetInfo."""
        return DatasetInfo(
            dataset_id=d.dataset_id,
            name=d.name,
            region=d.region,
            universe=(cell.universe if cell else universe),
            category=d.category,
            subcategory=d.subcategory,
            description=d.description,
            field_count=(cell.field_count if cell else None) or 0,
            alpha_success_count=(cell.alpha_success_count if cell else None) or 0,
            alpha_fail_count=(cell.alpha_fail_count if cell else None) or 0,
            mining_weight=(cell.mining_weight if cell else None) or 1.0,
            last_synced_at=(cell.last_synced_at if cell else None),
            date_coverage=(cell.date_coverage if cell else None),
            themes=(cell.themes if cell else None),
            resources=(cell.resources if cell else None),
            value_score=(cell.value_score if cell else None),
            alpha_count=(cell.alpha_count if cell else None),
            pyramid_multiplier=(cell.pyramid_multiplier if cell else None),
            coverage=(cell.coverage if cell else None),
        )
    
    async def list_categories(self) -> List[str]:
        """
        Get list of all unique dataset categories.
        
        Returns:
            Sorted list of category names
        """
        stmt = select(DatasetMetadata.category).distinct().where(
            DatasetMetadata.category != None
        )
        result = await self.db.execute(stmt)
        categories = result.scalars().all()
        return sorted([c for c in categories if c])
    
    # =========================================================================
    # Field Operations
    # =========================================================================
    
    async def get_dataset_fields(
        self,
        dataset_id: str,
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
        limit: int = 100,
        offset: int = 0,
    ) -> PaginatedResult:
        """
        Get fields for a specific dataset.
        
        Args:
            dataset_id: Dataset identifier
            region: Region filter
            universe: Universe filter
            delay: Delay filter
            limit: Maximum results
            offset: Pagination offset
            
        Returns:
            PaginatedResult with DataFieldInfo items
            
        Raises:
            ValueError if dataset not found
        """
        # Resolve dataset def (universe/delay-invariant → keyed by dataset_id+region).
        ds_stmt = select(DatasetMetadata).where(
            DatasetMetadata.dataset_id == dataset_id,
            DatasetMetadata.region == region,
        )
        ds_result = await self.db.execute(ds_stmt)
        dataset = ds_result.scalar_one_or_none()

        if not dataset:
            raise ValueError(f"Dataset {dataset_id} not found")

        # Query field defs + their (universe, delay) cell stats. LEFT JOIN so a
        # field def with no cell for this universe still lists (None stats).
        cell_on = and_(
            DataFieldCellStats.datafield_ref == DataField.id,
            DataFieldCellStats.universe == universe,
            DataFieldCellStats.delay == delay,
        )
        query = (
            select(DataField, DataFieldCellStats)
            .select_from(DataField)
            .outerjoin(DataFieldCellStats, cell_on)
            .where(DataField.dataset_id == dataset.id)
        )

        # Get total
        count_stmt = select(func.count()).select_from(query.subquery())
        count_result = await self.db.execute(count_stmt)
        total = count_result.scalar_one()

        # Apply pagination
        query = query.limit(limit).offset(offset)

        result = await self.db.execute(query)
        rows = result.all()

        items = [
            DataFieldInfo(
                field_id=f.field_id,
                field_name=f.field_name,
                description=f.description,
                dataset_id=dataset_id,
                region=dataset.region,
                universe=(cell.universe if cell else universe),
                delay=(cell.delay if cell else delay),
                is_active=(cell.is_active if cell else True),
                field_type=f.field_type,
                date_coverage=(cell.date_coverage if cell else None),
                coverage=(cell.coverage if cell else None),
                pyramid_multiplier=(cell.pyramid_multiplier if cell else None),
                user_count=(cell.user_count if cell else None),
                alpha_count=(cell.alpha_count if cell else None),
                category=f.category,
                category_name=f.category_name,
                subcategory=f.subcategory,
                subcategory_name=f.subcategory_name,
                themes=(cell.themes if cell else None),
            )
            for (f, cell) in rows
        ]

        return PaginatedResult(total=total, items=items)
    
    # =========================================================================
    # Sync Operations
    # =========================================================================
    
    def trigger_dataset_sync(
        self,
        region: str,
        universe: str = "TOP3000",
    ) -> str:
        """
        Trigger background sync of datasets.
        
        Args:
            region: Region to sync
            universe: Universe to sync
            
        Returns:
            Celery task ID
        """
        from backend.tasks import sync_datasets_from_brain
        task = sync_datasets_from_brain.delay(region=region, universe=universe)
        return str(task.id)
    
    def trigger_field_sync(
        self,
        dataset_id: str,
        region: str = "USA",
        universe: str = "TOP3000",
        delay: int = 1,
    ) -> str:
        """
        Trigger background sync of dataset fields.
        
        Args:
            dataset_id: Dataset to sync
            region: Region
            universe: Universe
            delay: Delay
            
        Returns:
            Celery task ID
        """
        from backend.tasks import sync_fields_from_brain
        task = sync_fields_from_brain.delay(
            dataset_id=dataset_id,
            region=region,
            universe=universe,
            delay=delay,
        )
        return str(task.id)
