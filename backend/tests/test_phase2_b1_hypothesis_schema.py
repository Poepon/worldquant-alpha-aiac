"""Phase 2 B1 — Hypothesis schema regression tests.

Locks in the Hypothesis ORM model + alpha.hypothesis_id FK + new
TraceStepType / HypothesisStatus / HypothesisKind enums introduced by
the c7f9e21b3a47 migration.

aiosqlite + JSONB doesn't render under SQLite (same constraint that bit
the V-19.2 tests), so DB-level integration is verified via the alembic
upgrade run separately. Here we focus on:
1. Model imports + column / relationship definitions
2. Enum values match the migration's CHECK strings
3. TraceStepType extensions
"""
from __future__ import annotations

import pytest

from backend.models import (
    Hypothesis,
    HypothesisStatus,
    HypothesisKind,
    TraceStepType,
    Alpha,
)


def test_hypothesis_table_columns():
    cols = {c.name for c in Hypothesis.__table__.columns}
    expected = {
        "id", "statement", "rationale", "kind", "target_tier",
        "expected_signal", "confidence", "novelty",
        "key_fields", "suggested_operators",
        "region", "universe", "dataset_pool",
        "parent_alpha_id", "parent_hypothesis_id",
        "experiment_variant",
        "alpha_count", "pass_count", "sharpe_avg", "sharpe_max",
        "status", "abandon_reason", "is_active",
        "created_at", "updated_at",
    }
    assert cols == expected, f"missing/extra: {expected ^ cols}"


def test_hypothesis_status_enum_values():
    assert {e.value for e in HypothesisStatus} == {
        "PROPOSED", "ACTIVE", "PROMOTED", "ABANDONED", "SUPERSEDED",
    }


def test_hypothesis_kind_enum_values():
    assert {e.value for e in HypothesisKind} == {
        "INVESTMENT_THESIS", "IMPROVEMENT_RULE",
    }


def test_alpha_has_hypothesis_id_fk():
    cols = {c.name for c in Alpha.__table__.columns}
    assert "hypothesis_id" in cols
    fk = Alpha.__table__.c.hypothesis_id
    assert fk.nullable is True
    fk_targets = list(fk.foreign_keys)
    assert len(fk_targets) == 1
    assert fk_targets[0].column.table.name == "hypotheses"
    assert fk_targets[0].column.name == "id"


def test_alpha_legacy_hypothesis_text_column_kept():
    """Phase 2 keeps the legacy alpha.hypothesis Text column for backwards
    compat — it stores the LLM-emitted summary text, distinct from the new
    typed hypothesis_id link."""
    cols = {c.name: c for c in Alpha.__table__.columns}
    assert "hypothesis" in cols
    # Should be Text, not Integer
    from sqlalchemy import Text
    assert isinstance(cols["hypothesis"].type, Text)


def test_tracesteptype_phase2_extensions():
    assert TraceStepType.HYPOTHESIS_PROPOSE.value == "HYPOTHESIS_PROPOSE"
    assert TraceStepType.HYPOTHESIS_FEEDBACK.value == "HYPOTHESIS_FEEDBACK"
    # Legacy HYPOTHESIS still present for Phase 1 path
    assert TraceStepType.HYPOTHESIS.value == "HYPOTHESIS"


def test_hypothesis_relationships_wired():
    rels = {r.key for r in Hypothesis.__mapper__.relationships}
    assert "alphas" in rels
    assert "parent_alpha" in rels


def test_alpha_hypothesis_obj_relationship():
    rels = {r.key for r in Alpha.__mapper__.relationships}
    assert "hypothesis_obj" in rels


def test_hypothesis_default_values():
    """Python-side defaults (in the model) match what node_hypothesis_propose
    will rely on for PROPOSED-state inserts that omit columns. The matching
    DB-side `server_default` is set in the alembic migration."""
    cols = {c.name: c for c in Hypothesis.__table__.columns}
    assert cols["status"].default.arg == "PROPOSED"
    assert cols["alpha_count"].default.arg == 0
    assert cols["pass_count"].default.arg == 0
    assert cols["kind"].default.arg == "INVESTMENT_THESIS"
    assert cols["target_tier"].default.arg == 1
    assert cols["is_active"].default.arg is True
    assert cols["expected_signal"].default.arg == "unknown"
    assert cols["confidence"].default.arg == "medium"
    assert cols["novelty"].default.arg == "established"


def test_hypothesis_required_columns():
    """statement and region are NOT NULL — code_gen requires them to render
    expressions. Aggregated stats default to 0 so filters work even before
    refresh_stats fires."""
    cols = {c.name: c for c in Hypothesis.__table__.columns}
    assert cols["statement"].nullable is False
    assert cols["region"].nullable is False
    assert cols["alpha_count"].nullable is False
    assert cols["pass_count"].nullable is False
    assert cols["status"].nullable is False
    assert cols["is_active"].nullable is False
    # Optional metadata fields are nullable
    assert cols["rationale"].nullable is True
    assert cols["abandon_reason"].nullable is True
    assert cols["sharpe_avg"].nullable is True
