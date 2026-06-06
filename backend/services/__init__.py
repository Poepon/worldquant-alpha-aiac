"""
Services Module - Business logic layer

This module provides service classes that encapsulate business logic,
separating it from routers (presentation) and repositories (data access).

All services inherit from BaseService and use dependency injection
for external dependencies.

Usage:
    from backend.services import AlphaService, DashboardService
    
    async def my_handler(db: AsyncSession):
        service = AlphaService(db)
        alphas, total = await service.list_alphas(filters)
"""

from backend.services.base import BaseService, transactional

# Core Services
from backend.services.alpha_service import AlphaService, AlphaListFilters
from backend.services.dashboard_service import DashboardService
# mining_service retired in Phase 1d (dead ONESHOT executor)
from backend.services.task_service import TaskService
from backend.services.analysis_service import AnalysisService
from backend.services.credentials_service import CredentialsService

# New Services (added in refactoring)
from backend.services.dataset_service import (
    DatasetService,
    DatasetListFilters,
    DatasetInfo,
    DataFieldInfo,
)
from backend.services.knowledge_service import (
    KnowledgeService,
    KnowledgeListFilters,
    KnowledgeCreateData,
    KnowledgeUpdateData,
    KnowledgeEntryInfo,
)
# run_service retired in Phase 1d (experiment_runs / per-run view)
from backend.services.operator_service import (
    OperatorService,
    OperatorListFilters,
    OperatorInfo,
)
from backend.services.config_service import (
    ConfigService,
    ThresholdsConfig,
    DiversityConfig,
    OperatorPrefInfo,
)
from backend.services.hypothesis_service import (
    HypothesisService,
    HypothesisCreateData,
    HypothesisStats,
)

__all__ = [
    # Base
    "BaseService",
    "transactional",
    # Core Services
    "AlphaService",
    "AlphaListFilters",
    "DashboardService",
    "TaskService",
    "AnalysisService",
    "CredentialsService",
    # Dataset Service
    "DatasetService",
    "DatasetListFilters",
    "DatasetInfo",
    "DataFieldInfo",
    # Knowledge Service
    "KnowledgeService",
    "KnowledgeListFilters",
    "KnowledgeCreateData",
    "KnowledgeUpdateData",
    "KnowledgeEntryInfo",
    # Run Service retired in Phase 1d
    # Operator Service
    "OperatorService",
    "OperatorListFilters",
    "OperatorInfo",
    # Config Service
    "ConfigService",
    "ThresholdsConfig",
    "DiversityConfig",
    "OperatorPrefInfo",
    # Hypothesis Service (Phase 2 B7)
    "HypothesisService",
    "HypothesisCreateData",
    "HypothesisStats",
]
