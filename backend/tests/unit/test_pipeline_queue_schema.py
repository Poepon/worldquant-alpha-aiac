"""Phase 0 schema contract test — pool pipeline queues (four-pool decoupling).

Locks the hyp_intent / candidate_queue / alpha_failures.metrics schema BEFORE
Phase 1b builds the claim/lease logic on top of it, so a future model/migration
drift (renamed index, diverged stage vocab, dropped role-snapshot column) trips
a test rather than surfacing as a silent runtime bug.

Uses a self-contained in-memory SQLite engine + the JSONB→JSON shim that
backend/tests/conftest.py registers globally (@compiles) — the same create_all
path init_db() and the other unit fixtures use.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database import SQLAlchemyBase
from backend.models import AlphaFailure, CandidateQueue, HypothesisIntent


@pytest.fixture
def sqlite_session():
    eng = create_engine("sqlite://")  # in-memory; conftest @compiles shims JSONB
    SQLAlchemyBase.metadata.create_all(eng)
    with Session(eng) as s:
        yield s
    eng.dispose()


def test_hyp_intent_defaults_and_jsonb_roundtrip(sqlite_session):
    s = sqlite_session
    hi = HypothesisIntent(
        region="USA", universe="TOP3000", dataset_id="pv1", delay=1, fanout=4,
        config_snapshot={"thresholds": {"sharpe_min": 1.5},
                         "brain_role_snapshot": {"effective_default_test_period": "P0Y"}},
        prompt_version="v27", thresholds_version="eval-band-2026-06",
    )
    s.add(hi)
    s.commit()
    s.refresh(hi)
    assert hi.stage == "PENDING"          # server_default
    assert hi.attempts == 0               # server_default
    assert hi.config_snapshot["thresholds"]["sharpe_min"] == 1.5
    assert hi.created_at is not None


def test_candidate_queue_rolesnapshot_firstclass(sqlite_session):
    s = sqlite_session
    hi = HypothesisIntent(region="USA", config_snapshot={})
    s.add(hi)
    s.commit()
    s.refresh(hi)

    cq = CandidateQueue(
        hyp_intent_id=hi.id, expression="ts_rank(close, 20)", region="USA",
        universe="TOP3000", delay=1, dataset_id="pv1", dataset_category="price_volume",
        effective_default_test_period="P0Y", effective_sharpe_submit_min=1.58,
        sim_settings={"neutralization": "INDUSTRY", "truncation": 0.08},
        context={"_validation_findings": []},
    )
    s.add(cq)
    s.commit()
    s.refresh(cq)

    assert cq.stage == "PENDING_SIM"      # server_default
    assert cq.attempts == 0
    # role-snapshot first-class columns (终审 #7) — must survive the round-trip
    # so a hydrated row never falls back to the wrong testPeriod / sharpe gate.
    assert cq.effective_default_test_period == "P0Y"
    assert cq.effective_sharpe_submit_min == 1.58
    assert cq.sim_settings["neutralization"] == "INDUSTRY"
    assert cq.context["_validation_findings"] == []


def test_alpha_failure_metrics_column(sqlite_session):
    s = sqlite_session
    af = AlphaFailure(
        expression="bad_expr", error_type="TEST",
        metrics={"sharpe": 0.1, "verdict_signals": {"turnover": 0.9}},
    )
    s.add(af)
    s.commit()
    s.refresh(af)
    assert af.metrics["verdict_signals"]["turnover"] == 0.9


def test_alpha_failure_candidate_queue_id_partial_unique(sqlite_session):
    """Pool dedup backstop: two FAIL rows with the SAME candidate_queue_id violate
    the partial-unique index; many NULLs (FLAT/legacy) are allowed (NULLs distinct)."""
    from sqlalchemy.exc import IntegrityError
    s = sqlite_session
    # many NULLs OK (FLAT / legacy path — candidate_queue_id unconstrained)
    s.add(AlphaFailure(expression="flat1", error_type="X", candidate_queue_id=None))
    s.add(AlphaFailure(expression="flat2", error_type="X", candidate_queue_id=None))
    s.commit()
    # first pool row OK
    s.add(AlphaFailure(expression="pool1", error_type="X", candidate_queue_id=777))
    s.commit()
    # duplicate candidate_queue_id → IntegrityError (the crash-window re-persist
    # backstop behind the Redis persist-marker)
    s.add(AlphaFailure(expression="pool1dup", error_type="X", candidate_queue_id=777))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    # the partial-unique index is declared on the model
    assert "uq_alpha_failures_candidate_queue_id" in {
        i.name for i in AlphaFailure.__table__.indexes
    }


def test_candidate_queue_dataset_category_widened():
    """dataset_category widened 80→200 (BRAIN categories can reach ~203)."""
    assert CandidateQueue.__table__.columns["dataset_category"].type.length == 200


def test_index_sets_and_lineage_anchor():
    """Index names must match the migration exactly; lineage anchors on
    hypotheses.id, never run_id (no run_id column on either queue table)."""
    cq_idx = {i.name for i in CandidateQueue.__table__.indexes}
    hi_idx = {i.name for i in HypothesisIntent.__table__.indexes}
    assert cq_idx == {
        "ix_candidate_queue_claim",
        "ix_candidate_queue_dataset_id",
        "ix_candidate_queue_hyp_intent",
        "ix_candidate_queue_hypothesis_id",
        "ix_candidate_queue_task_id",
    }
    assert hi_idx == {
        "ix_hyp_intent_claim",
        "ix_hyp_intent_dataset_id",
        "ix_hyp_intent_task_id",
    }
    assert "run_id" not in CandidateQueue.__table__.columns
    assert "run_id" not in HypothesisIntent.__table__.columns
    # current_hypothesis_id is the lineage anchor on candidate_queue
    assert "current_hypothesis_id" in CandidateQueue.__table__.columns
