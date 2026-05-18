"""P2-C node_hypothesis × regime style preset integration tests (2026-05-16).

PG-only via S5 ``_pg_reachable`` + module-level pytestmark.

Covers:
    N1 byte-for-byte (MF4 field assertion): flag=False + strategy.regime
       set → captured_ctx.style_preset is None AND
       "_regime_style_seen" not in primary_h. **Field assertion only**
       (no prompt-string equality — P2-A M8 / P2-D M5 lesson).
    N2 flag=True + regime='crisis' attached in strategy → prompt contains
       "Investment Philosophy" + "Risk-Off Defensive"; primary_h gets
       "_regime_style_seen" == "crisis".
"""
from __future__ import annotations

import os
import socket
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")


def _pg_reachable() -> bool:
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = int(os.getenv("POSTGRES_PORT", "5433"))
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="P2-C regime tests require Postgres reachable",
)


# Warm-up
import backend.tasks  # noqa: E402, F401
import backend.agents.graph.nodes.generation as _generation_mod  # noqa: E402, F401

from backend.models import Alpha, Hypothesis, MiningTask  # noqa: E402

_TAG = f"nhR{uuid.uuid4().hex[:3]}_"


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
                await s.commit()
        except Exception:
            pass
        await engine.dispose()


async def _make_state(maker, region="USA"):
    from backend.agents.graph.state import MiningState

    async with maker() as s:
        t = MiningTask(
            task_name=f"{_TAG}_task_{uuid.uuid4().hex[:5]}",
            region=region, universe="TOP3000",
            dataset_strategy="AUTO",            status="RUNNING",
            daily_goal=4, max_iterations=2,
            config={},
        )
        s.add(t)
        await s.commit()
        task_id = t.id
    return MiningState(
        task_id=task_id, region=region, universe="TOP3000",
        dataset_id="fundamental6",
        fields=[], operators=[],
        focused_fields=[],
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


def _crisis_strategy_blob() -> dict:
    """Mirror what mining_agent.run_mining_iteration writes to strategy.to_dict()
    after a crisis regime injection."""
    return {
        "mode": "balanced",
        "temperature": 0.7,
        "exploration_weight": 0.5,
        "regime": "crisis",
        "style_preset": {
            "regime": "crisis",
            "style_label": "Risk-Off Defensive",
            "style_philosophy": (
                "Capital preservation over alpha hunting. Favour low-beta, "
                "low-turnover, quality and defensive value signals."
            ),
            "pillar_bias": ["quality", "value", "volatility"],
        },
    }


class TestNodeHypothesisRegime:

    @pytest.mark.asyncio
    async def test_flag_off_byte_for_byte_legacy(self, pg_engine_maker):
        """MF4: flag=False even with strategy.regime set →
        captured_ctx.style_preset is None AND no _regime_style_seen stamp.
        Field assertion ONLY (no prompt-string equality)."""
        engine, maker = pg_engine_maker
        state = await _make_state(maker)

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
        # strategy carries regime — but the flag is OFF
        config = {
            "configurable": {
                "hypothesis_centric_level": 2,
                "strategy": _crisis_strategy_blob(),
            }
        }

        from backend.config import settings
        original = settings.ENABLE_STYLE_PRESET_GUIDANCE
        settings.ENABLE_STYLE_PRESET_GUIDANCE = False
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
            settings.ENABLE_STYLE_PRESET_GUIDANCE = original

        ctx = captured.get("ctx")
        assert ctx is not None, "build_hypothesis_prompt was not called"
        # MF4 byte-for-byte invariant: field-level assertion ONLY
        assert ctx.style_preset is None, (
            f"MF4 byte-for-byte invariant violated: expected None, got "
            f"{ctx.style_preset!r}"
        )
        primary_h = (result.get("hypotheses") or [{}])[0]
        assert "_regime_style_seen" not in primary_h, (
            "_regime_style_seen leaked when flag was off"
        )

    @pytest.mark.asyncio
    async def test_flag_on_injects_block_and_stamps(self, pg_engine_maker):
        """N2: flag=True + crisis preset attached → prompt contains
        'Investment Philosophy' and 'Risk-Off Defensive';
        primary_h._regime_style_seen == 'crisis'."""
        engine, maker = pg_engine_maker
        state = await _make_state(maker)

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_on_stmt",
                "pillar": "quality",
                "key_fields": ["eps", "roe"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        config = {
            "configurable": {
                "hypothesis_centric_level": 2,
                "strategy": _crisis_strategy_blob(),
            }
        }

        from backend.config import settings
        original = settings.ENABLE_STYLE_PRESET_GUIDANCE
        settings.ENABLE_STYLE_PRESET_GUIDANCE = True
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
            settings.ENABLE_STYLE_PRESET_GUIDANCE = original

        ctx = captured.get("ctx")
        assert ctx is not None
        assert isinstance(ctx.style_preset, dict)
        assert ctx.style_preset.get("regime") == "crisis"
        assert ctx.style_preset.get("style_label") == "Risk-Off Defensive"

        # The actual rendered prompt must include the block
        rendered = llm.call.call_args.kwargs.get("user_prompt") or (
            llm.call.call_args.args[1] if llm.call.call_args.args else ""
        )
        assert "Investment Philosophy" in rendered, (
            "style block missing from rendered prompt"
        )
        assert "Risk-Off Defensive" in rendered, (
            "style_label missing from rendered prompt"
        )

        primary_h = (result.get("hypotheses") or [{}])[0]
        assert primary_h.get("_regime_style_seen") == "crisis", (
            f"_regime_style_seen wrong/missing: "
            f"{primary_h.get('_regime_style_seen')}"
        )

    @pytest.mark.asyncio
    async def test_flag_on_but_no_regime_in_strategy(self, pg_engine_maker):
        """flag=True but strategy.regime is None (e.g. cold-start before
        first inference run) → no block, no stamp, no crash."""
        engine, maker = pg_engine_maker
        state = await _make_state(maker)

        parsed = {
            "hypotheses": [{
                "id": "H1",
                "statement": f"{_TAG}_cold_stmt",
                "pillar": "value",
                "key_fields": ["eps"],
                "suggested_operators": ["ts_rank"],
            }],
        }
        llm = _fake_llm(parsed)
        # strategy without regime
        cold_strategy = {
            "mode": "balanced",
            "temperature": 0.7,
            "exploration_weight": 0.5,
        }
        config = {
            "configurable": {
                "hypothesis_centric_level": 2,
                "strategy": cold_strategy,
            }
        }

        from backend.config import settings
        original = settings.ENABLE_STYLE_PRESET_GUIDANCE
        settings.ENABLE_STYLE_PRESET_GUIDANCE = True
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
            settings.ENABLE_STYLE_PRESET_GUIDANCE = original

        ctx = captured.get("ctx")
        assert ctx is not None
        assert ctx.style_preset is None
        primary_h = (result.get("hypotheses") or [{}])[0]
        assert "_regime_style_seen" not in primary_h
