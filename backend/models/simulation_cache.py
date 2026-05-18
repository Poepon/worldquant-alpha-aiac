"""Phase 3 R9 SimulationCache model (2026-05-18).

Caches BRAIN simulate_batch results keyed on
sha256(region|universe|expression|settings_json)[:64]. Hit returns
result_json; miss falls through to BRAIN call. TTL via cached_at filter.

Per master plan §4.5 R9. Alembic head: 9a4f7e8c1d6b.
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Index, Integer, JSON, String, Text,
)
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class SimulationCache(SQLAlchemyBase):
    """One row per cached (region, universe, expression, settings) tuple."""

    __tablename__ = "simulation_cache"
    __table_args__ = (
        Index("ix_sim_cache_key", "cache_key", unique=True),
        Index("ix_sim_cache_expression_hash", "expression_hash"),
        Index("ix_sim_cache_cached_at", "cached_at"),
        Index("ix_sim_cache_region_universe", "region", "universe"),
        {"extend_existing": True},
    )

    id = Column(BigInteger, primary_key=True)
    cache_key = Column(String(64), nullable=False)
    region = Column(String(20), nullable=False)
    universe = Column(String(50), nullable=False)
    expression = Column(Text, nullable=False)
    expression_hash = Column(String(64), nullable=False)
    settings_json = Column(JSON, nullable=False)
    result_json = Column(JSON, nullable=False)
    success = Column(Boolean, nullable=False, server_default="false")
    cached_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    accessed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    access_count = Column(Integer, server_default="1", nullable=False)
