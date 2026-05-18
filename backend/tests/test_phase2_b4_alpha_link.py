"""Phase 2 B4 — alpha.hypothesis_id population tests.

When state.current_hypothesis_id is set (Phase 2 path), every Alpha row
inserted by the workflow batch path or _incremental_save_alphas should
carry that FK in alphas.hypothesis_id. Legacy / level-1 / level-0 tasks
leave it NULL (alpha.hypothesis Text column carries the LLM summary).

These tests construct AlphaResult / pending_alpha objects directly and
exercise the persistence path end-to-end against live Postgres.
"""
from __future__ import annotations

import socket
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.models import (
    Alpha, AlphaFailure, ExperimentRun, Hypothesis, MiningTask,
)
from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.graph.nodes.persistence import _incremental_save_alphas
from backend.services.hypothesis_service import (
    HypothesisService, HypothesisCreateData,
)


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="Postgres not reachable on localhost:5433",
)

_TAG = f"_b4_{uuid.uuid4().hex[:8]}_"


@pytest_asyncio.fixture
async def session_and_seed():
    """Live Postgres session pre-seeded with a MiningTask + ExperimentRun +
    Hypothesis. Cleanup happens in test fixture teardown."""
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        task = MiningTask(
            task_name=f"{_TAG}task",
            region="USA", universe="TOP3000",
            dataset_strategy="AUTO",            status="RUNNING", daily_goal=4, max_iterations=2,
        )
        s.add(task)
        await s.flush()
        run = ExperimentRun(task_id=task.id, status="RUNNING")
        s.add(run)
        await s.flush()

        svc = HypothesisService(s)
        h = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}h1",
            rationale="b4 link test",
            region="USA",
            universe="TOP3000",        ))
        await s.commit()

        yield s, task, run, h

        # Cleanup
        try:
            await s.execute(delete(Alpha).where(Alpha.task_id == task.id))
            await s.execute(delete(AlphaFailure).where(AlphaFailure.task_id == task.id))
            await s.execute(delete(ExperimentRun).where(ExperimentRun.id == run.id))
            await s.execute(text("DELETE FROM mining_tasks WHERE id = :i"), {"i": task.id})
            await s.execute(delete(Hypothesis).where(Hypothesis.statement.like(f"{_TAG}%")))
            await s.commit()
        except Exception:
            await s.rollback()
    await engine.dispose()


def _candidate(expr: str, sharpe: float = 1.5, status: str = "PASS_PROVISIONAL") -> AlphaCandidate:
    # alpha_id column is VARCHAR(20). Use a compact 16-char id so we fit.
    aid = f"_b4{uuid.uuid4().hex[:13]}"
    assert len(aid) <= 20
    return AlphaCandidate(
        expression=expr,
        hypothesis="legacy text",
        explanation="b4 test",
        is_valid=True,
        is_simulated=True,
        simulation_success=True,
        alpha_id=aid,
        metrics={"sharpe": sharpe, "fitness": 0.5, "turnover": 0.3},
        quality_status=status,
    )


# =============================================================================
# Incremental persistence path (T2/T3)
# =============================================================================

@pytest.mark.asyncio
async def test_incremental_save_writes_hypothesis_id(session_and_seed):
    """T2/T3 path: _incremental_save_alphas must write hypothesis_id into
    each Alpha row when caller passes it."""
    s, task, run, h = session_and_seed

    pending = [
        _candidate("ts_rank(close, 5)", sharpe=1.8),
        _candidate("rank(returns)", sharpe=1.2),
    ]

    out = await _incremental_save_alphas(
        db_session=s,
        task_id=task.id,
        run_id=run.id,
        region="USA",
        universe="TOP3000",
        dataset_id="pv1",        pending_alphas=pending,
        hypothesis_id=h.id,
    )

    assert len(out) == 2
    for ar in out:
        assert ar.hypothesis_id == h.id
        assert ar.persisted is True

    # Verify DB rows
    r = await s.execute(
        select(Alpha.alpha_id, Alpha.hypothesis_id)
        .where(Alpha.task_id == task.id)
        .order_by(Alpha.id)
    )
    rows = r.fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row.hypothesis_id == h.id


@pytest.mark.asyncio
async def test_incremental_save_no_hypothesis_id_keeps_null(session_and_seed):
    """Legacy / level<2 path: hypothesis_id=None propagates to NULL in DB."""
    s, task, run, _h = session_and_seed

    pending = [_candidate("ts_zscore(close, 10)", sharpe=1.5)]

    out = await _incremental_save_alphas(
        db_session=s,
        task_id=task.id,
        run_id=run.id,
        region="USA",
        universe="TOP3000",
        dataset_id="pv1",        pending_alphas=pending,
        hypothesis_id=None,
    )

    assert out[0].hypothesis_id is None
    r = await s.execute(
        select(Alpha.hypothesis_id).where(Alpha.task_id == task.id)
    )
    row = r.fetchone()
    assert row.hypothesis_id is None


# =============================================================================
# state propagation in node_save_results
# =============================================================================

@pytest.mark.asyncio
async def test_node_save_results_passes_state_hypothesis_id(session_and_seed):
    """node_save_results must read state.current_hypothesis_id and propagate
    it to AlphaResult so workflow's batch INSERT writes it."""
    s, task, run, h = session_and_seed
    from backend.agents.graph.nodes.persistence import node_save_results

    state = MiningState(
        task_id=task.id,
        region="USA",
        universe="TOP3000",
        dataset_id="pv1",
        fields=[],
        operators=[],
        factor_tier=1,  # T1 → buffered path (NOT incremental)
        pending_alphas=[
            _candidate("rank(close)", sharpe=2.0, status="PASS"),
            _candidate("ts_delta(volume, 5)", sharpe=1.4, status="PASS_PROVISIONAL"),
            _candidate("rank(returns)", sharpe=-0.5, status="FAIL"),
        ],
        current_hypothesis_id=h.id,
    )

    config = {"configurable": {"trace_service": None}}
    result = await node_save_results(state, config)

    success_batch = result["generated_alphas"]
    # Two PASS / PROV alphas, one FAIL
    assert len(success_batch) == 2
    for ar in success_batch:
        assert ar.hypothesis_id == h.id
        # T1 buffered path — not yet persisted at this point
        assert ar.persisted is False


@pytest.mark.asyncio
async def test_node_save_results_legacy_path_no_hypothesis_id(session_and_seed):
    """When state.current_hypothesis_id is None (level<2), AlphaResult comes
    back with hypothesis_id=None. Legacy alpha.hypothesis Text column still
    populated."""
    s, task, run, _h = session_and_seed
    from backend.agents.graph.nodes.persistence import node_save_results

    state = MiningState(
        task_id=task.id,
        region="USA",
        universe="TOP3000",
        dataset_id="pv1",
        fields=[],
        operators=[],        pending_alphas=[_candidate("rank(close)", sharpe=2.0)],
        current_hypothesis_id=None,
    )
    config = {"configurable": {"trace_service": None}}
    result = await node_save_results(state, config)
    assert result["generated_alphas"][0].hypothesis_id is None
    # Legacy text column still populated
    assert result["generated_alphas"][0].hypothesis == "legacy text"
