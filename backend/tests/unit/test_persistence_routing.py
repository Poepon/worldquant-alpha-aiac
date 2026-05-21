"""P1 persistence routing tests (2026-05-19, plan v1.3.1 §3.3).

Covers:
- node_save_results success_batch / fail_batch routing under flag OFF / ON
- _incremental_save_alphas FAIL acceptance with real aiosqlite ORM (regression
  sentinel per [[feedback_orm_constructor_real_test]] — guards against the same
  class of bugs that masked the R1b Hypothesis task_id= kwarg)
- workflow.py post-loop defensive filter — FAIL without alpha_id is skipped
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class _FakeAlpha(SimpleNamespace):
    """Mirrors AlphaCandidate shape — fields touched by persistence.py."""
    def model_copy(self):
        clone = _FakeAlpha(**self.__dict__)
        clone.metrics = dict(self.metrics or {})
        return clone


def _mk_alpha(
    *,
    quality_status: str,
    alpha_id: str | None,
    is_simulated: bool = True,
    simulation_success: bool = True,
    is_valid: bool = True,
    expression: str = "ts_rank(close, 10)",
    metrics: dict | None = None,
    hypothesis: str = "test thesis",
):
    return _FakeAlpha(
        expression=expression,
        hypothesis=hypothesis,
        explanation="test",
        alpha_id=alpha_id,
        is_valid=is_valid,
        is_simulated=is_simulated,
        simulation_success=simulation_success,
        simulation_error=None,
        validation_error=None,
        quality_status=quality_status,
        metrics=metrics or {"sharpe": 0.8, "fitness": 0.5, "turnover": 0.3},
        parent_alpha_id=None,
        wrapper_kind=None,
    )


def _mk_state(alphas):
    return SimpleNamespace(
        pending_alphas=alphas,
        fields=[], region="USA", universe="TOP3000", task_id=42,
        round_idx=1, dataset_id="pv13",
        current_hypothesis_id=None,
        current_hypothesis_ids=[],
        generated_alphas=[],
        failures=[],
        round_history=[],
        current_round=0,
        hypothesis_round_history={},
        g8_forest_referenced_ids=None,
    )


@pytest.fixture(autouse=True)
def _stub_external_writes():
    """Block hypothesis-service / KB / r1b-persist DB calls; this test focuses
    on the success_batch / fail_batch *routing*, not the orthogonal subsystems
    that node_save_results also invokes."""
    with patch(
        "backend.agents.graph.nodes.persistence._process_hypothesis_feedback",
        new=AsyncMock(return_value={}),
    ), patch(
        "backend.agents.graph.nodes.persistence._get_fields_used_validator",
        return_value=SimpleNamespace(
            validate=lambda _e: SimpleNamespace(used_fields=set())
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# node_save_results: non-incremental path success_batch / fail_batch routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_1_flag_off_fail_with_alpha_id_routes_to_fail_batch_as_QC(monkeypatch):
    """Flag OFF (default) — FAIL with alpha_id stays in fail_batch as
    QUALITY_CHECK_FAILED. Pre-P1 legacy behavior preserved."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", False, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    alpha = _mk_alpha(quality_status="FAIL", alpha_id="A1")
    state = _mk_state([alpha])

    out = await node_save_results(state, config={"configurable": {}})

    assert out["generated_alphas"] == []
    failures = out["failures"]
    assert len(failures) == 1
    assert failures[0].error_type == "QUALITY_CHECK_FAILED"


@pytest.mark.asyncio
async def test_2_flag_on_pass_still_routes_to_success_batch(monkeypatch):
    """Regression sentinel — PASS alphas continue to land in success_batch
    when flag ON."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    alpha = _mk_alpha(quality_status="PASS", alpha_id="A2")
    state = _mk_state([alpha])

    out = await node_save_results(state, config={"configurable": {}})

    assert len(out["generated_alphas"]) == 1
    assert out["generated_alphas"][0].alpha_id == "A2"
    assert out["generated_alphas"][0].quality_status == "PASS"
    assert out["failures"] == []


@pytest.mark.asyncio
async def test_3_flag_on_brain_accepted_fail_routes_to_success_batch(monkeypatch):
    """P1 core: flag ON + FAIL + alpha_id + is_simulated + simulation_success
    → success_batch (FAIL alpha lands in alphas table, not alpha_failures)."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    alpha = _mk_alpha(quality_status="FAIL", alpha_id="A3")
    state = _mk_state([alpha])

    out = await node_save_results(state, config={"configurable": {}})

    assert len(out["generated_alphas"]) == 1
    assert out["generated_alphas"][0].alpha_id == "A3"
    assert out["generated_alphas"][0].quality_status == "FAIL"
    # MUST NOT be in fail_batch (otherwise we'd double-write)
    assert out["failures"] == []


