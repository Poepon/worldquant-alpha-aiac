"""Integration: GET /ops/costeer/deploy-recommendation (2026-05-18).

Synthesizes R1a + R1b + R8 telemetry into single ranked next-action
verdict so operators don't have to mentally combine 4 endpoints.

Mocks AsyncSession.execute returning canonical rows in the order the
endpoint reads (r1a distribution → r8 kb pair → r8 r5 join → r1b
distribution → hypotheses max depth).
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.database import get_db
from backend.routers.ops import router as ops_router


@pytest.fixture(autouse=True)
def _isolate_ops_token():
    prev = os.environ.pop("OPS_API_TOKEN", None)
    yield
    if prev is not None:
        os.environ["OPS_API_TOKEN"] = prev
    else:
        os.environ.pop("OPS_API_TOKEN", None)


def _mock_db(r1a_rows, r8_kb_pair, r5_count, r1b_rows, chain_max_depth):
    """5 execute calls in order."""
    r1a_r = MagicMock(); r1a_r.all = MagicMock(return_value=list(r1a_rows))
    r8_r = MagicMock(); r8_r.one = MagicMock(return_value=r8_kb_pair)
    r5_r = MagicMock(); r5_r.scalar = MagicMock(return_value=r5_count)
    r1b_r = MagicMock(); r1b_r.all = MagicMock(return_value=list(r1b_rows))
    depth_r = MagicMock(); depth_r.scalar = MagicMock(return_value=chain_max_depth)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[r1a_r, r8_r, r5_r, r1b_r, depth_r])
    return db


@pytest.fixture
def _isolate_flag_state():
    """Snapshot + restore relevant ENABLE_* flags so tests don't leak into
    sibling test files (real bug observed 2026-05-18 — without this, test
    test_dispatch_flag_off_uses_legacy_path in test_r8_rag_dispatch.py
    fails when run AFTER test_all_flags_on_returns_hold_verdict because the
    flag flip was not reverted)."""
    from backend.config import settings as _stg
    keys = [
        "ENABLE_R1A_HOOK", "ENABLE_LLM_JUDGE",
        "ENABLE_HIERARCHICAL_RAG", "ENABLE_R5_L2_RANKING",
        "ENABLE_R1B_RETRY_LOOP", "ENABLE_R1B_HYPOTHESIS_MUTATE",
        "ENABLE_R1B_FAILURE_TREE", "ENABLE_R1B_TYPED_PIPELINE",
        "ENABLE_R1B_DAG_RETRY_REWARD",
    ]
    saved = {k: getattr(_stg, k, False) for k in keys}
    yield
    for k, v in saved.items():
        setattr(_stg, k, v)


@pytest_asyncio.fixture
async def client_factory(_isolate_flag_state):
    async def _build(r1a_rows, r8_kb_pair, r5_count, r1b_rows, chain_max_depth, flags=None):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db(
            r1a_rows, r8_kb_pair, r5_count, r1b_rows, chain_max_depth,
        )
        if flags:
            from backend.config import settings as _stg
            for k, v in flags.items():
                setattr(_stg, k, v)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


# ---------------------------------------------------------------------------
# Empty state → no ready flags, R1a blocker first
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_db_blocks_on_r1a_sample_size(client_factory):
    """No data anywhere → all flags blocked; first blocker is R1a sample size."""
    client = await client_factory(
        r1a_rows=[],
        r8_kb_pair=(0, 0),
        r5_count=0,
        r1b_rows=[],
        chain_max_depth=0,
        flags={
            "ENABLE_R1A_HOOK": False,
            "ENABLE_HIERARCHICAL_RAG": False,
            "ENABLE_R5_L2_RANKING": False,
            "ENABLE_R1B_RETRY_LOOP": False,
            "ENABLE_R1B_HYPOTHESIS_MUTATE": False,
            "ENABLE_R1B_FAILURE_TREE": False,
            "ENABLE_R1B_DAG_RETRY_REWARD": False,
        },
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    body = r.json()
    assert body["ready_flags_to_flip"] == []
    assert "R1A" in body["blockers"][0]
    assert body["signals"]["r1a_total_in_window"] == 0


# ---------------------------------------------------------------------------
# R1a healthy → ENABLE_R1A_HOOK ready (when off)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r1a_ready_when_50_plus_samples(client_factory):
    """≥50 samples + R1A flag OFF → ENABLE_R1A_HOOK ready first."""
    r1a_rows = [
        ("hypothesis", 40),
        ("implementation", 20),  # total 60 > 50
    ]
    client = await client_factory(
        r1a_rows=r1a_rows,
        r8_kb_pair=(0, 0),
        r5_count=0,
        r1b_rows=[],
        chain_max_depth=0,
        flags={"ENABLE_R1A_HOOK": False},
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    body = r.json()
    assert "ENABLE_R1A_HOOK" in body["ready_flags_to_flip"]
    # next_action references first ready flag
    assert "ENABLE_R1A_HOOK" in body["next_action"]


# ---------------------------------------------------------------------------
# R8 KB sufficient → ENABLE_HIERARCHICAL_RAG ready
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r8_ready_when_corpus_sufficient(client_factory):
    """100 SUCCESS + 3 pillars → ENABLE_HIERARCHICAL_RAG ready."""
    client = await client_factory(
        r1a_rows=[],
        r8_kb_pair=(120, 4),  # 120 success / 4 pillars
        r5_count=0,
        r1b_rows=[],
        chain_max_depth=0,
        flags={
            "ENABLE_R1A_HOOK": True,  # so R1A check passes
            "ENABLE_HIERARCHICAL_RAG": False,
        },
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    body = r.json()
    assert "ENABLE_HIERARCHICAL_RAG" in body["ready_flags_to_flip"]


@pytest.mark.asyncio
async def test_r8_blocked_when_pillar_diversity_low(client_factory):
    """120 SUCCESS but only 1 pillar → ENABLE_HIERARCHICAL_RAG blocked, not ready."""
    client = await client_factory(
        r1a_rows=[],
        r8_kb_pair=(120, 1),
        r5_count=0,
        r1b_rows=[],
        chain_max_depth=0,
        flags={
            "ENABLE_R1A_HOOK": True,
            "ENABLE_HIERARCHICAL_RAG": False,
        },
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    body = r.json()
    assert "ENABLE_HIERARCHICAL_RAG" not in body["ready_flags_to_flip"]
    assert any("pillar diversity" in b for b in body["blockers"])


# ---------------------------------------------------------------------------
# R1b chain logic — DAG retry reward needs chain depth > 1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dag_retry_reward_ready_when_chain_grew(client_factory):
    """ENABLE_R1B_RETRY_LOOP ON + max_depth=2 → DAG retry reward ready."""
    client = await client_factory(
        r1a_rows=[],
        r8_kb_pair=(0, 0),
        r5_count=0,
        r1b_rows=[],
        chain_max_depth=2,
        flags={
            "ENABLE_R1B_RETRY_LOOP": True,
            "ENABLE_R1B_DAG_RETRY_REWARD": False,
        },
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    body = r.json()
    assert "ENABLE_R1B_DAG_RETRY_REWARD" in body["ready_flags_to_flip"]


@pytest.mark.asyncio
async def test_dag_retry_reward_blocked_when_chain_flat(client_factory):
    """max_depth=1 (no chain growth) → DAG retry reward NOT ready."""
    client = await client_factory(
        r1a_rows=[],
        r8_kb_pair=(0, 0),
        r5_count=0,
        r1b_rows=[],
        chain_max_depth=1,
        flags={
            "ENABLE_R1B_RETRY_LOOP": True,
            "ENABLE_R1B_DAG_RETRY_REWARD": False,
        },
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    body = r.json()
    assert "ENABLE_R1B_DAG_RETRY_REWARD" not in body["ready_flags_to_flip"]


# ---------------------------------------------------------------------------
# R1b mutate requires retry flag ON + sample + rate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r1b_mutate_ready_when_retry_pass_rate_met(client_factory):
    """Retry flag ON + 50 attempts + 20% pass rate (>15% gate) → mutate ready."""
    r1b_rows = [
        ("retry_impl", "pass", 10),
        ("retry_impl", "fail", 40),
    ]
    client = await client_factory(
        r1a_rows=[],
        r8_kb_pair=(0, 0),
        r5_count=0,
        r1b_rows=r1b_rows,
        chain_max_depth=0,
        flags={
            "ENABLE_R1B_RETRY_LOOP": True,
            "ENABLE_R1B_HYPOTHESIS_MUTATE": False,
        },
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    body = r.json()
    assert "ENABLE_R1B_HYPOTHESIS_MUTATE" in body["ready_flags_to_flip"]


# ---------------------------------------------------------------------------
# All flags on → next_action says "hold"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_flags_on_returns_hold_verdict(client_factory):
    """Every flag in current_flag_state True → no eligible flags, "Hold"."""
    client = await client_factory(
        r1a_rows=[("hypothesis", 100)],
        r8_kb_pair=(200, 5),
        r5_count=100,
        r1b_rows=[("retry_impl", "pass", 50)],
        chain_max_depth=3,
        flags={
            "ENABLE_R1A_HOOK": True,
            "ENABLE_LLM_JUDGE": True,
            "ENABLE_HIERARCHICAL_RAG": True,
            "ENABLE_R5_L2_RANKING": True,
            "ENABLE_R1B_RETRY_LOOP": True,
            "ENABLE_R1B_HYPOTHESIS_MUTATE": True,
            "ENABLE_R1B_FAILURE_TREE": True,
            "ENABLE_R1B_TYPED_PIPELINE": True,
            "ENABLE_R1B_DAG_RETRY_REWARD": True,
        },
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    body = r.json()
    assert body["ready_flags_to_flip"] == []
    assert "Hold" in body["next_action"]


# ---------------------------------------------------------------------------
# Signals + auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signals_dict_includes_all_kpis(client_factory):
    """All 10 KPIs present in signals payload."""
    client = await client_factory(
        r1a_rows=[("hypothesis", 1)],
        r8_kb_pair=(1, 1),
        r5_count=1,
        r1b_rows=[],
        chain_max_depth=0,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    signals = r.json()["signals"]
    for k in (
        "r1a_total_in_window", "r1a_non_unknown_pct",
        "r8_success_pattern_active", "r8_pillar_diversity",
        "r8_r5_rankable_success",
        "r1b_retry_pass_rate", "r1b_mutate_pass_rate",
        "r1b_retry_attempts", "r1b_mutate_attempts",
        "r1b_chain_max_depth",
    ):
        assert k in signals, f"missing signal: {k}"


@pytest.mark.asyncio
async def test_recommendation_requires_ops_token_when_env_set(client_factory):
    os.environ["OPS_API_TOKEN"] = "abc123"
    client = await client_factory([], (0, 0), 0, [], 0)
    async with client as ac:
        r = await ac.get("/api/v1/ops/costeer/deploy-recommendation")
    assert r.status_code == 401
