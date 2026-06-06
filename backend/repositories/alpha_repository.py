"""
Alpha Repository - Data access for Alpha entities

Provides specialized queries for alpha management, including
filtering by task, metrics, and deduplication checks.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import selectinload

from backend.repositories.base_repository import BaseRepository
from backend.protocols.repository_protocol import PaginationParams, PaginatedResult
from backend.models import Alpha, MiningTask, Hypothesis, DatasetMetadata

logger = logging.getLogger("repositories.alpha")


class AlphaRepository(BaseRepository[Alpha]):
    """
    Repository for Alpha entity with specialized queries.
    
    Provides methods for:
    - Querying alphas by task
    - Finding alphas by metrics thresholds
    - Deduplication via expression hash
    - Statistics and aggregations
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db, Alpha)
    
    # =========================================================================
    # Task-Related Queries
    # =========================================================================
    
    async def get_by_task_id(
        self,
        task_id: int,
        pagination: Optional[PaginationParams] = None,
    ) -> PaginatedResult[Alpha]:
        """
        Get alphas for a specific task.
        
        Args:
            task_id: The task ID
            pagination: Pagination parameters
            
        Returns:
            Paginated result of alphas
        """
        return await self.find_by({"task_id": task_id}, pagination)
    
    async def get_by_run_id(
        self,
        run_id: int,
        pagination: Optional[PaginationParams] = None,
    ) -> PaginatedResult[Alpha]:
        """
        Get alphas for a specific experiment run.
        
        Args:
            run_id: The experiment run ID
            pagination: Pagination parameters
            
        Returns:
            Paginated result of alphas
        """
        return await self.find_by({"run_id": run_id}, pagination)
    
    # =========================================================================
    # Deduplication
    # =========================================================================
    
    async def get_by_expression_hash(self, expr_hash: str) -> Optional[Alpha]:
        """
        Get alpha by expression hash for deduplication.
        
        Args:
            expr_hash: The expression hash (SHA-256)
            
        Returns:
            The alpha if found, None otherwise
        """
        return await self.find_one_by({"expression_hash": expr_hash})
    
    async def get_by_alpha_id(self, alpha_id: str) -> Optional[Alpha]:
        """
        Get alpha by BRAIN alpha ID.
        
        Args:
            alpha_id: The BRAIN platform alpha ID
            
        Returns:
            The alpha if found, None otherwise
        """
        return await self.find_one_by({"alpha_id": alpha_id})
    
    async def expression_exists(self, expr_hash: str) -> bool:
        """
        Check if an expression already exists (for deduplication).
        
        Args:
            expr_hash: The expression hash
            
        Returns:
            True if exists, False otherwise
        """
        count = await self.count_by({"expression_hash": expr_hash})
        return count > 0
    
    # =========================================================================
    # Metrics-Based Queries
    # =========================================================================
    
    async def get_successful_alphas(
        self,
        task_id: int,
        min_sharpe: Optional[float] = None,
        min_fitness: Optional[float] = None,
        limit: int = 100,
    ) -> List[Alpha]:
        """
        Get successful alphas meeting metrics criteria.
        
        Args:
            task_id: The task ID
            min_sharpe: Minimum Sharpe ratio
            min_fitness: Minimum fitness score
            limit: Maximum number of results
            
        Returns:
            List of alphas meeting criteria
        """
        query = select(Alpha).where(Alpha.task_id == task_id)
        
        # Filter by quality status
        query = query.where(Alpha.quality_status == "PASS")
        
        # Apply metrics filters
        if min_sharpe is not None:
            query = query.where(Alpha.is_sharpe >= min_sharpe)
        
        if min_fitness is not None:
            query = query.where(Alpha.is_fitness >= min_fitness)
        
        # Order by Sharpe descending
        query = query.order_by(Alpha.is_sharpe.desc().nullslast())
        query = query.limit(limit)
        
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    async def get_top_alphas(
        self,
        task_id: Optional[int] = None,
        order_by: str = "is_sharpe",
        limit: int = 10,
    ) -> List[Alpha]:
        """
        Get top alphas ordered by a metric.
        
        Args:
            task_id: Optional task ID filter
            order_by: Column to order by (is_sharpe, is_fitness, is_returns)
            limit: Maximum number of results
            
        Returns:
            List of top alphas
        """
        query = select(Alpha)
        
        if task_id is not None:
            query = query.where(Alpha.task_id == task_id)
        
        # Apply ordering
        if hasattr(Alpha, order_by):
            col = getattr(Alpha, order_by)
            query = query.order_by(col.desc().nullslast())
        
        query = query.limit(limit)
        
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    async def get_by_status(
        self,
        status: str,
        task_id: Optional[int] = None,
        pagination: Optional[PaginationParams] = None,
    ) -> PaginatedResult[Alpha]:
        """
        Get alphas by status.
        
        Args:
            status: The status to filter by (created, simulated, submitted)
            task_id: Optional task ID filter
            pagination: Pagination parameters
            
        Returns:
            Paginated result of alphas
        """
        filters = {"status": status}
        if task_id is not None:
            filters["task_id"] = task_id
        return await self.find_by(filters, pagination)
    
    async def get_by_quality_status(
        self,
        quality_status: str,
        task_id: Optional[int] = None,
        pagination: Optional[PaginationParams] = None,
    ) -> PaginatedResult[Alpha]:
        """
        Get alphas by quality status.
        
        Args:
            quality_status: The quality status (PENDING, PASS, REJECT)
            task_id: Optional task ID filter
            pagination: Pagination parameters
            
        Returns:
            Paginated result of alphas
        """
        filters = {"quality_status": quality_status}
        if task_id is not None:
            filters["task_id"] = task_id
        return await self.find_by(filters, pagination)
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    async def get_task_stats(self, task_id: int) -> Dict[str, Any]:
        """
        Get statistics for alphas in a task.
        
        Args:
            task_id: The task ID
            
        Returns:
            Dict with counts and metrics statistics
        """
        # Total count
        total = await self.count_by({"task_id": task_id})
        
        # Count by quality status
        pass_count = await self.count_by({"task_id": task_id, "quality_status": "PASS"})
        reject_count = await self.count_by({"task_id": task_id, "quality_status": "REJECT"})
        pending_count = await self.count_by({"task_id": task_id, "quality_status": "PENDING"})
        
        # Get metrics aggregates
        metrics_query = select(
            func.avg(Alpha.is_sharpe).label("avg_sharpe"),
            func.max(Alpha.is_sharpe).label("max_sharpe"),
            func.avg(Alpha.is_fitness).label("avg_fitness"),
            func.max(Alpha.is_fitness).label("max_fitness"),
        ).where(Alpha.task_id == task_id)
        
        metrics_result = await self.db.execute(metrics_query)
        metrics = metrics_result.one_or_none()
        
        return {
            "total": total,
            "pass_count": pass_count,
            "reject_count": reject_count,
            "pending_count": pending_count,
            "avg_sharpe": float(metrics.avg_sharpe) if metrics and metrics.avg_sharpe else None,
            "max_sharpe": float(metrics.max_sharpe) if metrics and metrics.max_sharpe else None,
            "avg_fitness": float(metrics.avg_fitness) if metrics and metrics.avg_fitness else None,
            "max_fitness": float(metrics.max_fitness) if metrics and metrics.max_fitness else None,
        }
    
    async def get_region_distribution(self, task_id: Optional[int] = None) -> Dict[str, int]:
        """
        Get distribution of alphas by region.
        
        Args:
            task_id: Optional task ID filter
            
        Returns:
            Dict of region -> count
        """
        query = select(
            Alpha.region,
            func.count(Alpha.id).label("count")
        ).group_by(Alpha.region)

        if task_id is not None:
            query = query.where(Alpha.task_id == task_id)

        result = await self.db.execute(query)
        return {row.region: row.count for row in result.all() if row.region}

    # =========================================================================
    # Baseline Grid Sampling (P0: fit-baseline + Nσ-residual screening)
    # =========================================================================

    async def get_cell_metric_samples(
        self,
        expected_signal: str,
        region: str,
        metric_col: str = "is_sharpe",
        dataset_id: Optional[str] = None,
        category: Optional[str] = None,
        lookback_days: int = 120,
        limit: int = 2000,
    ) -> List[float]:
        """
        Fetch historical metric samples for one baseline grid cell.

        The cell is (hypothesis-family × dataset × region):
          - hypothesis-family = Hypothesis.expected_signal
          - region            = Alpha.region
          - dataset scope:
              * dataset_id given -> FINE cell   (single dataset)
              * category given   -> COARSE cell (all datasets in that category)
              * neither          -> region-wide fallback

        Population = all successfully-backtested alphas (metric not null) in the
        lookback window — NOT just PASS — so the baseline represents a typical
        attempt in the cell, and the residual measures how much an alpha beats it.

        Args:
            expected_signal: hypothesis family tag (momentum / value / ...)
            region: trading region
            metric_col: Alpha column to sample (default "is_sharpe")
            dataset_id: when set, restrict to this dataset (fine cell)
            category: when set and dataset_id is None, restrict to this dataset
                category via the datasets table (coarse cell)
            lookback_days: only consider alphas created within this window
            limit: cap on number of samples returned

        Returns:
            List of metric float values for the cell.
        """
        metric_attr = getattr(Alpha, metric_col, None)
        if metric_attr is None:
            logger.warning(
                f"get_cell_metric_samples: unknown metric column '{metric_col}'"
            )
            return []

        # date_created is the alpha's BRAIN backtest-creation time, stored
        # naive-BEIJING (sync_tasks._parse_to_beijing: BRAIN-UTC +8h, tz stripped).
        # The lookback cutoff must be in the SAME Beijing-naive convention — a
        # naive-UTC utcnow() cutoff would skew this window's boundary by 8h.
        # (Do NOT swap to created_at: that is the local-DB-insert time, a DIFFERENT
        # thing — this query samples a cell by BRAIN-creation recency.)
        beijing_now = datetime.utcnow() + timedelta(hours=8)
        cutoff = beijing_now - timedelta(days=lookback_days)

        query = (
            select(metric_attr)
            .join(Hypothesis, Alpha.hypothesis_id == Hypothesis.id)
            .where(
                Hypothesis.expected_signal == expected_signal,
                Alpha.region == region,
                metric_attr.isnot(None),
                Alpha.date_created >= cutoff,
            )
        )

        if dataset_id is not None:
            query = query.where(Alpha.dataset_id == dataset_id)
        elif category is not None:
            # Subquery (not a join) keeps alpha sample rows from being multiplied.
            # Since the 2026-05-26 cell-stats normalization, datasets is one row
            # per (dataset_id, region), so the .distinct() is belt-and-suspenders.
            ds_subq = (
                select(DatasetMetadata.dataset_id)
                .where(
                    DatasetMetadata.category == category,
                    DatasetMetadata.region == region,
                )
                .distinct()
            )
            query = query.where(Alpha.dataset_id.in_(ds_subq))

        # Order by recency so the limit keeps the most recent samples — the
        # baseline should reflect recent attempts, not an arbitrary DB-order slice.
        query = query.order_by(Alpha.date_created.desc()).limit(limit)

        result = await self.db.execute(query)
        return [float(v) for v in result.scalars().all() if v is not None]

    # =========================================================================
    # Bulk Operations
    # =========================================================================
    
    async def update_quality_status(
        self,
        alpha_ids: List[int],
        quality_status: str,
    ) -> int:
        """
        Bulk update quality status for multiple alphas.
        
        Args:
            alpha_ids: List of alpha IDs to update
            quality_status: New quality status
            
        Returns:
            Number of alphas updated
        """
        if not alpha_ids:
            return 0
        
        from sqlalchemy import update
        
        stmt = (
            update(Alpha)
            .where(Alpha.id.in_(alpha_ids))
            .values(quality_status=quality_status)
        )
        result = await self.db.execute(stmt)
        return result.rowcount
