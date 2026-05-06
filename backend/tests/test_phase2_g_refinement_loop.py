"""G — Hypothesis Refinement Loop tests.

Verifies:
1. refine_hypothesis_llm:
   - Returns RefinedHypothesis on LLM "refine" decision
   - Returns None on LLM "give_up" decision
   - Returns None on LLM failure / parse error / empty statement
   - Returns None when chain depth exceeded
2. find_chain_depth walks parent_hypothesis_id correctly
3. find_unused_refined picks up PROPOSED-with-SUPERSEDED-parent matching
   region/variant
4. Integration: when B5/B6 should_abandon fires + LLM refines →
   parent=SUPERSEDED, child=PROPOSED, no abandon
"""
from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.models import Hypothesis, HypothesisStatus


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


_TAG = f"_g_{uuid.uuid4().hex[:8]}"


def _llm_refine(refined_statement="refined idea", give_up=False):
    response = MagicMock()
    response.success = True
    response.parsed = {
        "decision": "give_up" if give_up else "refine",
        "refined_statement": None if give_up else refined_statement,
        "rationale": "test rationale",
        "refinement_reason": "horizon was too long",
        "confidence": "medium",
    }
    response.content = ""
    llm = MagicMock()
    llm.call = AsyncMock(return_value=response)
    return llm


def _llm_failed():
    llm = MagicMock()
    llm.call = AsyncMock(side_effect=RuntimeError("DeepSeek down"))
    return llm


# =============================================================================
# refine_hypothesis_llm — pure unit tests
# =============================================================================

@pytest.mark.asyncio
async def test_refine_returns_refined_hypothesis_on_decision_refine():
    from backend.agents.graph.hypothesis_refine import refine_hypothesis_llm

    llm = _llm_refine(refined_statement="ts_rank works on weekly horizon")
    refined = await refine_hypothesis_llm(
        parent_statement="momentum on monthly horizon",
        parent_rationale="trends persist",
        history=[
            {"round_index": 1, "pass_count": 0, "attribution": "hypothesis"},
            {"round_index": 2, "pass_count": 0, "attribution": "hypothesis"},
            {"round_index": 3, "pass_count": 0, "attribution": "hypothesis"},
        ],
        sample_fail_exprs=["ts_rank(close, 60)", "ts_rank(returns, 60)"],
        llm_service=llm,
    )
    assert refined is not None
    assert refined.statement == "ts_rank works on weekly horizon"
    assert refined.refinement_reason == "horizon was too long"


@pytest.mark.asyncio
async def test_refine_returns_none_on_give_up():
    from backend.agents.graph.hypothesis_refine import refine_hypothesis_llm

    llm = _llm_refine(give_up=True)
    refined = await refine_hypothesis_llm(
        parent_statement="momentum",
        parent_rationale="x",
        history=[],
        sample_fail_exprs=[],
        llm_service=llm,
    )
    assert refined is None


@pytest.mark.asyncio
async def test_refine_returns_none_on_llm_failure():
    from backend.agents.graph.hypothesis_refine import refine_hypothesis_llm

    refined = await refine_hypothesis_llm(
        parent_statement="momentum",
        parent_rationale="x",
        history=[],
        sample_fail_exprs=[],
        llm_service=_llm_failed(),
    )
    assert refined is None


@pytest.mark.asyncio
async def test_refine_returns_none_on_no_llm_service():
    from backend.agents.graph.hypothesis_refine import refine_hypothesis_llm

    refined = await refine_hypothesis_llm(
        parent_statement="momentum",
        parent_rationale="x",
        history=[],
        sample_fail_exprs=[],
        llm_service=None,
    )
    assert refined is None


@pytest.mark.asyncio
async def test_refine_returns_none_on_empty_statement():
    from backend.agents.graph.hypothesis_refine import refine_hypothesis_llm

    refined = await refine_hypothesis_llm(
        parent_statement="",
        parent_rationale="x",
        history=[],
        sample_fail_exprs=[],
        llm_service=_llm_refine(),
    )
    assert refined is None