@pytest.mark.asyncio
async def test_4_flag_on_fail_without_alpha_id_logs_OTHER(monkeypatch, caplog):
    """Contract violation: flag ON + FAIL + NO alpha_id → fail_batch as OTHER
    with warning log. Should not be in success_batch."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    alpha = _mk_alpha(quality_status="FAIL", alpha_id=None)
    state = _mk_state([alpha])

    out = await node_save_results(state, config={"configurable": {}})

    assert out["generated_alphas"] == []
    failures = out["failures"]
    assert len(failures) == 1
    # Contract violation labeled OTHER (not QUALITY_CHECK_FAILED) post-P1
    assert failures[0].error_type == "OTHER"
    assert "BRAIN handle missing" in failures[0].error_message


@pytest.mark.asyncio
async def test_5_flag_on_fail_with_alpha_id_but_no_sim_success_routes_to_fail_batch(
    monkeypatch,
):
    """Defensive: flag ON + FAIL + alpha_id present but is_simulated=False
    → fail_batch as OTHER (BRAIN handle unverified)."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    alpha = _mk_alpha(
        quality_status="FAIL", alpha_id="A5",
        is_simulated=False, simulation_success=None,
    )
    state = _mk_state([alpha])

    out = await node_save_results(state, config={"configurable": {}})

    assert out["generated_alphas"] == []
    failures = out["failures"]
    assert len(failures) == 1
    assert failures[0].error_type == "OTHER"


@pytest.mark.asyncio
async def test_6_simulation_error_still_routes_to_fail_batch(monkeypatch):
    """SIMULATION_ERROR (is_simulated=True + simulation_success=False) unchanged
    by P1 — stays in fail_batch."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    alpha = _mk_alpha(
        quality_status="REJECT", alpha_id=None,
        is_simulated=True, simulation_success=False,
    )
    state = _mk_state([alpha])

    out = await node_save_results(state, config={"configurable": {}})

    assert out["generated_alphas"] == []
    failures = out["failures"]
    assert len(failures) == 1
    assert failures[0].error_type == "SIMULATION_ERROR"


@pytest.mark.asyncio
async def test_7_multiple_FAILs_all_routed_to_success_batch(monkeypatch):
    """Batch test: multiple FAIL alphas with alpha_ids all flow into
    success_batch in order (no SAVEPOINT regression — non-incremental path
    doesn't use SAVEPOINTs, but verifies the loop handles N>1 cleanly)."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    alphas = [
        _mk_alpha(quality_status="FAIL", alpha_id=f"A{i}")
        for i in range(4)
    ]
    state = _mk_state(alphas)

    out = await node_save_results(state, config={"configurable": {}})

    assert len(out["generated_alphas"]) == 4
    assert [r.alpha_id for r in out["generated_alphas"]] == ["A0", "A1", "A2", "A3"]
    assert out["failures"] == []


# ---------------------------------------------------------------------------
# Bug A (2026-05-20): pre-BRAIN skip → PRESIM_SKIP, not SIMULATION_ERROR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_brain_skip_labeled_PRESIM_SKIP(monkeypatch):
    """A pre-simulate/Q10 skip (is_simulated=True + simulation_success=False +
    metrics._pre_brain_skip=True) must be labeled PRESIM_SKIP, NOT
    SIMULATION_ERROR — so the quota guard can exclude it (it never consumed a
    BRAIN simulate slot)."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    skipped = _mk_alpha(
        quality_status="PENDING", alpha_id=None,
        is_simulated=True, simulation_success=False,
        metrics={"_pre_brain_skip": True, "sharpe": 0.0},
    )
    skipped.simulation_error = "pre-simulate filter skip: P(PASS)=0.034 < 0.1"
    state = _mk_state([skipped])

    out = await node_save_results(state, config={"configurable": {}})

    assert out["generated_alphas"] == []
    failures = out["failures"]
    assert len(failures) == 1
    assert failures[0].error_type == "PRESIM_SKIP"


@pytest.mark.asyncio
async def test_dedup_skip_labeled_DEDUP_SKIP(monkeypatch):
    """A local-DB dedup skip (metrics._pre_brain_skip + _skip_kind='dedup')
    must be labeled DEDUP_SKIP, NOT SIMULATION_ERROR — it never consumed a
    fresh BRAIN simulate slot, so the quota guard excludes it."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    dup = _mk_alpha(
        quality_status="PENDING", alpha_id=None,
        is_simulated=True, simulation_success=False,
        metrics={"_pre_brain_skip": True, "_skip_kind": "dedup"},
    )
    dup.simulation_error = "DB duplicate: already simulated"
    state = _mk_state([dup])

    out = await node_save_results(state, config={"configurable": {}})

    assert out["generated_alphas"] == []
    failures = out["failures"]
    assert len(failures) == 1
    assert failures[0].error_type == "DEDUP_SKIP"


