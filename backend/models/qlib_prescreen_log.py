"""Q10 pyqlib pre-screen telemetry log (Phase 3 Q10 PR1b, 2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md §6.

One row per ``prescreen_alpha()`` call. Captures the local pyqlib verdict
(pass / reject / skip) plus telemetry for post-shadow calibration. Flag-
gated by ``ENABLE_QLIB_PRESCREEN``; OFF = 0 rows. Dedicated table per
``[[feedback_r1a_dedicated_log_table]]`` (extending r1a_attribution_log
would lose 50x throughput because Q10 fires per simulate attempt, not
just per PASS/PROV alpha).

Cross-ref columns (``brain_followup_*``) are filled by a separate update
task scheduled after BRAIN returns. In hard mode they stay NULL because
BRAIN was never called.
"""
from sqlalchemy import BigInteger, Column, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class QlibPrescreenLog(SQLAlchemyBase):
    """One row per Q10 prescreen_alpha() invocation."""

    __tablename__ = "qlib_prescreen_log"
    __table_args__ = (
        Index("ix_q10_task_id", "task_id"),
        Index("ix_q10_created_at", "created_at"),
        Index("ix_q10_verdict", "verdict"),
        Index("ix_q10_expr_hash", "expression_hash"),
    )

    id = Column(BigInteger, primary_key=True)
    task_id = Column(Integer, nullable=True)
    alpha_candidate_idx = Column(Integer, nullable=True)  # state.pending_alphas idx for join-back

    brain_expression = Column(Text, nullable=False)
    expression_hash = Column(String(64), nullable=False)  # sha256[:64] for join-back (R1a convention)
    qlib_expression = Column(Text, nullable=True)         # NULL if untranslatable
    region = Column(String(20), nullable=False)
    universe = Column(String(50), nullable=False)

    # Verdict
    verdict = Column(String(20), nullable=False)          # 'pass' / 'reject' / 'skip'
    reject_reason = Column(Text, nullable=True)           # "sharpe=0.12<0.3" etc
    skip_reason = Column(String(80), nullable=True)       # 'untranslatable' / 'engine_disabled' / 'timeout' / 'eval_error:XYZ' / 'metrics_nan'
    translation_error = Column(Text, nullable=True)       # full error msg on translation failure

    # Metrics
    local_sharpe = Column(Float, nullable=True)
    local_ic = Column(Float, nullable=True)
    engine_kind = Column(String(32), nullable=False)      # 'pyqlib_live'/'pyqlib_snapshot'/'pandas_snapshot'/'disabled'
    elapsed_ms = Column(Integer, nullable=False)

    # Mode at call time (cohort analysis across rollout stages)
    mode_at_call = Column(String(8), nullable=False)      # 'shadow'/'soft'/'hard'

    # Cross-ref (post-BRAIN follow-up; NULL in hard mode)
    brain_followup_status = Column(String(20), nullable=True)    # 'PASS'/'PROVISIONAL'/'FAIL'
    brain_followup_sharpe = Column(Float, nullable=True)
    brain_disagreement = Column(String(8), nullable=True)        # 'true'/'false'

    # NOTE: no inline ``index=True`` here — the explicit
    # ``Index("ix_q10_created_at", "created_at")`` in ``__table_args__`` above
    # is the single source of truth. dev ``create_all()`` would otherwise
    # create a second auto-named index ``ix_qlib_prescreen_log_created_at``
    # that prod Alembic does not have.
    created_at = Column(DateTime(timezone=True), server_default=func.now())