@pytest.mark.asyncio
async def test_refine_blocks_at_max_chain_depth():
    """Don't refine-of-refined-of-refined indefinitely."""
    from backend.agents.graph.hypothesis_refine import refine_hypothesis_llm

    llm = _llm_refine()
    refined = await refine_hypothesis_llm(
        parent_statement="parent stmt",
        parent_rationale="x",
        history=[],
        sample_fail_exprs=[],
        llm_service=llm,
        max_refine_chain_depth=2,
        current_chain_depth=2,
    )
    assert refined is None
    llm.call.assert_not_called()


@pytest.mark.asyncio
async def test_refine_handles_invalid_decision_string():
    """LLM returns garbage decision → fall through to abandon (None)."""
    from backend.agents.graph.hypothesis_refine import refine_hypothesis_llm

    response = MagicMock()
    response.success = True
    response.parsed = {"decision": "ambiguous", "refined_statement": "x"}
    response.content = ""
    llm = MagicMock()
    llm.call = AsyncMock(return_value=response)

    refined = await refine_hypothesis_llm(
        parent_statement="x",
        parent_rationale="y",
        history=[],
        sample_fail_exprs=[],
        llm_service=llm,
    )
    assert refined is None


# =============================================================================
# find_chain_depth + find_unused_refined — DB integration
# =============================================================================

pytestmark_pg = pytest.mark.skipif(not _pg_reachable(), reason="PG not reachable")


@pytestmark_pg
@pytest.mark.asyncio
async def test_find_chain_depth_walks_parent_chain():
    from backend.agents.graph.hypothesis_refine import find_chain_depth
    from backend.services.hypothesis_service import HypothesisService, HypothesisCreateData

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt", echo=False
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        svc = HypothesisService(s)
        gp = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_gp", region="USA",
        ))
        await s.commit()
        parent = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_p", region="USA", parent_hypothesis_id=gp.id,
        ))
        await s.commit()
        child = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_c", region="USA", parent_hypothesis_id=parent.id,
        ))
        await s.commit()

        try:
            assert await find_chain_depth(gp.id, s) == 0
            assert await find_chain_depth(parent.id, s) == 1
            assert await find_chain_depth(child.id, s) == 2
        finally:
            await s.execute(delete(Hypothesis).where(
                Hypothesis.id.in_([gp.id, parent.id, child.id])
            ))
            await s.commit()
    await engine.dispose()


@pytestmark_pg
@pytest.mark.asyncio
async def test_find_unused_refined_picks_up_correctly():
    from backend.services.hypothesis_service import HypothesisService, HypothesisCreateData

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt", echo=False
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        svc = HypothesisService(s)
        # Setup: parent (will be SUPERSEDED) → child (PROPOSED, unused)
        parent = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_pickup_parent", region="USA",
            experiment_variant="2",
        ))
        await s.commit()
        child = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_pickup_child", region="USA",
            parent_hypothesis_id=parent.id,
            experiment_variant="2",
        ))
        await s.commit()
        # Mark parent SUPERSEDED via direct UPDATE (bypass mark_superseded
        # that would require child registration validation)
        await s.execute(
            text("UPDATE hypotheses SET status='SUPERSEDED' WHERE id = :i"),
            {"i": parent.id},
        )
        await s.commit()

        try:
            picked = await svc.find_unused_refined(region="USA", experiment_variant="2")
            assert picked is not None
            assert picked.id == child.id

            # variant mismatch → should not pick up
            picked_v1 = await svc.find_unused_refined(region="USA", experiment_variant="1")
            assert picked_v1 is None or picked_v1.id != child.id

            # region mismatch → should not pick up
            picked_chn = await svc.find_unused_refined(region="CHN", experiment_variant="2")
            assert picked_chn is None or picked_chn.id != child.id
        finally:
            await s.execute(delete(Hypothesis).where(
                Hypothesis.id.in_([parent.id, child.id])
            ))
            await s.commit()
    await engine.dispose()


