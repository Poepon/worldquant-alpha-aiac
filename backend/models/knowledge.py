"""
Knowledge Models - Knowledge base and learning entities

Contains KnowledgeEntry, OperatorPreference, and related models.
"""

import hashlib

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


def compute_pattern_hash(pattern_text: str, region: str = None, dataset_id: str = None) -> str:
    """W3-frozen pattern hash (plan R4 #6).

    Concatenation order: pattern_text.strip() | region | dataset_id
    Empty region/dataset_id collapse to ''. Result is sha256 hex truncated
    to 32 chars. ONCE STABLE THIS FORMULA MUST NOT CHANGE — otherwise the
    UNIQUE INDEX on knowledge_entries.pattern_hash becomes invalid and
    historical rows must be re-backfilled.
    """
    raw = (pattern_text or "").strip() + "|" + (region or "") + "|" + (dataset_id or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class KnowledgeEntry(SQLAlchemyBase):
    """
    Knowledge Entry - Stores patterns learned from mining operations.

    Used by RAG service to provide context for alpha generation.
    """
    __tablename__ = "knowledge_entries"
    __table_args__ = (
        Index("ix_kb_pattern_hash", "pattern_hash", unique=True),
        {'extend_existing': True},
    )

    id = Column(Integer, primary_key=True, index=True)
    entry_type = Column(String(50), nullable=False)  # SUCCESS_PATTERN, FAILURE_PITFALL, etc.
    pattern = Column(Text)
    # W3: stable hash of (pattern + region + dataset_id) for idempotent upsert.
    # Computed via compute_pattern_hash() at the application layer; UNIQUE
    # constraint enforces no duplicate entries for the same (pattern, region,
    # dataset_id) tuple even under concurrent writers.
    pattern_hash = Column(String(32))
    description = Column(Text)
    meta_data = Column(JSONB, default={})
    usage_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_by = Column(String(50), default="SYSTEM")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())


class BanditState(SQLAlchemyBase):
    """
    W3: Persistent state for the dataset-selection multi-armed bandit.

    One row per (region, dataset_id). sim_count_today is the value used
    by the cost-aware reward formula in dataset_selector.update_reward;
    it is reset each UTC midnight by a Celery beat task.
    """
    __tablename__ = "bandit_state"
    __table_args__ = {'extend_existing': True}

    region = Column(String(10), primary_key=True)
    dataset_id = Column(String(100), primary_key=True)
    pulls = Column(Integer, default=0, nullable=False)
    total_reward = Column(Float, default=0.0, nullable=False)
    sim_count_today = Column(Integer, default=0, nullable=False)
    last_reset = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())


class OperatorPreference(SQLAlchemyBase):
    """
    Operator Preference - Tracks operator usage statistics.
    
    Used for learning which operators are more successful.
    """
    __tablename__ = "operator_prefs"
    __table_args__ = {'extend_existing': True}
    
    operator_name = Column(String(100), primary_key=True)
    status = Column(String(50), default="ACTIVE")
    usage_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_rate = Column(Float, default=0.0)
    updated_at = Column(DateTime, server_default=func.now())


class RLState(SQLAlchemyBase):
    """
    RL State - Reinforcement learning state for exploration.
    """
    __tablename__ = "rl_states"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    state_key = Column(String(200), unique=True, nullable=False)
    state_type = Column(String(50), nullable=False)
    q_value = Column(Float, default=0.0)
    visit_count = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    meta_data = Column(JSONB)
    updated_at = Column(DateTime, server_default=func.now())
    created_at = Column(DateTime, server_default=func.now())


class RLAction(SQLAlchemyBase):
    """
    RL Action - Reinforcement learning action record.
    """
    __tablename__ = "rl_actions"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    state_id = Column(Integer)
    action_type = Column(String(100))
    action_params = Column(JSONB)
    reward = Column(Float)
    next_state_id = Column(Integer)
    executed_at = Column(DateTime, server_default=func.now())
