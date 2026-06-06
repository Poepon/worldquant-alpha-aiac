"""P2-D node_hypothesis × negative-knowledge nudge integration tests.

来源: docs/alphagbm_skills_research_2026-05-15.md skills `take-profit`/
`health-check`.

PG-only — uses the same fixture style as test_node_hypothesis_pillar.

Covers:
  - N1 byte-for-byte invariant: flag=False → failure_pitfalls == state.pitfalls[:5]
  - N2 flag=True + seeded pitfalls → injected + _negative_knowledge_pitfalls_seen stamp
  - N3 fetch failure non-fatal → fallback to legacy state.pitfalls[:5]
  - N4 redis cache hit on 2nd call
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
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
    reason="P2-D node_hypothesis nudge tests require Postgres on localhost:5433",
)


from backend.models import (  # noqa: E402
    Alpha,
    Hypothesis,
    MiningTask,
    KnowledgeEntry,
)

# Warm-up to dodge known circular: tasks must load before agents.graph.nodes
import backend.tasks  # noqa: E402, F401
import backend.agents.graph.nodes.generation as _generation_mod  # noqa: E402,F401
from backend.negative_knowledge import (  # noqa: E402
    FailureSignature,
    compute_signature_key,
)


_TAG = f"nhN{uuid.uuid4().hex[:3]}_"


@pytest_asyncio.fixture
async def pg_engine_maker():
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
                await s.execute(
                    text(
                        "DELETE FROM knowledge_entries "
                        "WHERE meta_data->>'rule_id' ILIKE :p"
                    ),
                    {"p": f"{_TAG}%"},
                )
                await s.commit()
        except Exception:
            pass
        await engine.dispose()


async def _make_state(maker, region="USA", pitfalls=None):
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
        pitfalls=list(pitfalls or []),
    )


class _FakeLLMResponse:
    def __init__(self, parsed):
        self.success = True
        self.parsed = parsed
        self.error = None


def _fake_llm(parsed):
    svc = AsyncMock()
    svc.call = AsyncMock(return_value=_FakeLLMResponse(parsed))
    return svc


async def _seed_pitfall(maker, *, rule_id, region, fail_count=10,
                        skeleton="ts_rank(seeded)"):
    """Insert a FAILURE_PITFALL row that fetch_top_pitfalls will pick up."""
    from backend.services.negative_knowledge_service import (
        NegativeKnowledgeService,
    )
    when = datetime.now(timezone.utc).isoformat()
    sig = FailureSignature(
        signature_key=compute_signature_key(rule_id, skeleton, region),
        rule_id=rule_id,
        skeleton=skeleton,
        region=region,
        category="static_finding",
        severity="orange",
        expected_signal="seeded pitfall",
        remediation_hint=f"avoid {rule_id}",
        failure_count=int(fail_count),
        top_examples=[{"alpha_id": f"{_TAG}seed", "expression": "x",
                       "at": when}],
        first_seen_at=when,
        last_seen_at=when,
    )
    async with maker() as s:
        svc = NegativeKnowledgeService(s)
        await svc.upsert_pitfalls([sig], min_failure_count_to_promote=1)
    return sig


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestNodeHypothesisNegativeKnowledge:

    @pytest.mark.asyncio
    async def test_flag_off_byte_for_byte_legacy(self, pg_engine_maker):
        """N1: flag=False + seeded pitfall → PromptContext.failure_pitfalls
        equals state.pitfalls[:5] (legacy path) AND primary_h has no
        _negative_knowledge_pitfalls_seen stamp.

        Verified via PromptContext field assertion (M5 — frozen prompt
        text is infeasible).
        """
        engine, maker = pg_engine_maker
        # Seed a pitfall that WOULD be picked up if flag were on
        await _seed_pitfall(maker, rule_id=f"{_TAG}OFF_PITFALL",
                            region="USA", fail_count=99999)

        legacy_pitfalls = [
            {"pattern": "ts_rank(legacy1)", "description": "legacy desc 1"},
            {"pattern": "ts_rank(legacy2)", "description": "legacy desc 2"},
        ]
        state = await _make_state(maker, pitfalls=legacy_pitfalls)

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_flag_off_stmt",
                "pillar": "value",
                "key_fields": ["eps"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE
        settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE = False
        try:
            # Capture the PromptContext constructed by the node by
            # spying on build_hypothesis_prompt. Its first arg is the
            # PromptContext instance.
            captured = {}
            import backend.agents.graph.nodes.generation as _g
            original_builder = _g.build_hypothesis_prompt

            def _spy_builder(ctx, exp_trace=None):
                captured["ctx"] = ctx
                return original_builder(ctx, exp_trace)

            with patch.object(
                _g, "build_hypothesis_prompt", side_effect=_spy_builder,
            ):
                from backend.agents.graph.nodes.generation import (
                    node_hypothesis,
                )
                result = await node_hypothesis(state, llm, config=config)
        finally:
            settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE = original

        ctx = captured.get("ctx")
        assert ctx is not None, "build_hypothesis_prompt was not called"
        # N1 invariant: failure_pitfalls == state.pitfalls[:5] EXACTLY
        assert ctx.failure_pitfalls == legacy_pitfalls[:5], (
            f"N1 byte-for-byte invariant violated: "
            f"expected {legacy_pitfalls[:5]}, got {ctx.failure_pitfalls}"
        )
        # And the LLM-emitted hypothesis must NOT carry the nudge stamp
        primary_h = (result.get("hypotheses") or [{}])[0]
        assert "_negative_knowledge_pitfalls_seen" not in primary_h, (
            "_negative_knowledge_pitfalls_seen leaked when flag was off"
        )

    @pytest.mark.asyncio
    async def test_flag_on_injects_pitfalls(self, pg_engine_maker):
        """N2: flag=True + 3 seeded USA pitfalls (fc≥3) →
        PromptContext.failure_pitfalls contains the seeded pitfalls AND
        primary_h._negative_knowledge_pitfalls_seen is a non-empty list."""
        engine, maker = pg_engine_maker
        sig_a = await _seed_pitfall(
            maker, rule_id=f"{_TAG}INJ_A", region="USA",
            fail_count=99999, skeleton="ts_rank(inj_a)",
        )
        sig_b = await _seed_pitfall(
            maker, rule_id=f"{_TAG}INJ_B", region="USA",
            fail_count=99998, skeleton="ts_rank(inj_b)",
        )
        sig_c = await _seed_pitfall(
            maker, rule_id=f"{_TAG}INJ_C", region="USA",
            fail_count=99997, skeleton="ts_rank(inj_c)",
        )
        seeded_keys = {sig_a.signature_key, sig_b.signature_key,
                       sig_c.signature_key}

        state = await _make_state(maker, pitfalls=[])

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_inject_stmt",
                "pillar": "value",
                "key_fields": ["eps"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE
        settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE = True
        try:
            captured = {}
            import backend.agents.graph.nodes.generation as _g
            original_builder = _g.build_hypothesis_prompt

            def _spy_builder(ctx, exp_trace=None):
                captured["ctx"] = ctx
                return original_builder(ctx, exp_trace)

            # Patch redis to fail so we always go to DB (and don't depend
            # on a live redis instance for the test).
            import backend.tasks.redis_pool as _rp
            with patch.object(
                _rp, "get_redis_client",
                side_effect=RuntimeError("no redis (test forced)"),
            ), patch.object(
                _g, "build_hypothesis_prompt", side_effect=_spy_builder,
            ):
                from backend.agents.graph.nodes.generation import (
                    node_hypothesis,
                )
                result = await node_hypothesis(state, llm, config=config)
        finally:
            settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE = original

        ctx = captured.get("ctx")
        assert ctx is not None
        # Should have at least our 3 seeded pitfalls in the list
        fp_keys = {
            p.get("signature_key", "") for p in ctx.failure_pitfalls
            if isinstance(p, dict)
        }
        assert seeded_keys.issubset(fp_keys), (
            f"seeded pitfalls not in failure_pitfalls: "
            f"got {fp_keys}, expected superset {seeded_keys}"
        )

        # primary_h must carry the stamp
        primary_h = (result.get("hypotheses") or [{}])[0]
        seen = primary_h.get("_negative_knowledge_pitfalls_seen")
        assert isinstance(seen, list) and len(seen) >= 3, (
            f"_negative_knowledge_pitfalls_seen missing or short: {seen}"
        )
        assert seeded_keys.issubset(set(seen))

    @pytest.mark.asyncio
    async def test_flag_on_fetch_failure_nonfatal(self, pg_engine_maker):
        """N3: flag=True but fetch_top_pitfalls raises → node continues,
        failure_pitfalls falls back to state.pitfalls[:5], primary_h has
        no stamp."""
        engine, maker = pg_engine_maker
        legacy = [{"pattern": "ts_rank(legacy_n3)", "description": "x"}]
        state = await _make_state(maker, pitfalls=legacy)

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_n3_stmt",
                "pillar": "value",
                "key_fields": ["eps"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE
        settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE = True
        try:
            captured = {}
            import backend.agents.graph.nodes.generation as _g
            original_builder = _g.build_hypothesis_prompt

            def _spy_builder(ctx, exp_trace=None):
                captured["ctx"] = ctx
                return original_builder(ctx, exp_trace)

            # Monkeypatch fetch_top_pitfalls to raise (mid-block failure)
            import backend.services.negative_knowledge_service as _svc_mod

            async def _boom(self, region, *, limit=5, min_fail_count=3,
                            category_filter=None):
                raise RuntimeError("simulated fetch failure")

            with patch.object(
                _svc_mod.NegativeKnowledgeService, "fetch_top_pitfalls", _boom,
            ), patch.object(
                _g, "build_hypothesis_prompt", side_effect=_spy_builder,
            ):
                from backend.agents.graph.nodes.generation import (
                    node_hypothesis,
                )
                result = await node_hypothesis(state, llm, config=config)
        finally:
            settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE = original

        ctx = captured.get("ctx")
        assert ctx is not None
        assert ctx.failure_pitfalls == legacy[:5], (
            "non-fatal fallback did not preserve state.pitfalls[:5]"
        )
        primary_h = (result.get("hypotheses") or [{}])[0]
        assert "_negative_knowledge_pitfalls_seen" not in primary_h

    @pytest.mark.asyncio
    async def test_redis_cache_used(self, pg_engine_maker):
        """N4: same (region, sh-date) on two runs → second run reads from
        Redis cache (fetch_top_pitfalls call_count = 1)."""
        engine, maker = pg_engine_maker
        await _seed_pitfall(
            maker, rule_id=f"{_TAG}CACHE_A", region="USA",
            fail_count=99999, skeleton="ts_rank(cache_a)",
        )
        state = await _make_state(maker, pitfalls=[])

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_cache_stmt",
                "pillar": "value",
                "key_fields": ["eps"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE
        settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE = True

        # Fake redis client — backed by an in-test dict
        fake_store: dict = {}
        fake_redis = MagicMock()
        fake_redis.get = MagicMock(
            side_effect=lambda k: fake_store.get(k),
        )

        def _setex(k, ttl, v):
            fake_store[k] = v.encode() if isinstance(v, str) else v
        fake_redis.setex = MagicMock(side_effect=_setex)

        # Spy on fetch_top_pitfalls (call_count tracking) — wrap the real
        # method so we still hit the DB on the first call.
        import backend.services.negative_knowledge_service as _svc_mod
        real = _svc_mod.NegativeKnowledgeService.fetch_top_pitfalls
        call_count = {"n": 0}

        async def _spy(self, region, *, limit=5, min_fail_count=3,
                      category_filter=None):
            call_count["n"] += 1
            return await real(
                self, region,
                limit=limit, min_fail_count=min_fail_count,
                category_filter=category_filter,
            )

        try:
            import backend.tasks.redis_pool as _rp
            with patch.object(
                _rp, "get_redis_client", return_value=fake_redis,
            ), patch.object(
                _svc_mod.NegativeKnowledgeService,
                "fetch_top_pitfalls", _spy,
            ):
                from backend.agents.graph.nodes.generation import (
                    node_hypothesis,
                )
                # First call — cache MISS → fetch_top_pitfalls runs
                await node_hypothesis(state, llm, config=config)
                first_calls = call_count["n"]
                # Cache should now hold the payload
                assert any(
                    k.startswith(f"aiac:neg_knowledge:USA:")
                    for k in fake_store
                ), f"redis cache not populated: keys={list(fake_store)}"

                # Second call — cache HIT → fetch_top_pitfalls NOT called
                await node_hypothesis(state, llm, config=config)
                second_calls = call_count["n"]
                assert second_calls == first_calls, (
                    f"cache miss on 2nd call: fetch ran "
                    f"{second_calls - first_calls} extra times"
                )
        finally:
            settings.ENABLE_NEGATIVE_KNOWLEDGE_NUDGE = original
