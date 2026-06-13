"""
Config Models - System configuration and credentials

Contains SystemConfig, credentials, and auth token models.
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, Float, Index
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class SystemConfig(SQLAlchemyBase):
    """
    System Config - Key-value configuration storage.
    """
    __tablename__ = "system_configs"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Text)
    config_type = Column(String(50))
    description = Column(Text)
    updated_at = Column(DateTime, server_default=func.now())


class BrainAuthToken(SQLAlchemyBase):
    """
    Brain Auth Token - Cached authentication tokens.
    """
    __tablename__ = "brain_auth_tokens"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True, default=1)
    email = Column(String(255))
    jwt_token = Column(Text, nullable=False)
    last_auth_time = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class WQBCredential(SQLAlchemyBase):
    """
    WQB Credential - Encrypted WorldQuant credentials.
    """
    __tablename__ = "wqb_credentials"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    username_encrypted = Column(Text, nullable=False)
    password_encrypted = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())


class LLMProvider(SQLAlchemyBase):
    """
    LLM Provider - Configuration for LLM providers.
    """
    __tablename__ = "llm_providers"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    model_name = Column(String(200), nullable=False)
    api_key_encrypted = Column(Text)
    base_url = Column(String(500))
    max_tokens = Column(Integer, default=4096)
    temperature = Column(Float, default=0.7)
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# =============================================================================
# P3 — Runtime Feature Flag Override (2026-05-16)
# Source: docs/alphagbm_skills_research_2026-05-15.md, ops dashboard plan §1.4.
# Allows ops console to flip ENABLE_* flags at runtime without restarting
# FastAPI / Celery workers. The Settings.__getattribute__ hook in
# backend/config.py reads the in-process _flag_override_cache; this table is
# the durable source. Cache refresher (lifespan + worker_process_init)
# polls every 60s; explicit POST /ops/flags/refresh-all forces immediate sync.
# =============================================================================


class FeatureFlagOverride(SQLAlchemyBase):
    """Runtime override for a single ENABLE_* flag in backend/config.Settings.

    `flag_value` is JSON-encoded text so the same row can carry bool / int /
    float / str / json values; FeatureFlagService coerces back per
    `flag_type`. `flag_name` is unique — UPSERT-style writes only.
    """
    __tablename__ = "feature_flag_overrides"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True)
    flag_name = Column(String(80), unique=True, nullable=False)
    flag_value = Column(Text, nullable=False)               # JSON-encoded
    flag_type = Column(String(20), nullable=False, default="bool")  # bool|int|float|str|json
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by = Column(String(64), default="system")
    note = Column(Text)


class FeatureFlagAudit(SQLAlchemyBase):
    """Append-only log of every flag flip / clear operation.

    Powers the audit Drawer + Timeline in /ops/feature-flags. We keep
    old_value alongside new_value so reverts can be reconstructed without
    consulting the override table history.
    """
    __tablename__ = "feature_flag_audit"
    __table_args__ = (
        Index("ix_feature_flag_audit_name_created", "flag_name", "created_at"),
        {'extend_existing': True},
    )

    id = Column(Integer, primary_key=True)
    flag_name = Column(String(80), nullable=False, index=True)
    old_value = Column(Text)                                # JSON-encoded; null on first-set
    new_value = Column(Text, nullable=False)                # JSON-encoded
    # Phase 4 A1.2 (2026-05-20): widened action enum —
    #   'set' | 'clear'                       (legacy, unchanged)
    #   'sentinel_set' | 'sentinel_restore'   (NEW — R12 sentinel cascade)
    action = Column(String(20), nullable=False)
    actor = Column(String(64), nullable=False, default="ops_console")
    note = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    # Phase 4 A1.2 (2026-05-20): R12 LLM_MODE=assistant sentinel cascade.
    # When ENABLE_LLM_ASSISTANT_MODE is set True, feature_flag_service
    # forces the 4 LLM_ASSISTANT_SENTINEL_FLAGS to False in the same
    # transaction. Each forced flip writes an audit row with
    # sentinel_trigger_for='ENABLE_LLM_ASSISTANT_MODE' so restore_sentinel()
    # can reverse the cascade later via a single WHERE clause.
    # NULL on regular set/clear rows (legacy semantics preserved).
    sentinel_trigger_for = Column(String(64), nullable=True)
    # Stamped when restore_sentinel() reverts the row's override; lets
    # repeated restore_sentinel calls idempotently skip already-restored
    # rows via "WHERE sentinel_trigger_for=X AND restored_at IS NULL".
    restored_at = Column(DateTime, nullable=True)
    restored_by = Column(String(64), nullable=True)


