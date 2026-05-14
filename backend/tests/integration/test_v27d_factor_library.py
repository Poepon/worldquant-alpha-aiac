"""V-27 backlog D 段 — factor_library 性能 / 脆弱性收尾.

Covers V-27.154 (submittable self_corr partial expression index) and
V-27.155 (refresh_iqc_batch eta computation). Targets the real PostgreSQL DB.

Run:
    pytest backend/tests/integration/test_v27d_factor_library.py -v

Requires: PostgreSQL on POSTGRES_PORT (5433).
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

os.environ.setdefault("POSTGRES_PORT", "5433")

_TAG = f"v27d-test-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_session():
    """Real-PG session per test. Cleans up _TAG-tagged rows after."""
    from sqlalchemy import delete, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from backend.config import settings
    from backend.models import Alpha, MiningTask

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        yield db
        async with Session() as cleanup:
            tids = (
                await cleanup.execute(
                    text("SELECT id FROM mining_tasks WHERE task_name LIKE :p"),
                    {"p": f"{_TAG}%"},
                )
            ).scalars().all()
            if tids:
                await cleanup.execute(delete(Alpha).where(Alpha.task_id.in_(tids)))
                await cleanup.execute(
                    delete(MiningTask).where(MiningTask.id.in_(tids))
                )
                await cleanup.commit()
    await engine.dispose()


# ---------------------------------------------------------------------------
# V-27.154 — submittable self_corr partial expression index
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_self_corr_partial_index_exists(pg_session):
    from sqlalchemy import text

    indexdef = (
        await pg_session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'ix_alphas_submittable_self_corr'"
            )
        )
    ).scalar()
    assert indexdef is not None, (
        "V-27.154 partial index missing — run `alembic upgrade head`"
    )
    # the expression + the partial WHERE clause both matter for planner use
    assert "_self_corr" in indexdef
    assert "can_submit IS TRUE" in indexdef
    assert "date_submitted IS NULL" in indexdef


# ---------------------------------------------------------------------------
# V-27.155 — refresh_iqc_batch eta = last queued countdown, not enqueued*2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_iqc_eta_uses_last_countdown(pg_session, monkeypatch):
    from backend.models import Alpha, MiningTask
    from backend.routers.factor_library import refresh_iqc_batch
    import backend.tasks.refresh_tasks as rt

    # Seed >=3 can_submit=True, unsubmitted alphas so the batch is non-empty.
    task = MiningTask(
        task_name=f"{_TAG}-task", region="ZZ1", universe="TOP3000",
        dataset_strategy="AUTO", agent_mode="AUTONOMOUS_TIER1",
        status="RUNNING", daily_goal=4, max_iterations=2,
    )
    pg_session.add(task)
    await pg_session.flush()
    for k in range(3):
        pg_session.add(Alpha(
            alpha_id=f"{_TAG[:10]}{k}", task_id=task.id,
            expression=f"rank(close_{k})", expression_hash=uuid.uuid4().hex,
            region="ZZ1", universe="TOP3000", status="created",
            quality_status="PASS", human_feedback="NONE",
            can_submit=True, date_submitted=None, is_sharpe=1.0 + k, metrics={},
        ))
    await pg_session.commit()

    # Capture the countdown of every apply_async call (fire-and-forget mock).
    countdowns: list = []

    def _fake_apply_async(args=None, countdown=None, **kw):
        countdowns.append(countdown)

    monkeypatch.setattr(
        rt.audit_iqc_marginal_for_alpha, "apply_async", _fake_apply_async
    )

    resp = await refresh_iqc_batch(
        scope="all_can_submit", limit=500, db=pg_session
    )
    assert countdowns, "expected at least the 3 seeded alphas to be enqueued"

    # V-27.155: eta is the LAST queued task's real countdown (max i*2), not
    # `enqueued * 2`. With all enqueues succeeding, countdowns are
    # [0, 2, 4, …, (n-1)*2] — the fix reports (n-1)*2; the old `enqueued*2`
    # would have over-reported n*2.
    expected_eta = max(countdowns)
    assert expected_eta == (len(countdowns) - 1) * 2
    assert f"约 {expected_eta}s" in resp.message, resp.message
    # the old buggy value would have been len(countdowns)*2
    assert f"约 {len(countdowns) * 2}s" not in resp.message
