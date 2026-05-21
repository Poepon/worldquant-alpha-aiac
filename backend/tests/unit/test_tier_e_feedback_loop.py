"""Tier E — feedback-loop completer tests (2026-05-20).

  E1: cognitive-layer bandit reward cron aggregation + node_hypothesis
      loads BanditState (verified via the cron's pure aggregation)
  E2: enforce_token_budget wired into build_hypothesis_prompt — off /
      under-budget = byte-for-byte legacy; flag-on + over-budget trims
  E3: per-model price table in _DistillLLMShim cost estimate
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# E1: cognitive-layer bandit reward aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e1_bandit_update_aggregates_pass_fail(monkeypatch):
    """The cron aggregates _cognitive_layer_used PASS/FAIL per layer +
    cumulatively upserts. Verify the in-memory aiosqlite path end-to-end."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from backend.database import SQLAlchemyBase
    from backend.models import Alpha
    from backend.models.cognitive_layer_bandit import CognitiveLayerBanditState

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Seed alphas: macro layer 3 PASS / 1 FAIL; value layer 1 PASS
    async with maker() as s:
        for i in range(3):
            s.add(Alpha(alpha_id=f"m{i}", expression="x", region="USA",
                        universe="TOP3000", quality_status="PASS",
                        metrics={"_cognitive_layer_used": "macro_top_down"}))
        s.add(Alpha(alpha_id="m_fail", expression="x", region="USA",
                    universe="TOP3000", quality_status="FAIL",
                    metrics={"_cognitive_layer_used": "macro_top_down"}))
        s.add(Alpha(alpha_id="v1", expression="y", region="USA",
                    universe="TOP3000", quality_status="PASS",
                    metrics={"_cognitive_layer_used": "fundamental_value"}))
        await s.commit()

    # Patch AsyncSessionLocal used by the cron's _update_async
    import backend.tasks.cognitive_layer_bandit_tasks as mod
    monkeypatch.setattr(
        "backend.database.AsyncSessionLocal", maker, raising=False,
    )
    # The cron imports AsyncSessionLocal inside _update_async; patch there
    result = await mod._update_async(window_days=30)

    assert result["updated_layers"] == 2
    assert result["by_layer"]["macro_top_down"] == {"pass": 3, "fail": 1}
    assert result["by_layer"]["fundamental_value"] == {"pass": 1, "fail": 0}

    # Verify DB rows written
    from sqlalchemy import select
    async with maker() as s:
        rows = (await s.execute(select(CognitiveLayerBanditState))).scalars().all()
        by_id = {r.layer_id: (r.pass_count, r.fail_count) for r in rows}
    assert by_id["macro_top_down"] == (3, 1)
    assert by_id["fundamental_value"] == (1, 0)

    await engine.dispose()