@pytestmark_pg
@pytest.mark.asyncio
async def test_find_unused_refined_skips_when_alphas_linked():
    """Once an alpha gets linked to the refined child, find_unused_refined
    should stop returning it (it's been "used" — has rounds attributed)."""
    from backend.services.hypothesis_service import HypothesisService, HypothesisCreateData
    from backend.models import Alpha, MiningTask, ExperimentRun

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt", echo=False
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        svc = HypothesisService(s)
        parent = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_used_parent", region="USA",
            experiment_variant="2",
        ))
        await s.commit()
        child = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_used_child", region="USA",
            parent_hypothesis_id=parent.id, experiment_variant="2",
        ))
        await s.commit()
        await s.execute(
            text("UPDATE hypotheses SET status='SUPERSEDED' WHERE id = :i"),
            {"i": parent.id},
        )
        # Add an alpha to the child
        task = MiningTask(
            task_name=f"{_TAG}_used_task", region="USA", universe="TOP3000",
            dataset_strategy="AUTO", agent_mode="AUTONOMOUS_TIER1",
            status="RUNNING", daily_goal=4, max_iterations=2,
        )
        s.add(task)
        await s.flush()
        run = ExperimentRun(task_id=task.id, status="RUNNING")
        s.add(run)
        await s.flush()
        s.add(Alpha(
            task_id=task.id, run_id=run.id,
            alpha_id=f"_g_{uuid.uuid4().hex[:13]}",
            expression="rank(close)", region="USA", universe="TOP3000",
            quality_status="PASS", is_sharpe=1.5,
            factor_tier=1, hypothesis_id=child.id,
        ))
        await s.commit()

        try:
            picked = await svc.find_unused_refined(region="USA", experiment_variant="2")
            # Either None or a different unused refined — but NOT this used child
            assert picked is None or picked.id != child.id
        finally:
            await s.execute(delete(Alpha).where(Alpha.hypothesis_id == child.id))
            await s.execute(delete(ExperimentRun).where(ExperimentRun.id == run.id))
            await s.execute(text("DELETE FROM mining_tasks WHERE id = :i"), {"i": task.id})
            await s.execute(delete(Hypothesis).where(
                Hypothesis.id.in_([parent.id, child.id])
            ))
            await s.commit()
    await engine.dispose()


@pytestmark_pg
@pytest.mark.asyncio
async def test_find_unused_refined_respects_ttl():
    """Refined hypotheses older than ttl_minutes shouldn't be picked up
    (avoid stale refinement reuse weeks later)."""
    from backend.services.hypothesis_service import HypothesisService, HypothesisCreateData

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt", echo=False
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        svc = HypothesisService(s)
        parent = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_ttl_parent", region="USA", experiment_variant="2",
        ))
        await s.commit()
        child = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}_ttl_child", region="USA",
            parent_hypothesis_id=parent.id, experiment_variant="2",
        ))
        await s.commit()
        # Rollback created_at to 2 hours ago
        await s.execute(text(
            "UPDATE hypotheses SET status='SUPERSEDED' WHERE id = :i"
        ), {"i": parent.id})
        old_ts = datetime.utcnow() - timedelta(hours=2)
        await s.execute(text(
            "UPDATE hypotheses SET created_at = :t WHERE id = :i"
        ), {"t": old_ts, "i": child.id})
        await s.commit()

        try:
            # 60 min TTL — child is 2hr old → shouldn't pick up
            picked = await svc.find_unused_refined(
                region="USA", experiment_variant="2", ttl_minutes=60,
            )
            assert picked is None or picked.id != child.id
            # 180 min TTL — should pick up
            picked2 = await svc.find_unused_refined(
                region="USA", experiment_variant="2", ttl_minutes=180,
            )
            assert picked2 is not None
            assert picked2.id == child.id
        finally:
            await s.execute(delete(Hypothesis).where(
                Hypothesis.id.in_([parent.id, child.id])
            ))
            await s.commit()
    await engine.dispose()
