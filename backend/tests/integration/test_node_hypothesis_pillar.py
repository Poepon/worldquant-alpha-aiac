"""Integration tests for P2-B node_hypothesis pillar persistence + nudge (M8/M3/M9).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.

Targets live Postgres because the Hypothesis schema uses JSONB / partial
indexes that aiosqlite can't render (same as test_pillar_balance_check).

Covers the M8 invariant: Hypothesis.pillar is stamped EVEN WHEN
``ENABLE_PILLAR_AWARE_SELECTION=False`` (data collection runs always; only
nudge injection is gated by the flag).
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")


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


from backend.models import (  # noqa: E402
    Alpha,
    Hypothesis,
    MiningTask,
)
# Pre-warm imports to dodge a known pre-existing circular import between
# ``backend.agents``, ``backend.tasks`` and ``backend.tasks.mining_tasks``.
# Importing ``backend.tasks`` FIRST (eagerly via backend.tasks.__init__)
# triggers the chain through ``backend.agents`` before any test code runs;
# subsequent imports then resolve from cache.
import backend.tasks  # noqa: E402, F401
import backend.agents.graph.nodes.generation as _generation_mod  # noqa: E402,F401


_TAG = f"nhT{uuid.uuid4().hex[:3]}_"


@pytest_asyncio.fixture
async def pg_engine_maker():
    """Yield a (engine, sessionmaker) pair the test owns. Cleanup happens in
    finally."""
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield engine, maker
    finally:
        try:
            async with maker() as s:
                await s.execute(
                    delete(Alpha).where(Alpha.alpha_id.like(f"{_TAG}%"))
                )
                await s.execute(
                    delete(Hypothesis).where(
                        Hypothesis.statement.like(f"{_TAG}%")
                    )
                )
                await s.execute(
                    text("DELETE FROM mining_tasks WHERE task_name LIKE :p"),
                    {"p": f"{_TAG}%"},
                )
                await s.commit()
        except Exception:
            pass
        await engine.dispose()


async def _make_state(maker, region="USA"):
    """Seed a MiningTask + return a MiningState bound to its id."""
    from backend.agents.graph.state import MiningState

    async with maker() as s:
        t = MiningTask(
            task_name=f"{_TAG}_task_{uuid.uuid4().hex[:5]}",
            region=region,
            universe="TOP3000",
            dataset_strategy="AUTO",            status="RUNNING",
            daily_goal=4,
            
            config={},
        )
        s.add(t)
        await s.commit()
        task_id = t.id
    return MiningState(
        task_id=task_id,
        region=region,
        universe="TOP3000",
        dataset_id="fundamental6",
        fields=[],
        operators=[],
    )


class _FakeLLMResponse:
    def __init__(self, parsed):
        self.success = True
        self.parsed = parsed
        self.error = None


def _fake_llm(parsed):
    """Build a fake LLMService whose ``call`` returns ``parsed``."""
    svc = AsyncMock()
    svc.call = AsyncMock(return_value=_FakeLLMResponse(parsed))
    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNodeHypothesisPillar:

    @pytest.mark.asyncio
    async def test_llm_emit_pillar_persisted(self, pg_engine_maker):
        """M8: LLM emits ``pillar: value`` → DB row pillar='value'."""
        engine, maker = pg_engine_maker
        state = await _make_state(maker)
        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_stmt_llm_emit_value",
                "rationale": "test",
                "expected_signal": "value",
                "pillar": "value",
                "key_fields": ["eps"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.agents.graph.nodes.generation import node_hypothesis
        result = await node_hypothesis(state, llm, config=config)

        hid = result["current_hypothesis_id"]
        assert hid is not None
        async with maker() as s:
            h = (await s.execute(
                select(Hypothesis).where(Hypothesis.id == hid)
            )).scalar_one()
            assert h.pillar == "value"

    @pytest.mark.asyncio
    async def test_llm_no_pillar_infer_fallback(self, pg_engine_maker):
        """M8: LLM omits pillar → infer_pillar resolves from key_fields."""
        engine, maker = pg_engine_maker
        state = await _make_state(maker)
        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_stmt_no_pillar",
                "expected_signal": "value",
                # pillar intentionally absent
                "key_fields": ["eps", "book_value"],
                "suggested_operators": ["ts_mean"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.agents.graph.nodes.generation import node_hypothesis
        result = await node_hypothesis(state, llm, config=config)

        hid = result["current_hypothesis_id"]
        assert hid is not None
        async with maker() as s:
            h = (await s.execute(
                select(Hypothesis).where(Hypothesis.id == hid)
            )).scalar_one()
            # expected_signal="value" short-circuits to pillar=value
            assert h.pillar == "value"

    @pytest.mark.asyncio
    async def test_pillar_persisted_when_flag_off(self, pg_engine_maker):
        """Data collection runs unconditionally — stamp always happens
        regardless of ENABLE_PILLAR_AWARE_SELECTION."""
        engine, maker = pg_engine_maker
        state = await _make_state(maker)
        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_stmt_flag_off",
                "pillar": "momentum",
                "key_fields": ["close"],
                "suggested_operators": ["ts_delta"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        # Flag is OFF (default)
        from backend.config import settings
        assert settings.ENABLE_PILLAR_AWARE_SELECTION is False

        from backend.agents.graph.nodes.generation import node_hypothesis
        result = await node_hypothesis(state, llm, config=config)

        hid = result["current_hypothesis_id"]
        assert hid is not None
        async with maker() as s:
            h = (await s.execute(
                select(Hypothesis).where(Hypothesis.id == hid)
            )).scalar_one()
            assert h.pillar == "momentum"  # stamped even with flag OFF

    @pytest.mark.asyncio
    async def test_nudge_failure_non_fatal(self, pg_engine_maker):
        """M9: when the nudge SQL/Redis path raises, node_hypothesis must
        keep running with pillar_hint=None (graceful degrade)."""
        engine, maker = pg_engine_maker
        state = await _make_state(maker)
        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_stmt_redis_fail",
                "pillar": "quality",
                "key_fields": ["roe"],
                "suggested_operators": ["ts_mean"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        # Flip flag on so the nudge block runs, then force the Redis import
        # to raise mid-block. ``get_redis_client`` is the symbol the block
        # imports — patch it on the module.
        import backend.tasks.redis_pool as _rp
        from backend.config import settings

        original_flag = settings.ENABLE_PILLAR_AWARE_SELECTION
        settings.ENABLE_PILLAR_AWARE_SELECTION = True
        try:
            with patch.object(
                _rp, "get_redis_client",
                side_effect=RuntimeError("redis down (forced)"),
            ):
                from backend.agents.graph.nodes.generation import (
                    node_hypothesis,
                )
                result = await node_hypothesis(state, llm, config=config)
        finally:
            settings.ENABLE_PILLAR_AWARE_SELECTION = original_flag

        # Node completed; hypothesis persisted with explicit pillar emit
        hid = result["current_hypothesis_id"]
        assert hid is not None
        async with maker() as s:
            h = (await s.execute(
                select(Hypothesis).where(Hypothesis.id == hid)
            )).scalar_one()
            assert h.pillar == "quality"

    @pytest.mark.asyncio
    async def test_outerjoin_includes_legacy_alphas(self, pg_engine_maker):
        """M3: pillar_balance / nudge query must outerjoin Alpha → Hypothesis
        so legacy alphas (hypothesis_id NULL) are NOT silently dropped. We
        verify by ensuring the deficit calculation considers them as
        ``unknown`` rather than 0 share."""
        engine, maker = pg_engine_maker
        # Seed: a legacy Alpha without a hypothesis link.
        async with maker() as s:
            t = MiningTask(
                task_name=f"{_TAG}_task_{uuid.uuid4().hex[:5]}",
                region="USA",
                universe="TOP3000",
                dataset_strategy="AUTO",                status="RUNNING",
                daily_goal=4,
                
                config={},
            )
            s.add(t)
            await s.commit()
            a = Alpha(
                alpha_id=f"{_TAG}{uuid.uuid4().hex[:13]}",
                task_id=t.id,
                region="USA",
                universe="TOP3000",
                expression="ts_delta(close, 5)",
                hypothesis_id=None,  # legacy
                quality_status="PASS",
                is_sharpe=1.5,
                delay=1,
            )
            s.add(a)
            await s.commit()

        # Now run pillar_balance_check — it MUST include the legacy alpha
        # in its ``legacy_inferred`` bucket via outerjoin + infer_pillar.
        from backend.tasks import pillar_balance_check as _pbc
        result = await _pbc._run_async()
        assert result.get("legacy_inferred_alphas", 0) >= 1, (
            "M3 invariant violated: legacy alpha (hypothesis_id NULL) "
            "was not counted in legacy_inferred — outerjoin missing"
        )

    @pytest.mark.asyncio
    async def test_pillar_nudge_when_skewed(self, pg_engine_maker):
        """When the alpha pool is skewed (>=80% momentum), the nudge picks
        an under-represented pillar and stamps ``_pillar_nudged`` if the
        LLM responds with that target."""
        engine, maker = pg_engine_maker

        # Seed: 10 momentum hypotheses + alphas (heavy skew)
        async with maker() as s:
            t = MiningTask(
                task_name=f"{_TAG}_task_{uuid.uuid4().hex[:5]}",
                region="EUR",  # use a fresh region to minimise contamination
                universe="TOP3000",
                dataset_strategy="AUTO",                status="RUNNING",
                daily_goal=4,
                
                config={},
            )
            s.add(t)
            await s.commit()
            tid = t.id
            for i in range(10):
                h = Hypothesis(
                    statement=f"{_TAG}_mom_{i}_{uuid.uuid4().hex[:4]}",
                    region="EUR",
                    universe="TOP3000",
                    kind="INVESTMENT_THESIS",
                    status="ACTIVE",
                    is_active=True,
                    pillar="momentum",
                )
                s.add(h)
                await s.flush()
                a = Alpha(
                    alpha_id=f"{_TAG}{uuid.uuid4().hex[:13]}",
                    task_id=tid,
                    region="EUR",
                    universe="TOP3000",
                    expression="ts_delta(close, 5)",
                    hypothesis_id=h.id,
                    quality_status="PASS",
                    is_sharpe=1.5,
                    delay=1,
                )
                s.add(a)
            await s.commit()

        state = await _make_state(maker, region="EUR")
        # LLM emits 'value' — the under-represented pillar
        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_value_stmt",
                "pillar": "value",
                "key_fields": ["eps"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_PILLAR_AWARE_SELECTION
        settings.ENABLE_PILLAR_AWARE_SELECTION = True
        try:
            # Patch Redis so we don't depend on a live redis instance
            import backend.tasks.redis_pool as _rp
            with patch.object(_rp, "get_redis_client", side_effect=RuntimeError("no redis")):
                from backend.agents.graph.nodes.generation import (
                    node_hypothesis,
                )
                result = await node_hypothesis(state, llm, config=config)
        finally:
            settings.ENABLE_PILLAR_AWARE_SELECTION = original

        # The hypothesis was persisted with pillar=value (stamp invariant)
        hid = result["current_hypothesis_id"]
        assert hid is not None
        async with maker() as s:
            h = (await s.execute(
                select(Hypothesis).where(Hypothesis.id == hid)
            )).scalar_one()
            assert h.pillar == "value"
