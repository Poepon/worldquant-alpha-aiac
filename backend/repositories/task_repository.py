"""
Task Repository - Data access for MiningTask entities

Provides specialized queries for task management, including
status updates and experiment run tracking.
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from sqlalchemy.orm import selectinload

from backend.repositories.base_repository import BaseRepository
from backend.protocols.repository_protocol import PaginationParams, PaginatedResult
from backend.models import MiningTask, TraceStep, MiningStatus

logger = logging.getLogger("repositories.task")


class TaskRepository(BaseRepository[MiningTask]):
    """
    Repository for MiningTask entity with specialized queries.
    
    Provides methods for:
    - Task lifecycle management
    - Status queries
    - Experiment run tracking
    - Progress updates
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db, MiningTask)
    
    # =========================================================================
    # Status-Based Queries
    # =========================================================================
    
    async def get_active_tasks(self) -> List[MiningTask]:
        """
        Get all currently active (RUNNING) tasks.
        
        Returns:
            List of running tasks
        """
        query = select(MiningTask).where(MiningTask.status == "RUNNING")
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    async def get_by_status(
        self,
        status: str,
        pagination: Optional[PaginationParams] = None,
    ) -> PaginatedResult[MiningTask]:
        """
        Get tasks by status.
        
        Args:
            status: The status to filter by
            pagination: Pagination parameters
            
        Returns:
            Paginated result of tasks
        """
        return await self.find_by({"status": status}, pagination)
    
    async def get_pending_tasks(self, limit: int = 10) -> List[MiningTask]:
        """
        Get pending tasks ready to start.
        
        Args:
            limit: Maximum number of tasks to return
            
        Returns:
            List of pending tasks
        """
        query = (
            select(MiningTask)
            .where(MiningTask.status == "PENDING")
            .order_by(MiningTask.created_at.asc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        return list(result.scalars().all())
    
    # =========================================================================
    # Status Updates
    # =========================================================================
    
    async def update_status(self, task_id: int, status: str) -> bool:
        """
        Update task status.
        
        Args:
            task_id: The task ID
            status: New status
            
        Returns:
            True if updated, False if not found
        """
        return await self.update_by_id(task_id, {"status": status})
    
    # update_progress / increment_iteration retired in Phase 1d-2 (progress_current
    # / current_iteration columns dropped; no live caller post-ONESHOT removal).

    async def mark_completed(self, task_id: int) -> bool:
        """
        Mark a task as completed.
        
        Args:
            task_id: The task ID
            
        Returns:
            True if updated, False if not found
        """
        return await self.update_status(task_id, "COMPLETED")
    
    async def mark_failed(self, task_id: int, error_message: Optional[str] = None) -> bool:
        """
        Mark a task as failed.
        
        Args:
            task_id: The task ID
            error_message: Optional error message
            
        Returns:
            True if updated, False if not found
        """
        values = {"status": "FAILED"}
        # Note: MiningTask doesn't have error_message field, 
        # we could add it to config JSONB or create a separate log
        return await self.update_by_id(task_id, values)
    
    # =========================================================================
    # Task with Relations
    # =========================================================================
    
    async def get_with_alphas(self, task_id: int) -> Optional[MiningTask]:
        """
        Get task with its alphas loaded.
        
        Args:
            task_id: The task ID
            
        Returns:
            Task with alphas relation loaded
        """
        return await self.get_by_id(task_id, load_relations=["alphas"])
    
    async def get_with_trace_steps(self, task_id: int) -> Optional[MiningTask]:
        """
        Get task with its trace steps loaded.
        
        Args:
            task_id: The task ID
            
        Returns:
            Task with trace_steps relation loaded
        """
        return await self.get_by_id(task_id, load_relations=["trace_steps"])
    
    async def get_full(self, task_id: int) -> Optional[MiningTask]:
        """
        Get task with all relations loaded.
        
        Args:
            task_id: The task ID
            
        Returns:
            Task with all relations loaded
        """
        query = (
            select(MiningTask)
            .where(MiningTask.id == task_id)
            .options(
                selectinload(MiningTask.alphas),
                selectinload(MiningTask.trace_steps),
            )
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    async def get_status_counts(self) -> Dict[str, int]:
        """
        Get count of tasks by status.
        
        Returns:
            Dict of status -> count
        """
        query = select(
            MiningTask.status,
            func.count(MiningTask.id).label("count")
        ).group_by(MiningTask.status)
        
        result = await self.db.execute(query)
        return {row.status: row.count for row in result.all()}
    
    async def get_region_distribution(self) -> Dict[str, int]:
        """
        Get distribution of tasks by region.
        
        Returns:
            Dict of region -> count
        """
        query = select(
            MiningTask.region,
            func.count(MiningTask.id).label("count")
        ).group_by(MiningTask.region)
        
        result = await self.db.execute(query)
        return {row.region: row.count for row in result.all()}
