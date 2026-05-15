"""
Models Module - Database entities

This module provides all SQLAlchemy models for the application.
Models are organized into separate files by domain but re-exported
here for backward compatibility.

Usage:
    from backend.models import Alpha, MiningTask, KnowledgeEntry
"""

# Enums
from backend.models.base import (
    MiningStatus,
    DatasetStrategy,
    AgentMode,
    TraceStepType,
    QualityStatus,
    HumanFeedback,
    KnowledgeEntryType,
    JobStatus,
    HypothesisStatus,
    HypothesisKind,
)

# Task models
from backend.models.task import (
    MiningTask,
    ExperimentRun,
    TraceStep,
    MiningJob,
)

# Alpha models
from backend.models.alpha import (
    Alpha,
    AlphaFailure,
    AlphaPnl,
)

# Status transition audit
from backend.models.transition import (
    AlphaStatusTransition,
    HypothesisStatusTransition,
)

# Hypothesis (Phase 2 B1) + per-round stats (V-27.92)
from backend.models.hypothesis import Hypothesis, HypothesisRoundStats

# Knowledge models
from backend.models.knowledge import (
    KnowledgeEntry,
    OperatorPreference,
    RLState,
    RLAction,
    BanditState,
    compute_pattern_hash,
)

# Metadata models
from backend.models.metadata import (
    DatasetMetadata,
    DataField,
    Operator,
    OperatorBlacklist,
    Region,
    Universe,
    Neutralization,
    PyramidMultiplier,
    Template,
    TemplateVariable,
)

# Config models
from backend.models.config import (
    SystemConfig,
    BrainAuthToken,
    WQBCredential,
    LLMProvider,
)

__all__ = [
    # Enums
    "MiningStatus",
    "DatasetStrategy",
    "AgentMode",
    "TraceStepType",
    "QualityStatus",
    "HumanFeedback",
    "KnowledgeEntryType",
    "JobStatus",
    "HypothesisStatus",
    "HypothesisKind",
    # Task
    "MiningTask",
    "ExperimentRun",
    "TraceStep",
    "MiningJob",
    # Alpha
    "Alpha",
    "AlphaFailure",
    "AlphaPnl",
    "AlphaStatusTransition",
    "HypothesisStatusTransition",
    # Hypothesis (Phase 2)
    "Hypothesis",
    "HypothesisRoundStats",
    # Knowledge
    "KnowledgeEntry",
    "OperatorPreference",
    "RLState",
    "RLAction",
    "BanditState",
    "compute_pattern_hash",
    # Metadata
    "DatasetMetadata",
    "DataField",
    "Operator",
    "OperatorBlacklist",
    "Region",
    "Universe",
    "Neutralization",
    "PyramidMultiplier",
    "Template",
    "TemplateVariable",
    # Config
    "SystemConfig",
    "BrainAuthToken",
    "WQBCredential",
    "LLMProvider",
]
