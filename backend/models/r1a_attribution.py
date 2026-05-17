"""R1a hook attribution log table (Phase 0 v1.6 fix, 2026-05-17).

WHY THIS TABLE EXISTS — production reality vs original Phase 0 design:

  Plan v1.5 §1.4 wrote R1a metrics into `AlphaCandidate.metrics` (Pydantic
  field), counting on the downstream persistence path
  (`backend/agents/graph/nodes/persistence.py`) to insert them into the
  `alphas` table JSONB column. That assumption holds for **PROV/PASS**
  alphas (which always INSERT). But empirical run of task #1334 shows:

      EVALUATE node round 1 routing breakdown:
          pass_count            = 0
          provisional_count     = 1     ← only this one INSERTs
          optimize_count        = 10    ← go to optimization queue, not alphas
          fail_count            = 39    ← dropped (don't INSERT)
          total alphas evaluated = 50

  R1a hook fires 50 times per round (one per AlphaCandidate), but only the
  1 PROV row actually carries the `_r1a_attribution` field forward to DB.
  49 R1a evaluations are GC'd with the Pydantic objects. Plan §1.4
  hook-on-AlphaCandidate.metrics design fundamentally cannot capture
  FAIL/OPTIMIZE attribution that way.

  Phase 1 R2/Q7 bandit-arm-set design needs **all** attribution samples
  (especially the FAIL ones — that's the whole point of a "hypothesis vs
  implementation" reverse-attribution signal). So R1a must INSERT
  independently, not piggyback on alpha persistence.

DESIGN:
  - One row per (task_id, alpha-evaluated) — 50/round vs 1/round = 50×
    better R1a accumulation throughput.
  - `alpha_id_brain` = BRAIN's returned id when sim succeeded, else NULL
    (FAIL alphas still get a row).
  - `expression_hash` always populated — allows joining back to alphas if
    the alpha later gets PROV-rerun and INSERTs into the alphas table.
  - All R1a fields preserved verbatim from the shim output, plus
    `hook_version` + `hook_error` for debugging post-mortem.
"""
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Index, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class R1aAttributionLog(SQLAlchemyBase):
    """One row per R1a hook invocation in evaluation node."""

    __tablename__ = "r1a_attribution_log"
    __table_args__ = (
        Index("ix_r1a_task_id", "task_id"),
        Index("ix_r1a_created_at", "created_at"),
        Index("ix_r1a_attribution", "attribution"),
    )

    id = Column(BigInteger, primary_key=True)
    task_id = Column(Integer, nullable=True, index=True)  # mining_task.id, nullable for non-task hook calls
    alpha_id_brain = Column(String(64), nullable=True)    # BRAIN-returned id when sim ok; NULL for FAIL alphas
    expression = Column(Text, nullable=False)             # the alpha expression evaluated
    expression_hash = Column(String(64), nullable=True)   # sha256 prefix for join-back to alphas

    # Hook output — verbatim from enhance_existing_node_evaluate
    attribution = Column(String(20), nullable=True)       # 'hypothesis'/'implementation'/'both'/'unknown' or NULL on fail
    attribution_confidence = Column(Float, nullable=True)
    attribution_evidence = Column(JSONB, nullable=True)   # List[str], NULL when empty
    should_retry_implementation = Column(String(8), nullable=True)  # 'true'/'false' string for asyncpg simplicity
    should_modify_hypothesis = Column(String(8), nullable=True)

    # Bookkeeping
    hook_version = Column(String(8), default="v1")
    hook_error = Column(Text, nullable=True)              # str(exception)[:200] when hook raised
    quality_status_at_eval = Column(String(20), nullable=True)  # PASS/PROV/OPTIMIZE/FAIL/PENDING
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
