"""Phase 4 Sprint 2 B2 — R13 factor_lens_residuals model.

Plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.9 / v2 §4.6.

One row per (alpha_id, computed_at) — OLS decomposition of an alpha's
daily PnL series against the static factor-returns snapshot stored
under backend/data/factor_returns_snapshot/{region}.parquet.

Schema matches the Alembic migration l7c2d8e1f4a3:
  - alpha_id    FK to alphas, ON DELETE CASCADE
  - residual_sharpe (annualized, post-neutralization)
  - factor_exposures JSON {factor → beta}  (incl. _intercept)
  - r_squared / ols_n_days / mode_used / region

mode_used: "ols_daily" | "bucket_median" | "skipped"

Dedicated table per [[feedback_r1a_dedicated_log_table]] — operator
queries on residual distribution don't fight alpha persistence hot
path, and rows survive alpha purges via cascade-only cleanup.
"""
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class FactorLensResidual(SQLAlchemyBase):
    """OLS factor decomposition result for one alpha snapshot."""

    __tablename__ = "factor_lens_residuals"

    # BigInteger on PG; SQLite test fixtures auto-cast to Integer
    id = Column(BigInteger, primary_key=True)

    alpha_id = Column(
        Integer,
        ForeignKey("alphas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    computed_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    residual_sharpe = Column(Float, nullable=False)
    # {factor_name: beta_coefficient, "_intercept": float}
    factor_exposures = Column(JSONB, nullable=False, default=dict)
    r_squared = Column(Float, nullable=True)
    ols_n_days = Column(Integer, nullable=True)
    mode_used = Column(String(20), nullable=False)
    region = Column(String(10), nullable=True, index=True)
