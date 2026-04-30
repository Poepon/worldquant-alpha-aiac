"""
Base Models - Enums and common definitions

This module contains all enums and common base definitions
used across model modules.
"""

import enum
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.sql import func


# =============================================================================
# ENUMS
# =============================================================================

class MiningStatus(str, enum.Enum):
    """Status of a mining task."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    EARLY_STOPPED = "EARLY_STOPPED"  # W1: round-level pruner triggered


class DatasetStrategy(str, enum.Enum):
    """Strategy for dataset selection."""
    AUTO = "AUTO"           # Hierarchical RAG exploration
    SPECIFIC = "SPECIFIC"   # User-specified datasets


class AgentMode(str, enum.Enum):
    """Mode of agent operation."""
    AUTONOMOUS = "AUTONOMOUS"   # Fully automatic
    INTERACTIVE = "INTERACTIVE"  # Pause at each step


class TraceStepType(str, enum.Enum):
    """Type of trace step in mining workflow."""
    RAG_QUERY = "RAG_QUERY"
    HYPOTHESIS = "HYPOTHESIS"
    CODE_GEN = "CODE_GEN"
    VALIDATE = "VALIDATE"
    SIMULATE = "SIMULATE"
    SELF_CORRECT = "SELF_CORRECT"
    EVALUATE = "EVALUATE"


class QualityStatus(str, enum.Enum):
    """Quality status of an alpha."""
    PENDING = "PENDING"
    PASS = "PASS"
    PASS_PROVISIONAL = "PASS_PROVISIONAL"  # near-PASS: 134-class candidates from R4/R5
    OPTIMIZE = "OPTIMIZE"
    FAIL = "FAIL"
    REJECT = "REJECT"


class HumanFeedback(str, enum.Enum):
    """Human feedback on an alpha."""
    NONE = "NONE"
    LIKED = "LIKED"
    DISLIKED = "DISLIKED"


class KnowledgeEntryType(str, enum.Enum):
    """Type of knowledge entry."""
    SUCCESS_PATTERN = "SUCCESS_PATTERN"
    FAILURE_PITFALL = "FAILURE_PITFALL"
    FIELD_BLACKLIST = "FIELD_BLACKLIST"
    OPERATOR_STAT = "OPERATOR_STAT"


class JobStatus(str, enum.Enum):
    """Status of a mining job."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
