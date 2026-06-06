"""V-27.92 — Hypothesis state machine single source of truth.

RCA: docs/rca_2026-05-14_v27_92_hypothesis_state_machine_dual_track.md — the
abandon decision used to read state.hypothesis_round_history (in-memory),
lost on worker restart / Celery task-boundary switch, so a hypothesis that
should have been abandoned stayed ACTIVE forever. Fix: a hypothesis_round_
stats table is the authoritative input; should_abandon_hypothesis reads it.

Covers V-27.92 (DB-backed abandon), V-27.71 (flip products excluded from
alpha_count), V-27.61 (retryable attempts excluded).

Targets the real PostgreSQL DB (the Hypothesis schema uses JSONB + the new
table; on_conflict upsert is a PG dialect feature). Each test tags its rows
and tears them down.

Run:
    pytest backend/tests/integration/test_v27_92_hypothesis_round_stats.py -v

Requires: PostgreSQL on POSTGRES_PORT (5433).
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.agents.graph.early_stop import should_abandon_hypothesis  # noqa: E402
from backend.services.hypothesis_service import (  # noqa: E402
    HypothesisService,
    HypothesisCreateData,
)

_TAG = f"v27-92-test-{uuid.uuid4().hex[:8]}"


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
                    text(
                        "SELECT id FROM hypotheses WHERE statement LIKE :p"
                    ),
                    {"p": f"{_TAG}%"},
                )
            ).scalars().all()
            tids = (
                await cleanup.execute(
                    text(
                        "SELECT id FROM mining_tasks WHERE task_name LIKE :p"
                    ),
                    {"p": f"{_TAG}%"},
                )
            ).scalars().all()
            if hids:
                # hypothesis_round_stats FK→hypotheses is ON DELETE CASCADE,
                # but delete explicitly first so the task delete below is free.
                await cleanup.execute(
                    text(
                        "DELETE FROM hypothesis_round_stats "
                        "WHERE hypothesis_id = ANY(:h)"
                    ),
                    {"h": hids},
                )
                await cleanup.execute(
                    text("DELETE FROM alpha_failures WHERE hypothesis_id = ANY(:h)"),
                    {"h": hids},
                )
                await cleanup.execute(
                    delete(Hypothesis).where(Hypothesis.id.in_(hids))
                )
            if tids:
                await cleanup.execute(
                    text(
                        "DELETE FROM hypothesis_round_stats "
                        "WHERE task_id = ANY(:t)"
                    ),
                    {"t": tids},
                )
                await cleanup.execute(
                    delete(MiningTask).where(MiningTask.id.in_(tids))
                )
            await cleanup.commit()
    await engine.dispose()


async def _seed(pg_session, suffix):
    """Create a tagged hypothesis + task, return (hypothesis_id, task_id)."""
    from backend.models import MiningTask

    svc = HypothesisService(pg_session)
    h = await svc.create_hypothesis(
        HypothesisCreateData(statement=f"{_TAG}-{suffix}", region="USA")
    )
    task = MiningTask(
        task_name=f"{_TAG}-task-{suffix}", region="USA", universe="TOP3000",
        dataset_strategy="AUTO",        status="RUNNING", daily_goal=4, 
    )
    pg_session.add(task)
    await pg_session.commit()
    await pg_session.refresh(task)
    return h.id, task.id


async def _upsert(svc, hid, tid, round_index, **kw):
    """Thin wrapper — defaults the boring count kwargs."""
    base = dict(
        alpha_count=3, pass_count=0, syntax_fail_count=0,
        simulate_fail_count=0, quality_fail_count=3,
        attribution="hypothesis",
    )
    base.update(kw)
    await svc.upsert_round_stats(
        hypothesis_id=hid, task_id=tid, round_index=round_index, **base
    )


# ---------------------------------------------------------------------------
# upsert_round_stats — insert + idempotent overwrite
# ---------------------------------------------------------------------------

class TestUpsertRoundStats:
    @pytest.mark.asyncio
    async def test_upsert_inserts_row(self, pg_session):
        from backend.models import HypothesisRoundStats
        from sqlalchemy import select

        hid, tid = await _seed(pg_session, "upsert-insert")
        svc = HypothesisService(pg_session)
        await _upsert(svc, hid, tid, 0, alpha_count=5, pass_count=2)
        await pg_session.commit()

        rows = (
            await pg_session.execute(
                select(HypothesisRoundStats).where(
                    HypothesisRoundStats.hypothesis_id == hid
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].alpha_count == 5
        assert rows[0].pass_count == 2
        assert rows[0].round_index == 0

    @pytest.mark.asyncio
    async def test_upsert_idempotent_on_replay(self, pg_session):
        # LangGraph can replay the same B5 round after a worker restart —
        # the same (hid, round, task) key must overwrite, not duplicate.
        from backend.models import HypothesisRoundStats
        from sqlalchemy import select

        hid, tid = await _seed(pg_session, "upsert-replay")
        svc = HypothesisService(pg_session)
        await _upsert(svc, hid, tid, 1, alpha_count=3, pass_count=0)
        await pg_session.commit()
        await _upsert(svc, hid, tid, 1, alpha_count=7, pass_count=1)
        await pg_session.commit()

        rows = (
            await pg_session.execute(
                select(HypothesisRoundStats).where(
                    HypothesisRoundStats.hypothesis_id == hid
                )
            )
        ).scalars().all()
        assert len(rows) == 1, "replay must overwrite, not duplicate"
        assert rows[0].alpha_count == 7
        assert rows[0].pass_count == 1


# ---------------------------------------------------------------------------
# should_abandon_hypothesis — reads the DB table
# ---------------------------------------------------------------------------

class TestShouldAbandonFromDB:
    @pytest.mark.asyncio
    async def test_window_insufficient(self, pg_session):
        hid, tid = await _seed(pg_session, "abandon-short")
        svc = HypothesisService(pg_session)
        await _upsert(svc, hid, tid, 0)
        await _upsert(svc, hid, tid, 1)
        await pg_session.commit()
        assert await should_abandon_hypothesis(pg_session, hypothesis_id=hid) == (
            False, None,
        )

    @pytest.mark.asyncio
    async def test_triggers_after_n_hypothesis_fail_rounds(self, pg_session):
        hid, tid = await _seed(pg_session, "abandon-trigger")
        svc = HypothesisService(pg_session)
        for r in (0, 1, 2):
            await _upsert(svc, hid, tid, r)  # 3 alphas, 0 pass, attribution=hypothesis
        await pg_session.commit()
        abandon, reason = await should_abandon_hypothesis(
            pg_session, hypothesis_id=hid
        )
        assert abandon is True
        assert "3 consecutive rounds" in reason

    @pytest.mark.asyncio
    async def test_skip_when_has_pass(self, pg_session):
        hid, tid = await _seed(pg_session, "abandon-haspass")
        svc = HypothesisService(pg_session)
        await _upsert(svc, hid, tid, 0)
        await _upsert(svc, hid, tid, 1, pass_count=1, quality_fail_count=2)
        await _upsert(svc, hid, tid, 2)
        await pg_session.commit()
        assert await should_abandon_hypothesis(pg_session, hypothesis_id=hid) == (
            False, None,
        )

    @pytest.mark.asyncio
    async def test_skip_when_non_hypothesis_attribution(self, pg_session):
        hid, tid = await _seed(pg_session, "abandon-impl")
        svc = HypothesisService(pg_session)
        await _upsert(svc, hid, tid, 0)
        await _upsert(svc, hid, tid, 1, attribution="implementation")
        await _upsert(svc, hid, tid, 2)
        await pg_session.commit()
        assert await should_abandon_hypothesis(pg_session, hypothesis_id=hid) == (
            False, None,
        )

    @pytest.mark.asyncio
    async def test_v27_68_empty_round_guard(self, pg_session):
        # A round that tested NOTHING (0 real AND 0 flip — e.g. a
        # retryable-only round or an all-dedup round) never tested the
        # hypothesis and must not count as a failure round.
        # V-27.92 followup: "empty" requires flip_alpha_count==0 too.
        hid, tid = await _seed(pg_session, "abandon-empty")
        svc = HypothesisService(pg_session)
        await _upsert(svc, hid, tid, 0)
        await _upsert(svc, hid, tid, 1, alpha_count=0, quality_fail_count=0,
                      flip_alpha_count=0, retryable_count=4)
        await _upsert(svc, hid, tid, 2)
        await pg_session.commit()
        assert await should_abandon_hypothesis(pg_session, hypothesis_id=hid) == (
            False, None,
        )

    @pytest.mark.asyncio
    async def test_flip_only_round_counts_toward_abandon(self, pg_session):
        # V-27.92 followup (flip-only 轮): a flip-only round (real=0, flip>0)
        # DID test the hypothesis — it found the stated direction wrong. It
        # must NOT be masked by the empty-round guard; 3 such rounds with 0
        # real PASS + attribution=hypothesis → abandon.
        hid, tid = await _seed(pg_session, "abandon-fliponly")
        svc = HypothesisService(pg_session)
        for r in (0, 1, 2):
            await _upsert(svc, hid, tid, r, alpha_count=0, quality_fail_count=0,
                          flip_alpha_count=4, flip_pass_count=4)
        await pg_session.commit()
        abandon, reason = await should_abandon_hypothesis(
            pg_session, hypothesis_id=hid
        )
        assert abandon is True
        assert reason

    @pytest.mark.asyncio
    async def test_survives_memory_loss(self, pg_session):
        # The whole point of V-27.92: rows written directly to the table
        # (no in-memory history accumulation at all, as after a worker
        # restart) still drive a correct abandon decision.
        hid, tid = await _seed(pg_session, "abandon-memloss")
        svc = HypothesisService(pg_session)
        for r in (5, 6, 7):  # non-zero start round_index — restart resumed mid-run
            await _upsert(svc, hid, tid, r)
        await pg_session.commit()
        abandon, _ = await should_abandon_hypothesis(pg_session, hypothesis_id=hid)
        assert abandon is True

    @pytest.mark.asyncio
    async def test_cross_session_rows_visible(self, pg_session):
        # V-20.1 prefetch round runs in an isolated session — rows it writes
        # must still be seen by should_abandon on another session.
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from backend.config import settings

        hid, tid = await _seed(pg_session, "abandon-xsession")

        engine2 = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
        Session2 = async_sessionmaker(engine2, expire_on_commit=False)
        try:
            async with Session2() as other:
                svc_other = HypothesisService(other)
                await _upsert(svc_other, hid, tid, 0)
                await _upsert(svc_other, hid, tid, 1)
                await other.commit()
        finally:
            await engine2.dispose()

        # round 2 written on the fixture session
        svc = HypothesisService(pg_session)
        await _upsert(svc, hid, tid, 2)
        await pg_session.commit()

        abandon, _ = await should_abandon_hypothesis(pg_session, hypothesis_id=hid)
        assert abandon is True, "rows from the other session must be visible"


# ---------------------------------------------------------------------------
# _process_hypothesis_feedback — end to end (flip / retryable exclusion,
# clean-count lifecycle, DB-backed abandon)
# ---------------------------------------------------------------------------

def _mk_alpha(
    quality_status="FAIL", *, is_valid=True, is_simulated=True,
    simulation_success=True, metrics=None, metadata=None,
):
    """Duck-typed alpha — _process_hypothesis_feedback only reads these."""
    return SimpleNamespace(
        quality_status=quality_status,
        is_valid=is_valid,
        is_simulated=is_simulated,
        simulation_success=simulation_success,
        metrics=metrics if metrics is not None else {},
        metadata=metadata if metadata is not None else {},
        expression="rank(close)",
        alpha_id="t",
        hypothesis="",
    )


class TestProcessHypothesisFeedback:
    @pytest.mark.asyncio
    async def test_flip_alpha_excluded_from_alpha_count(self, pg_session):
        from backend.agents.graph.nodes.persistence import _process_hypothesis_feedback
        from backend.models import HypothesisRoundStats
        from sqlalchemy import select

        hid, tid = await _seed(pg_session, "feedback-flip")
        state = SimpleNamespace(
            current_hypothesis_ids=[hid], current_hypothesis_id=hid, task_id=tid,
        )
        pending = [
            _mk_alpha("FAIL"), _mk_alpha("FAIL"),                       # 2 real
            _mk_alpha("PASS", metadata={"flipped": True}),              # flip
            _mk_alpha("PASS", metadata={"flipped": True}),              # flip
            _mk_alpha("PASS", metadata={"flipped": True}),              # flip
        ]
        await _process_hypothesis_feedback(
            state=state, round_index=0, pending_alphas=pending,
            history_so_far={}, llm_service=None,
        )
        row = (
            await pg_session.execute(
                select(HypothesisRoundStats).where(
                    HypothesisRoundStats.hypothesis_id == hid
                )
            )
        ).scalars().one()
        assert row.alpha_count == 2, "flip products must not be in alpha_count"
        assert row.flip_alpha_count == 3
        assert row.flip_pass_count == 3

    @pytest.mark.asyncio
    async def test_retryable_alpha_excluded_from_alpha_count(self, pg_session):
        from backend.agents.graph.nodes.persistence import _process_hypothesis_feedback
        from backend.models import HypothesisRoundStats
        from sqlalchemy import select

        hid, tid = await _seed(pg_session, "feedback-retry")
        state = SimpleNamespace(
            current_hypothesis_ids=[hid], current_hypothesis_id=hid, task_id=tid,
        )
        pending = [
            _mk_alpha("FAIL"), _mk_alpha("FAIL"),                          # 2 real
            _mk_alpha("PENDING", is_simulated=False,
                      metrics={"_sim_retryable": True}),                   # retryable
            _mk_alpha("PENDING", is_simulated=False,
                      metrics={"_sim_retryable": True}),                   # retryable
        ]
        await _process_hypothesis_feedback(
            state=state, round_index=0, pending_alphas=pending,
            history_so_far={}, llm_service=None,
        )
        row = (
            await pg_session.execute(
                select(HypothesisRoundStats).where(
                    HypothesisRoundStats.hypothesis_id == hid
                )
            )
        ).scalars().one()
        assert row.alpha_count == 2, "retryable attempts must not be in alpha_count"
        assert row.retryable_count == 2

    @pytest.mark.asyncio
    async def test_three_hypothesis_fail_rounds_abandon(self, pg_session):
        from backend.agents.graph.nodes.persistence import _process_hypothesis_feedback
        from backend.models import Hypothesis, HypothesisStatus

        hid, tid = await _seed(pg_session, "feedback-abandon")
        state = SimpleNamespace(
            current_hypothesis_ids=[hid], current_hypothesis_id=hid, task_id=tid,
        )
        # 3 rounds, each 3 real quality-FAIL alphas → attribution=hypothesis.
        for r in (0, 1, 2):
            pending = [_mk_alpha("FAIL") for _ in range(3)]
            await _process_hypothesis_feedback(
                state=state, round_index=r, pending_alphas=pending,
                history_so_far={}, llm_service=None,
            )
        h = await pg_session.get(Hypothesis, hid)
        await pg_session.refresh(h)
        assert h.status == HypothesisStatus.ABANDONED.value

    @pytest.mark.asyncio
    async def test_flip_only_round_marks_active_not_promoted(self, pg_session):
        # V-27.92 followup (flip-only 轮): a round that produced only flip
        # products DID test the hypothesis (it found the stated direction
        # wrong) → mark_active fires, hypothesis leaves PROPOSED. It is NOT
        # promoted — flip is implementation salvage, not vindication of the
        # stated direction (V-27.71 still holds).
        from backend.agents.graph.nodes.persistence import _process_hypothesis_feedback
        from backend.models import Hypothesis, HypothesisRoundStats, HypothesisStatus
        from sqlalchemy import select

        hid, tid = await _seed(pg_session, "feedback-fl:active")
        state = SimpleNamespace(
            current_hypothesis_ids=[hid], current_hypothesis_id=hid, task_id=tid,
        )
        pending = [
            _mk_alpha("PASS", metadata={"flipped": True}),
            _mk_alpha("PASS", metadata={"flipped": True}),
        ]
        await _process_hypothesis_feedback(
            state=state, round_index=0, pending_alphas=pending,
            history_so_far={}, llm_service=None,
        )
        h = await pg_session.get(Hypothesis, hid)
        await pg_session.refresh(h)
        # ACTIVE (tried), NOT PROMOTED (flip is salvage, not vindication).
        assert h.status == HypothesisStatus.ACTIVE.value
        # Flip-only round → attribution explicitly "hypothesis" (stated
        # direction failed), not an LLM classification on an empty list.
        row = (
            await pg_session.execute(
                select(HypothesisRoundStats).where(
                    HypothesisRoundStats.hypothesis_id == hid
                )
            )
        ).scalars().one()
        assert row.alpha_count == 0 and row.flip_alpha_count == 2
        assert row.attribution == "hypothesis"
