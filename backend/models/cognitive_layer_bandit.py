"""Phase 4 Tier E E1 — cognitive_layer_bandit_state model.

Per-layer Beta-Bernoulli posterior counts for R8-v3 cognitive-layer
selection. Written by the weekly cron run_cognitive_layer_bandit_update;
read by node_hypothesis to seed cognitive_layer_service.select_layer's
``bandit`` strategy. 7 rows (one per layer_id).
"""
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class CognitiveLayerBanditState(SQLAlchemyBase):
    __tablename__ = "cognitive_layer_bandit_state"
    __table_args__ = {"extend_existing": True}

    layer_id = Column(String(64), primary_key=True)
    pass_count = Column(Integer, default=0, nullable=False)
    fail_count = Column(Integer, default=0, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