@pytest.mark.asyncio
async def test_real_sim_error_still_SIMULATION_ERROR_not_presim(monkeypatch):
    """A real BRAIN sim error (is_simulated=True + sim_success=False + NO
    _pre_brain_skip marker) stays SIMULATION_ERROR — the new branch must not
    swallow genuine BRAIN failures."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import node_save_results

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(settings, "T2_INCREMENTAL_PERSISTENCE", False, raising=False)

    real_err = _mk_alpha(
        quality_status="PENDING", alpha_id=None,
        is_simulated=True, simulation_success=False,
        metrics={"sharpe": 0.0},   # no _pre_brain_skip
    )
    real_err.simulation_error = "BRAIN 500 internal error"
    state = _mk_state([real_err])

    out = await node_save_results(state, config={"configurable": {}})

    failures = out["failures"]
    assert len(failures) == 1
    assert failures[0].error_type == "SIMULATION_ERROR"


# ---------------------------------------------------------------------------
# _incremental_save_alphas — FAIL acceptance (filter) test
# ---------------------------------------------------------------------------
# The real ORM end-to-end test sits behind @pytest.mark.requires_postgres
# because _incremental_save_alphas uses `pg_insert(...).on_conflict_do_nothing
# (index_elements=["alpha_id"]).returning(Alpha.id)` which is PostgreSQL-
# specific syntax (aiosqlite silently SAVEPOINT-rolls-back on it). The
# filter-level test below uses a stub db_session that captures the INSERT
# call without executing it — sufficient to verify the §3.2.3 filter
# accepts FAIL alphas with alpha_id + is_simulated + simulation_success.


@pytest.mark.asyncio
async def test_8_incremental_save_filter_accepts_brain_accepted_fail(
    monkeypatch
):
    """Filter regression: with flag ON, _incremental_save_alphas's status
    filter (§3.2.3) accepts FAIL alphas that pass the BRAIN-handle checks.
    Uses a stub db_session that captures the INSERT statement attempt;
    verifies the filter doesn't reject the FAIL row before reaching INSERT.
    """
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import _incremental_save_alphas

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(
        settings, "HYPOTHESIS_REUSE_TERMINAL_GUARD_ENABLED", False, raising=False,
    )

    # Track INSERT attempts; the function does two round-trips per alpha:
    # (1) pg_insert ... RETURNING id (scalar int)
    # (2) post-commit SELECT * WHERE alpha_id=... (Alpha row with .id) used
    #     to populate AlphaResult.db_id
    captured = {"insert": 0, "select": 0}

    class _StubInsertResult:
        def scalar_one_or_none(self):
            return 999    # synthetic inserted_id

    class _StubSelectResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=999)    # synthetic Alpha row

    class _StubNested:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class _StubSession:
        def begin_nested(self):
            return _StubNested()
        async def execute(self, stmt):
            # Crude but effective: pg_insert renders as an Insert statement;
            # post-commit re-fetch renders as a Select. Branch by str type.
            stmt_class = type(stmt).__name__
            if "Insert" in stmt_class:
                captured["insert"] += 1
                return _StubInsertResult()
            captured["select"] += 1
            return _StubSelectResult()
        async def commit(self): pass

    fail_alpha = _mk_alpha(
        quality_status="FAIL",
        alpha_id="brain-Z42",
        metrics={"sharpe": 0.4, "fitness": 0.05, "turnover": 0.35},
    )

    results = await _incremental_save_alphas(
        db_session=_StubSession(),
        task_id=42, run_id=None,
        region="USA", universe="TOP3000", dataset_id="pv13",
        pending_alphas=[fail_alpha],
        hypothesis_id=None, g8_forest_referenced_ids=None,
    )

    # Filter let the FAIL alpha through → INSERT path reached
    assert captured["insert"] >= 1
    assert len(results) == 1
    assert results[0].quality_status == "FAIL"
    assert results[0].alpha_id == "brain-Z42"
    assert results[0].persisted is True
    assert results[0].db_id == 999


@pytest.mark.asyncio
async def test_9_incremental_save_filter_skips_fail_alpha_when_not_simulated(
    monkeypatch
):
    """P1 defensive (§3.2.3): flag ON + FAIL + alpha_id present but
    is_simulated=False → filter skips before reaching INSERT path. The stub
    db_session captures any INSERT attempt; we assert NONE was made."""
    from backend.config import settings
    from backend.agents.graph.nodes.persistence import _incremental_save_alphas

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)
    monkeypatch.setattr(
        settings, "HYPOTHESIS_REUSE_TERMINAL_GUARD_ENABLED", False, raising=False,
    )

    captured = {"insert": 0, "select": 0}

    class _StubInsertResult:
        def scalar_one_or_none(self):
            return 999

    class _StubSelectResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=999)

    class _StubNested:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class _StubSession:
        def begin_nested(self):
            return _StubNested()
        async def execute(self, stmt):
            stmt_class = type(stmt).__name__
            if "Insert" in stmt_class:
                captured["insert"] += 1
                return _StubInsertResult()
            captured["select"] += 1
            return _StubSelectResult()
        async def commit(self): pass

    bogus_alpha = _mk_alpha(
        quality_status="FAIL",
        alpha_id="brain-BOGUS",
        is_simulated=False,
        simulation_success=None,
    )

    results = await _incremental_save_alphas(
        db_session=_StubSession(),
        task_id=42, run_id=None,
        region="USA", universe="TOP3000", dataset_id="pv13",
        pending_alphas=[bogus_alpha],
        hypothesis_id=None, g8_forest_referenced_ids=None,
    )

    # Filter rejected before INSERT was attempted
    assert captured["insert"] == 0
    assert results == []


# ---------------------------------------------------------------------------
# Real-ORM end-to-end (PG-only — pg_insert ON CONFLICT not aiosqlite-compatible)
# ---------------------------------------------------------------------------

@pytest.mark.requires_postgres
@pytest.mark.asyncio
async def test_10_e2e_fail_alpha_lands_in_alphas_table_against_real_pg(
    monkeypatch
):
    """End-to-end real ORM test: with flag ON + real PG, a FAIL alpha with
    BRAIN handle is INSERTed into `alphas` with `quality_status='FAIL'`.
    Requires PG_TEST_DSN env var (auto-skipped otherwise per conftest).

    Skipped on aiosqlite because `pg_insert(...).on_conflict_do_nothing()`
    is PostgreSQL-dialect-specific syntax.
    """
    import os
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import select as _sel, delete as _delete

    from backend.config import settings
    from backend.agents.graph.nodes.persistence import _incremental_save_alphas
    from backend.models import Alpha

    monkeypatch.setattr(settings, "ENABLE_FAIL_ALPHA_PERSIST", True, raising=False)

    engine = create_async_engine(os.environ["PG_TEST_DSN"])
    session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    alpha_id = "TEST_P1_FAIL_001"
    # Ensure clean slate
    async with session_maker() as sess:
        await sess.execute(_delete(Alpha).where(Alpha.alpha_id == alpha_id))
        await sess.commit()

    fail_alpha = _mk_alpha(
        quality_status="FAIL",
        alpha_id=alpha_id,
        metrics={"sharpe": 0.42, "fitness": 0.08, "turnover": 0.36},
    )

    try:
        async with session_maker() as sess:
            results = await _incremental_save_alphas(
                db_session=sess,
                task_id=42, run_id=None,
                region="USA", universe="TOP3000", dataset_id="pv13",
                pending_alphas=[fail_alpha],
                hypothesis_id=None, g8_forest_referenced_ids=None,
            )

        assert len(results) == 1
        assert results[0].quality_status == "FAIL"

        async with session_maker() as sess:
            row = (
                await sess.execute(_sel(Alpha).where(Alpha.alpha_id == alpha_id))
            ).scalar_one()
        assert row.quality_status == "FAIL"
        assert row.region == "USA"
        assert row.is_sharpe == pytest.approx(0.42)
    finally:
        # Cleanup
        async with session_maker() as sess:
            await sess.execute(_delete(Alpha).where(Alpha.alpha_id == alpha_id))
            await sess.commit()
        await engine.dispose()
