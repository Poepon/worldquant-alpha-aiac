"""V-27.45 — hypothesis reuse TOCTOU guard.

RCA: docs/v27_backlog.md B 段. V-22.13 hypothesis reuse (generation.py) reads
hypothesis status in a since-closed session; a concurrent B5 mark_abandoned
may flip it to terminal (ABANDONED/SUPERSEDED) in the race window, leaving
this round's alphas linked to a terminal hypothesis. Fix: re-check at
alpha/failure INSERT time and drop the link (hypothesis_id → NULL) if
terminal.

Targets the real PostgreSQL DB. Run:
    pytest backend/tests/integration/test_v27_45_hypothesis_reuse_guard.py -v

Requires: PostgreSQL on POSTGRES_PORT (5433).
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.models import HypothesisStatus  # noqa: E402
from backend.services.hypothesis_service import (  # noqa: E402
    HypothesisService,
    HypothesisCreateData,
)

_TAG = f"v27-45-test-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_session():
    """Real-PG session per test. Cleans up _TAG-tagged rows after."""
    from sqlalchemy import delete, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from backend.config import settings
    from backend.models import Hypothesis, MiningTask

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        yield db
        async with Session() as cleanup:
            hids = (
                await cleanup.execute(
                    text("SELECT id FROM hypotheses WHERE statement LIKE :p"),
                    {"p": f"{_TAG}%"},
                )
            ).scalars().all()
            tids = (
                await cleanup.execute(
                    text("SELECT id FROM mining_tasks WHERE task_name LIKE :p"),
                    {"p": f"{_TAG}%"},
                )
            ).scalars().all()
            if tids:
                await cleanup.execute(
                    text("DELETE FROM alphas WHERE task_id = ANY(:t)"), {"t": tids}
                )
                await cleanup.execute(
                    text("DELETE FROM alpha_failures WHERE task_id = ANY(:t)"),
                    {"t": tids},
                )
                await cleanup.execute(
                    text("DELETE FROM experiment_runs WHERE task_id = ANY(:t)"),
                    {"t": tids},
                )
            if hids:
                await cleanup.execute(
                    text("DELETE FROM alphas WHERE hypothesis_id = ANY(:h)"),
                    {"h": hids},
                )
                await cleanup.execute(
                    delete(Hypothesis).where(Hypothesis.id.in_(hids))
                )
            if tids:
                await cleanup.execute(
                    delete(MiningTask).where(MiningTask.id.in_(tids))
                )
            await cleanup.commit()
    await engine.dispose()


async def _mk_hypothesis(svc, suffix, status):
    """Create a tagged hypothesis in the requested lifecycle status."""
    h = await svc.create_hypothesis(
        HypothesisCreateData(statement=f"{_TAG}-{suffix}", region="USA")
    )
    if status == "ABANDONED":
        await svc.mark_abandoned(h.id, reason=f"{_TAG} test abandon")
    elif status == "ACTIVE":
        await svc.mark_active(h.id)
    elif status == "SUPERSEDED":
        from sqlalchemy import update
        from backend.models import Hypothesis
        await svc.db.execute(
            update(Hypothesis)
            .where(Hypothesis.id == h.id)
            .values(status=HypothesisStatus.SUPERSEDED.value)
        )
    # PROPOSED — leave as created
    return h.id


async def _mk_task(pg_session, suffix):
    """Real task + run for the alphas FK."""
    from backend.models import MiningTask, ExperimentRun
    t = MiningTask(
        task_name=f"{_TAG}-task-{suffix}", region="USA", universe="TOP3000",
        dataset_strategy="AUTO", agent_mode="AUTONOMOUS_TIER1",
        status="RUNNING", daily_goal=4, max_iterations=2,
    )
    pg_session.add(t)
    await pg_session.flush()
    r = ExperimentRun(task_id=t.id, status="RUNNING")
    pg_session.add(r)
    await pg_session.flush()
    return t.id, r.id


def _mk_alpha(quality_status="PASS"):
    """A real AlphaCandidate — _incremental_save_alphas reads many fields
    (wrapper_kind, parent_alpha_id, …) so the typed model is safest."""
    from backend.agents.graph.state import AlphaCandidate
    aid = uuid.uuid4().hex[:18]
    return AlphaCandidate(
        expression=f"rank(close_{aid})",
        alpha_id=aid,
        quality_status=quality_status,
        is_valid=True,
        is_simulated=True,
        simulation_success=True,
        metrics={"sharpe": 1.5},
    )


# ---------------------------------------------------------------------------
# filter_terminal_ids
# ---------------------------------------------------------------------------

class TestFilterTerminalIds:
    @pytest.mark.asyncio
    async def test_empty_input(self, pg_session):
        svc = HypothesisService(pg_session)
        assert await svc.filter_terminal_ids([]) == set()

    @pytest.mark.asyncio
    async def test_mixed_statuses(self, pg_session):
        svc = HypothesisService(pg_session)
        proposed = await _mk_hypothesis(svc, "fti-proposed", "PROPOSED")
        active = await _mk_hypothesis(svc, "fti-active", "ACTIVE")
        abandoned = await _mk_hypothesis(svc, "fti-abandoned", "ABANDONED")
        superseded = await _mk_hypothesis(svc, "fti-superseded", "SUPERSEDED")
        await pg_session.commit()
        result = await svc.filter_terminal_ids(
            [proposed, active, abandoned, superseded]
        )
        assert result == {abandoned, superseded}

    @pytest.mark.asyncio
    async def test_nonexistent_id(self, pg_session):
        svc = HypothesisService(pg_session)
        assert await svc.filter_terminal_ids([99_999_999]) == set()


# ---------------------------------------------------------------------------
# _incremental_save_alphas — terminal link dropped at INSERT time
# ---------------------------------------------------------------------------

class TestIncrementalSaveTerminalGuard:
    @pytest.mark.asyncio
    async def test_abandoned_hypothesis_link_dropped(self, pg_session):
        from backend.agents.graph.nodes.persistence import _incremental_save_alphas
        from backend.models import Alpha
        from sqlalchemy import select

        svc = HypothesisService(pg_session)
        hid = await _mk_hypothesis(svc, "inc-aband", "ABANDONED")
        tid, rid = await _mk_task(pg_session, "inc-aband")
        await pg_session.commit()

        alphas = [_mk_alpha(), _mk_alpha()]
        await _incremental_save_alphas(
            db_session=pg_session, task_id=tid, run_id=rid,
            region="USA", universe="TOP3000", dataset_id="pv1",
            factor_tier=2, pending_alphas=alphas, hypothesis_id=hid,
        )
        await pg_session.commit()

        rows = (
            await pg_session.execute(select(Alpha).where(Alpha.task_id == tid))
        ).scalars().all()
        assert len(rows) == 2, "alpha rows must still land"
        assert all(r.hypothesis_id is None for r in rows), (
            "link to an ABANDONED hypothesis must be dropped to NULL"
        )

    @pytest.mark.asyncio
    async def test_active_hypothesis_link_kept(self, pg_session):
        from backend.agents.graph.nodes.persistence import _incremental_save_alphas
        from backend.models import Alpha
        from sqlalchemy import select

        svc = HypothesisService(pg_session)
        hid = await _mk_hypothesis(svc, "inc-active", "ACTIVE")
        tid, rid = await _mk_task(pg_session, "inc-active")
        await pg_session.commit()

        alphas = [_mk_alpha()]
        await _incremental_save_alphas(
            db_session=pg_session, task_id=tid, run_id=rid,
            region="USA", universe="TOP3000", dataset_id="pv1",
            factor_tier=2, pending_alphas=alphas, hypothesis_id=hid,
        )
        await pg_session.commit()

        rows = (
            await pg_session.execute(select(Alpha).where(Alpha.task_id == tid))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].hypothesis_id == hid, "ACTIVE hypothesis link must be kept"

    @pytest.mark.asyncio
    async def test_flag_off_keeps_terminal_link(self, pg_session, monkeypatch):
        from backend.config import settings
        from backend.agents.graph.nodes.persistence import _incremental_save_alphas
        from backend.models import Alpha
        from sqlalchemy import select

        monkeypatch.setattr(
            settings, "HYPOTHESIS_REUSE_TERMINAL_GUARD_ENABLED", False
        )
        svc = HypothesisService(pg_session)
        hid = await _mk_hypothesis(svc, "inc-flagoff", "ABANDONED")
        tid, rid = await _mk_task(pg_session, "inc-flagoff")
        await pg_session.commit()

        alphas = [_mk_alpha()]
        await _incremental_save_alphas(
            db_session=pg_session, task_id=tid, run_id=rid,
            region="USA", universe="TOP3000", dataset_id="pv1",
            factor_tier=2, pending_alphas=alphas, hypothesis_id=hid,
        )
        await pg_session.commit()

        rows = (
            await pg_session.execute(select(Alpha).where(Alpha.task_id == tid))
        ).scalars().all()
        assert len(rows) == 1
        # Flag off → guard is a no-op, the terminal link is kept (pre-fix).
        assert rows[0].hypothesis_id == hid