@pytest.mark.asyncio
async def test_e1_bandit_update_idempotent_rerun(monkeypatch):
    """Tier A-F review fix: an immediate re-run does NOT double-count
    (watermark advanced past the alphas). Cross-week sharpening still
    works because each week's window is non-overlapping (watermark)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy import select
    from backend.database import SQLAlchemyBase
    from backend.models import Alpha
    from backend.models.cognitive_layer_bandit import CognitiveLayerBanditState

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        s.add(Alpha(alpha_id="m1", expression="x", region="USA",
                    universe="TOP3000", quality_status="PASS",
                    metrics={"_cognitive_layer_used": "macro_top_down"}))
        await s.commit()
    monkeypatch.setattr("backend.database.AsyncSessionLocal", maker, raising=False)

    import backend.tasks.cognitive_layer_bandit_tasks as mod
    await mod._update_async(window_days=30)
    await mod._update_async(window_days=30)  # immediate re-run

    async with maker() as s:
        row = (await s.execute(
            select(CognitiveLayerBanditState).where(
                CognitiveLayerBanditState.layer_id == "macro_top_down"
            )
        )).scalar_one()
    # 1 PASS counted ONCE — re-run is idempotent (was the double-count bug)
    assert row.pass_count == 1
    await engine.dispose()


# ---------------------------------------------------------------------------
# E2: enforce_token_budget wired (byte-for-byte legacy + over-budget trim)
# ---------------------------------------------------------------------------

def test_e2_under_budget_byte_for_byte_legacy(monkeypatch):
    """Flag ON but prompt under budget → identical to flag-OFF prompt."""
    from backend.agents.prompts.hypothesis import build_hypothesis_prompt
    from backend.agents.prompts.base import PromptContext
    from backend.config import settings

    ctx = PromptContext(dataset_id="ds1", region="USA")
    monkeypatch.setattr(settings, "ENABLE_COGNITIVE_LAYER_PROMPT", False)
    off = build_hypothesis_prompt(ctx)
    monkeypatch.setattr(settings, "ENABLE_COGNITIVE_LAYER_PROMPT", True)
    monkeypatch.setattr(settings, "COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET", 100000)
    on_under = build_hypothesis_prompt(ctx)
    assert off == on_under  # under budget → no trim → identical


def test_e2_over_budget_trims_cross_task_then_macro(monkeypatch):
    """Flag ON + over budget → drops cross_task block (then macro)."""
    from backend.agents.prompts.hypothesis import build_hypothesis_prompt
    from backend.agents.prompts.base import PromptContext
    from backend.config import settings

    # Large macro + cross_task blocks to blow the budget
    big_macro = [{"mechanism": "x" * 4000, "transmission": "y" * 4000}]
    big_forest = [{
        "hypothesis_id": 1, "statement": "z" * 4000, "rationale": "",
        "pillar": "momentum", "sharpe_avg": 1.5, "pass_count": 3, "alpha_count": 5,
    }]
    ctx = PromptContext(
        dataset_id="ds1", region="USA",
        macro_narratives=big_macro, cross_task_hypotheses=big_forest,
    )
    monkeypatch.setattr(settings, "ENABLE_COGNITIVE_LAYER_PROMPT", True)
    monkeypatch.setattr(settings, "COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET", 500)

    out = build_hypothesis_prompt(ctx)
    # cross_task forest content (the 'z's) should be dropped first
    assert "z" * 100 not in out


# ---------------------------------------------------------------------------
# E3: per-model price table
# ---------------------------------------------------------------------------

def test_e3_price_table_deepseek_cheaper_than_opus():
    from backend.tasks.logic_distill_tasks import _DistillLLMShim
    shim = _DistillLLMShim(MagicMock())
    deepseek = shim._estimate_cost_usd(10000, "deepseek-chat")
    opus = shim._estimate_cost_usd(10000, "claude-opus-4-7")
    assert deepseek < opus
    # deepseek 10k tokens × $0.0014/1k = $0.014
    assert deepseek == pytest.approx(0.014, rel=1e-3)


def test_e3_unknown_model_uses_conservative_default():
    from backend.tasks.logic_distill_tasks import _DistillLLMShim
    shim = _DistillLLMShim(MagicMock())
    unknown = shim._estimate_cost_usd(1000, "some-unknown-model-xyz")
    # default $0.10/1k → 1000 tokens = $0.10
    assert unknown == pytest.approx(0.10, rel=1e-3)


def test_e3_zero_tokens_zero_cost():
    from backend.tasks.logic_distill_tasks import _DistillLLMShim
    shim = _DistillLLMShim(MagicMock())
    assert shim._estimate_cost_usd(0, "deepseek") == 0.0


@pytest.mark.asyncio
async def test_e3_shim_call_uses_model_for_cost():
    from backend.tasks.logic_distill_tasks import _DistillLLMShim
    from backend.agents.services.llm_service import LLMResponse
    fake = MagicMock()
    fake.call = AsyncMock(return_value=LLMResponse(
        content="Distilled.", model="deepseek-chat", tokens_used=5000, success=True,
    ))
    shim = _DistillLLMShim(fake)
    out = await shim.call("prompt")
    # 5000 tokens × deepseek $0.0014/1k = $0.007
    assert out["cost_usd"] == pytest.approx(0.007, rel=1e-3)
    assert out["model"] == "deepseek-chat"
