"""Phase 1b B5 — pool scheduler core + beat gating tests."""
import random

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.database import SQLAlchemyBase
from backend.models import HypothesisIntent
from backend.pool import scheduler as sched
from backend.pool import stages as st


def test_weighted_pick_distinct_and_capped():
    cells = [
        {"region": "USA", "dataset_id": "pv1", "mining_weight": 5.0},
        {"region": "USA", "dataset_id": "analyst4", "mining_weight": 1.0},
        {"region": "USA", "dataset_id": "model16", "mining_weight": 0.1},
    ]
    rng = random.Random(0)
    picks = sched.weighted_pick(cells, 2, rng=rng)
    assert len(picks) == 2
    ids = [p["dataset_id"] for p in picks]
    assert len(set(ids)) == 2  # distinct (no replacement)
    # n > available → returns all available
    assert len(sched.weighted_pick(cells, 9, rng=random.Random(1))) == 3
    assert sched.weighted_pick([], 3) == []


def test_weighted_pick_degenerate_weights_uniform():
    cells = [{"dataset_id": "a", "mining_weight": 0.0},
             {"dataset_id": "b", "mining_weight": 0.0}]
    picks = sched.weighted_pick(cells, 2, rng=random.Random(0))
    assert {p["dataset_id"] for p in picks} == {"a", "b"}  # both picked, no starvation


def test_freeze_config_snapshot_shape():
    snap = sched.freeze_config_snapshot()
    role = snap["brain_role_snapshot"]
    assert "brain_consultant_mode_at_start" in role
    assert "effective_default_test_period" in role
    assert "effective_sharpe_submit_min" in role
    assert "effective_region_universes" in role
    assert snap["llm_overrides"] is None
    assert isinstance(snap["thresholds_version"], str)


@pytest.mark.asyncio
async def test_insert_intents_writes_pending_hyp_intent():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)
    try:
        picks = [
            {"region": "USA", "dataset_id": "pv1", "universe": "TOP3000", "delay": 1},
            {"region": "CHN", "dataset_id": "x", "universe": "TOP2000U", "delay": 0},
        ]
        snap = {"brain_role_snapshot": {}, "llm_overrides": None, "thresholds_version": "v1"}
        n = await sched.insert_intents(picks, snap, fanout=10, session_factory=sf)
        assert n == 2
        async with sf() as s:
            rows = (await s.execute(select(HypothesisIntent))).scalars().all()
        assert len(rows) == 2
        assert all(r.stage == st.INTENT_PENDING for r in rows)
        assert all(r.fanout == 10 for r in rows)
        by_region = {r.region: r for r in rows}
        assert by_region["USA"].dataset_id == "pv1" and by_region["USA"].delay == 1
        assert by_region["CHN"].delay == 0
        assert by_region["USA"].config_snapshot == snap
        assert by_region["USA"].thresholds_version == "v1"
    finally:
        await eng.dispose()


def test_beats_noop_when_flag_off():
    from backend.tasks.pool_tasks import run_pool_scheduler, run_pool_lease_recycle
    # ENABLE_POOL_PIPELINE defaults False → both beats skip (no DB/redis touched)
    assert run_pool_scheduler() == {"skipped": "ENABLE_POOL_PIPELINE OFF"}
    assert run_pool_lease_recycle() == {"skipped": "ENABLE_POOL_PIPELINE OFF"}
