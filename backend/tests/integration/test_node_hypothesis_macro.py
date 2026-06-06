"""P2-A node_hypothesis × macro-narrative nudge integration tests (2026-05-16).

PG-only (uses JSONB). M3: inline ``_pg_reachable`` + module-level
``pytestmark``.

Covers:
  - N1 byte-for-byte: flag=False → macro_narratives=[] in PromptContext,
       no _macro_narratives_seen on primary_h (M8 field assertion)
  - N2 flag=True + seeded narratives → injected + N4 stamp
  - N3 flag=True but fetch raises → non-fatal fallback, no stamp
  - N4 Redis cache hit on 2nd call
  - N5 field_id double-key extraction (M7): focused_fields mixed
       ``{"field_id": ...}`` and ``{"id": ...}`` keys both surface as
       candidate keys
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
    reason="P2-A node_hypothesis macro tests require Postgres on localhost:5433",
)


# Warm-up
import backend.tasks  # noqa: E402, F401
import backend.agents.graph.nodes.generation as _generation_mod  # noqa: E402, F401

from backend.macro_narratives import (  # noqa: E402
    MacroNarrative,
    narrative_to_kb_payload,
)
from backend.models import (  # noqa: E402
    Alpha,
    Hypothesis,
    KnowledgeEntry,
    MiningTask,
)


_TAG = f"nhM{uuid.uuid4().hex[:3]}_"


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
                        "DELETE FROM knowledge_entries WHERE "
                        "entry_type='MACRO_NARRATIVE' AND "
                        "(meta_data->>'field_id' ILIKE :p "
                        " OR meta_data->>'dataset_category' ILIKE :p)"
                    ),
                    {"p": f"%{_TAG}%"},
                )
                await s.commit()
        except Exception:
            pass
        await engine.dispose()


async def _make_state(maker, region="USA", focused_fields=None):
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
        focused_fields=list(focused_fields or []),
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


async def _seed_macro(maker, *, field_id=None, dataset_category=None,
                      region="*", confidence=0.85, source="seed",
                      mechanism=None, transmission=None,
                      hint="momentum"):
    """Insert one MACRO_NARRATIVE row directly into the KB."""
    mechanism = mechanism or f"{_TAG}MECH"
    transmission = transmission or f"{_TAG}TRANS"
    n = MacroNarrative(
        field_id=field_id, dataset_category=dataset_category,
        region=region, mechanism=mechanism, transmission_channel=transmission,
        expected_signal_hint=hint, confidence=confidence, source=source,
    )
    payload = narrative_to_kb_payload(n)
    row = KnowledgeEntry(
        entry_type=payload["entry_type"],
        pattern=payload["pattern"],
        pattern_hash=payload["pattern_hash"],
        description=payload["description"],
        meta_data=payload["meta_data"],
        is_active=True,
        created_by=payload["created_by"],
        usage_count=0,
    )
    async with maker() as s:
        s.add(row)
        await s.commit()
    return n


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestNodeHypothesisMacro:

    @pytest.mark.asyncio
    async def test_flag_off_byte_for_byte_legacy(self, pg_engine_maker):
        """N1: flag=False + seeded narrative for our field → PromptContext.
        macro_narratives == [] AND primary_h has NO _macro_narratives_seen.
        Field-level assertion (M8). Pre-seed a narrative that WOULD match
        if the flag were on so we verify the flag actually gates the fetch.
        """
        engine, maker = pg_engine_maker
        seed_fid = f"{_TAG}field_off"
        await _seed_macro(maker, field_id=seed_fid, confidence=0.99)

        state = await _make_state(maker, focused_fields=[
            {"field_id": seed_fid},
        ])

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_off_stmt",
                "pillar": "value",
                "key_fields": ["eps"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_MACRO_NARRATIVE_GUIDANCE
        settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = False
        try:
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
            settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = original

        ctx = captured.get("ctx")
        assert ctx is not None, "build_hypothesis_prompt was not called"
        # N1 / M8 invariant: macro_narratives is EXACTLY []
        assert ctx.macro_narratives == [], (
            f"M8 byte-for-byte invariant violated: expected [], got "
            f"{ctx.macro_narratives!r}"
        )
        primary_h = (result.get("hypotheses") or [{}])[0]
        assert "_macro_narratives_seen" not in primary_h, (
            "N4 stamp leaked when flag was off"
        )

    @pytest.mark.asyncio
    async def test_flag_on_injects_narratives(self, pg_engine_maker):
        """N2: flag=True + seeded field narrative → ctx.macro_narratives
        non-empty + primary_h._macro_narratives_seen contains the field_id."""
        engine, maker = pg_engine_maker
        fid = f"{_TAG}field_on"
        await _seed_macro(maker, field_id=fid, confidence=0.95,
                          mechanism=f"INJ_{_TAG}MECH",
                          transmission=f"INJ_{_TAG}TRANS")

        state = await _make_state(maker, focused_fields=[
            {"field_id": fid},
        ])

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_on_stmt",
                "pillar": "value",
                "key_fields": [fid],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_MACRO_NARRATIVE_GUIDANCE
        settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = True
        try:
            captured = {}
            import backend.agents.graph.nodes.generation as _g
            original_builder = _g.build_hypothesis_prompt

            def _spy_builder(ctx, exp_trace=None):
                captured["ctx"] = ctx
                return original_builder(ctx, exp_trace)

            # Force DB path (no Redis) so the test doesn't depend on a
            # live redis cache shared across runs.
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
            settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = original

        ctx = captured.get("ctx")
        assert ctx is not None
        assert isinstance(ctx.macro_narratives, list)
        assert any(
            n.get("field_id") == fid for n in ctx.macro_narratives
        ), f"seeded narrative not in ctx: {ctx.macro_narratives}"

        primary_h = (result.get("hypotheses") or [{}])[0]
        seen = primary_h.get("_macro_narratives_seen")
        assert isinstance(seen, list) and fid in seen, (
            f"_macro_narratives_seen missing or wrong: {seen}"
        )

    @pytest.mark.asyncio
    async def test_flag_on_fetch_failure_nonfatal(self, pg_engine_maker):
        """N3: flag=True but fetch raises → node continues, macro_narratives=[],
        no stamp."""
        engine, maker = pg_engine_maker
        state = await _make_state(maker, focused_fields=[
            {"field_id": "eps"},
        ])

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
        original = settings.ENABLE_MACRO_NARRATIVE_GUIDANCE
        settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = True
        try:
            captured = {}
            import backend.agents.graph.nodes.generation as _g
            original_builder = _g.build_hypothesis_prompt

            def _spy_builder(ctx, exp_trace=None):
                captured["ctx"] = ctx
                return original_builder(ctx, exp_trace)

            import backend.services.macro_narrative_service as _svc_mod

            async def _boom(self, *, dataset_id, region, key_fields=None,
                            limit_field=3, limit_dataset=1, limit_category=1):
                raise RuntimeError("simulated fetch failure")

            with patch.object(
                _svc_mod.MacroNarrativeService,
                "fetch_macro_narratives", _boom,
            ), patch.object(
                _g, "build_hypothesis_prompt", side_effect=_spy_builder,
            ):
                from backend.agents.graph.nodes.generation import (
                    node_hypothesis,
                )
                result = await node_hypothesis(state, llm, config=config)
        finally:
            settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = original

        ctx = captured.get("ctx")
        assert ctx is not None
        assert ctx.macro_narratives == [], (
            f"non-fatal fetch failure did not fall back to []: "
            f"{ctx.macro_narratives}"
        )
        primary_h = (result.get("hypotheses") or [{}])[0]
        assert "_macro_narratives_seen" not in primary_h

    @pytest.mark.asyncio
    async def test_redis_cache_hit(self, pg_engine_maker):
        """N4: same (dataset_id, region, sh-date) two calls → second call
        reads from Redis (fetch_macro_narratives call_count = 1)."""
        engine, maker = pg_engine_maker
        fid = f"{_TAG}cache_fld"
        await _seed_macro(maker, field_id=fid, confidence=0.9)

        state = await _make_state(maker, focused_fields=[
            {"field_id": fid},
        ])

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_cache_stmt",
                "pillar": "value",
                "key_fields": [fid],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_MACRO_NARRATIVE_GUIDANCE
        settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = True

        fake_store: dict = {}
        fake_redis = MagicMock()
        fake_redis.get = MagicMock(side_effect=lambda k: fake_store.get(k))

        def _setex(k, ttl, v):
            fake_store[k] = v.encode() if isinstance(v, str) else v
        fake_redis.setex = MagicMock(side_effect=_setex)

        import backend.services.macro_narrative_service as _svc_mod
        real = _svc_mod.MacroNarrativeService.fetch_macro_narratives
        call_count = {"n": 0}

        async def _spy(self, *, dataset_id, region, key_fields=None,
                       limit_field=3, limit_dataset=1, limit_category=1):
            call_count["n"] += 1
            return await real(
                self, dataset_id=dataset_id, region=region,
                key_fields=key_fields, limit_field=limit_field,
                limit_dataset=limit_dataset, limit_category=limit_category,
            )

        try:
            import backend.tasks.redis_pool as _rp
            with patch.object(
                _rp, "get_redis_client", return_value=fake_redis,
            ), patch.object(
                _svc_mod.MacroNarrativeService,
                "fetch_macro_narratives", _spy,
            ):
                from backend.agents.graph.nodes.generation import (
                    node_hypothesis,
                )
                await node_hypothesis(state, llm, config=config)
                first_calls = call_count["n"]
                assert any(
                    k.startswith("aiac:macro_narrative:") for k in fake_store
                ), f"cache not populated: {list(fake_store)}"

                await node_hypothesis(state, llm, config=config)
                second_calls = call_count["n"]
                assert second_calls == first_calls, (
                    f"cache miss on 2nd call: extra calls = "
                    f"{second_calls - first_calls}"
                )
        finally:
            settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = original

    @pytest.mark.asyncio
    async def test_field_id_double_key_extraction(self, pg_engine_maker):
        """N5 / M7: state.focused_fields may carry either ``field_id`` (Phase-1
        union path) or ``id`` (distillation path) — both must surface as
        candidate keys. Seed two narratives; verify both are retrievable via
        ctx.macro_narratives when one focused field uses each key style."""
        engine, maker = pg_engine_maker
        fid_a = f"{_TAG}dk_a"
        fid_b = f"{_TAG}dk_b"
        await _seed_macro(maker, field_id=fid_a, confidence=0.9,
                          mechanism=f"DK_A_{_TAG}")
        await _seed_macro(maker, field_id=fid_b, confidence=0.9,
                          mechanism=f"DK_B_{_TAG}")

        # Mixed key styles: one entry uses field_id, the other id.
        state = await _make_state(maker, focused_fields=[
            {"field_id": fid_a},
            {"id": fid_b},
        ])

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_dk_stmt",
                "pillar": "value",
                "key_fields": [fid_a, fid_b],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {"configurable": {"hypothesis_centric_level": 2}}

        from backend.config import settings
        original = settings.ENABLE_MACRO_NARRATIVE_GUIDANCE
        settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = True
        try:
            captured = {}
            import backend.agents.graph.nodes.generation as _g
            original_builder = _g.build_hypothesis_prompt

            def _spy_builder(ctx, exp_trace=None):
                captured["ctx"] = ctx
                return original_builder(ctx, exp_trace)

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
                await node_hypothesis(state, llm, config=config)
        finally:
            settings.ENABLE_MACRO_NARRATIVE_GUIDANCE = original

        ctx = captured.get("ctx")
        assert ctx is not None
        seen_fids = {
            n.get("field_id") for n in ctx.macro_narratives
            if isinstance(n, dict)
        }
        assert fid_a in seen_fids and fid_b in seen_fids, (
            f"M7 double-key extraction failed — expected both {fid_a} and "
            f"{fid_b} in ctx.macro_narratives, got {seen_fids}"
        )
