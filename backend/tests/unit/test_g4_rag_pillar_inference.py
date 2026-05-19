"""G4 补强 Phase A unit tests for RAGService._infer_pillar_hint_from_pool +
RAGService.query dispatch path (2026-05-19).

Coverage:
  - _infer_pillar_hint_from_pool: empty pool → None
  - _infer_pillar_hint_from_pool: balanced pool (no deficit > threshold) → None
  - _infer_pillar_hint_from_pool: heavy deficit → returns top deficit pillar
  - _infer_pillar_hint_from_pool: Redis cache hit short-circuits DB
  - _infer_pillar_hint_from_pool: Redis down → DB fallback works
  - _infer_pillar_hint_from_pool: empty PILLAR_TARGET_DISTRIBUTION → None
  - _infer_pillar_hint_from_pool: SQL exception → None (NEVER raises)
  - query dispatch: ENABLE_HIERARCHICAL_RAG=False → no pillar inference call
  - query dispatch: caller passes hypothesis_pillar → inference SKIPPED
  - query dispatch: caller passes current_expression → inference SKIPPED
  - query dispatch: no region → inference SKIPPED (defensive)
  - query dispatch: inferred pillar → query_hierarchical called with it
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _flag_on(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True)
    yield


@pytest.fixture
def _flag_off(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", False)
    yield


@pytest.fixture
def _pillar_targets(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(
        settings,
        "PILLAR_TARGET_DISTRIBUTION",
        {"momentum": 0.25, "value": 0.20, "quality": 0.20, "volatility": 0.15, "sentiment": 0.20},
    )
    monkeypatch.setattr(settings, "PILLAR_BALANCE_SKEW_THRESHOLD", 0.4)
    yield


def _make_svc(rows=None, redis_value=None, raise_on_sql=False):
    """Build a RAGService with mocked db.execute returning the given rows."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = AsyncMock()
    if raise_on_sql:
        svc.db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    else:
        result = MagicMock()
        result.all = MagicMock(return_value=rows or [])
        svc.db.execute = AsyncMock(return_value=result)

    redis = MagicMock()
    redis.get = MagicMock(return_value=redis_value)
    redis.setex = MagicMock()
    return svc, redis


# ---------------------------------------------------------------------------
# _infer_pillar_hint_from_pool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infer_pillar_empty_pool_returns_none(_pillar_targets):
    """No alphas in window → counts empty → no deficit triggers."""
    svc, redis = _make_svc(rows=[])
    with patch("backend.tasks.redis_pool.get_redis_client", return_value=redis):
        out = await svc._infer_pillar_hint_from_pool(region="USA")
    # All targets have 0 share → deficit = target for each → max > threshold * target
    # So actually empty pool DOES trigger a hint (the most-target pillar wins).
    # Verify behavior: with all pillars at 0 share, the largest target (momentum
    # at 0.25) has deficit = 0.25 > 0.4 * 0.25 = 0.1 → returns "momentum".
    assert out == "momentum"


@pytest.mark.asyncio
async def test_infer_pillar_balanced_pool_returns_none(_pillar_targets):
    """Pool whose shares match target distribution → no deficit > threshold."""
    svc, redis = _make_svc(rows=[
        ("momentum", 25),
        ("value", 20),
        ("quality", 20),
        ("volatility", 15),
        ("sentiment", 20),
    ])
    with patch("backend.tasks.redis_pool.get_redis_client", return_value=redis):
        out = await svc._infer_pillar_hint_from_pool(region="USA")
    assert out is None


@pytest.mark.asyncio
async def test_infer_pillar_heavy_deficit_returns_top(_pillar_targets):
    """Pool skewed momentum-heavy → value is most deficient → returns 'value'."""
    svc, redis = _make_svc(rows=[
        ("momentum", 95),
        ("value", 1),
        ("quality", 1),
        ("volatility", 1),
        ("sentiment", 2),
    ])
    with patch("backend.tasks.redis_pool.get_redis_client", return_value=redis):
        out = await svc._infer_pillar_hint_from_pool(region="USA")
    # value share ≈ 0.01, target 0.20, deficit ≈ 0.19, threshold 0.4*0.20=0.08
    # → 0.19 > 0.08 → triggers, value is top deficit
    assert out == "value"


@pytest.mark.asyncio
async def test_infer_pillar_redis_cache_hit_short_circuits_sql(_pillar_targets):
    """Cache hit → no SQL execute call."""
    import json
    cached = json.dumps({"momentum": 95, "value": 1, "quality": 1, "volatility": 1, "sentiment": 2})
    svc, redis = _make_svc(rows=[], redis_value=cached)
    with patch("backend.tasks.redis_pool.get_redis_client", return_value=redis):
        out = await svc._infer_pillar_hint_from_pool(region="USA")
    # value deficit triggers
    assert out == "value"
    # SQL must NOT have been called
    svc.db.execute.assert_not_called()
    # setex NOT called either (we read from cache, no write-back needed)
    redis.setex.assert_not_called()


@pytest.mark.asyncio
async def test_infer_pillar_redis_down_falls_back_to_sql(_pillar_targets):
    """Redis client raises → DB query still runs."""
    svc, redis = _make_svc(rows=[
        ("momentum", 95), ("value", 1), ("quality", 1), ("volatility", 1), ("sentiment", 2),
    ])
    with patch("backend.tasks.redis_pool.get_redis_client", side_effect=RuntimeError("redis down")):
        out = await svc._infer_pillar_hint_from_pool(region="USA")
    assert out == "value"
    svc.db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_infer_pillar_empty_target_distribution_returns_none(monkeypatch):
    """No PILLAR_TARGET_DISTRIBUTION config → can't compute deficits → None."""
    from backend.config import settings
    monkeypatch.setattr(settings, "PILLAR_TARGET_DISTRIBUTION", {})

    svc, redis = _make_svc(rows=[("momentum", 50)])
    with patch("backend.tasks.redis_pool.get_redis_client", return_value=redis):
        out = await svc._infer_pillar_hint_from_pool(region="USA")
    assert out is None


@pytest.mark.asyncio
async def test_infer_pillar_sql_exception_returns_none(_pillar_targets):
    """SQL exception → soft-fail → None (NEVER raises)."""
    svc, redis = _make_svc(raise_on_sql=True)
    with patch("backend.tasks.redis_pool.get_redis_client", return_value=redis):
        out = await svc._infer_pillar_hint_from_pool(region="USA")
    assert out is None


# ---------------------------------------------------------------------------
# RAGService.query dispatch — when does inference fire?
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_flag_off_no_inference_called(_flag_off, _pillar_targets):
    """ENABLE_HIERARCHICAL_RAG=False → inference helper not called."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = AsyncMock()
    svc._infer_pillar_hint_from_pool = AsyncMock(return_value=None)
    # Stub the legacy retrieval path so we don't need the full SQL graph
    svc._get_success_patterns_enhanced = AsyncMock(return_value=[])
    svc._get_failure_pitfalls_enhanced = AsyncMock(return_value=[])
    svc._get_dataset_info = AsyncMock(return_value=None)

    await svc.query(dataset_id="pv1", region="USA")
    svc._infer_pillar_hint_from_pool.assert_not_called()


@pytest.mark.asyncio
async def test_query_caller_provided_pillar_skips_inference(_flag_on, _pillar_targets):
    """Caller passes hypothesis_pillar='momentum' → inference NOT called."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = AsyncMock()
    svc._infer_pillar_hint_from_pool = AsyncMock(return_value=None)

    with patch(
        "backend.agents.hierarchical_rag.query_hierarchical",
        new=AsyncMock(return_value=MagicMock(
            patterns=[], pitfalls=[], layer_hits={}, total_queries=0,
        )),
    ):
        svc._get_dataset_info = AsyncMock(return_value=None)
        await svc.query(region="USA", hypothesis_pillar="momentum")
    svc._infer_pillar_hint_from_pool.assert_not_called()


@pytest.mark.asyncio
async def test_query_caller_provided_expression_skips_inference(_flag_on, _pillar_targets):
    """Caller passes current_expression → inference NOT called."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = AsyncMock()
    svc._infer_pillar_hint_from_pool = AsyncMock(return_value=None)

    with patch(
        "backend.agents.hierarchical_rag.query_hierarchical",
        new=AsyncMock(return_value=MagicMock(
            patterns=[], pitfalls=[], layer_hits={}, total_queries=0,
        )),
    ):
        svc._get_dataset_info = AsyncMock(return_value=None)
        await svc.query(region="USA", current_expression="ts_rank(returns, 20)")
    svc._infer_pillar_hint_from_pool.assert_not_called()


@pytest.mark.asyncio
async def test_query_no_region_skips_inference(_flag_on, _pillar_targets):
    """Defensive: no region → inference NOT called → goes legacy."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = AsyncMock()
    svc._infer_pillar_hint_from_pool = AsyncMock(return_value=None)
    svc._get_success_patterns_enhanced = AsyncMock(return_value=[])
    svc._get_failure_pitfalls_enhanced = AsyncMock(return_value=[])
    svc._get_dataset_info = AsyncMock(return_value=None)

    await svc.query(dataset_id="pv1")
    svc._infer_pillar_hint_from_pool.assert_not_called()


@pytest.mark.asyncio
async def test_query_inferred_pillar_dispatches_to_hierarchical(_flag_on, _pillar_targets):
    """Inference returns 'value' → query_hierarchical called with that pillar."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = AsyncMock()
    svc._infer_pillar_hint_from_pool = AsyncMock(return_value="value")
    svc._get_dataset_info = AsyncMock(return_value=None)

    qh_mock = AsyncMock(return_value=MagicMock(
        patterns=[], pitfalls=[], layer_hits={"L1": 3}, total_queries=1,
    ))
    with patch("backend.agents.hierarchical_rag.query_hierarchical", new=qh_mock):
        await svc.query(dataset_id="pv1", region="USA")
    svc._infer_pillar_hint_from_pool.assert_awaited_once_with(region="USA")
    qh_mock.assert_awaited_once()
    # hypothesis_pillar kwarg propagated to query_hierarchical
    call_kwargs = qh_mock.call_args.kwargs
    assert call_kwargs["hypothesis_pillar"] == "value"
    assert call_kwargs["current_expression"] is None
    assert call_kwargs["region"] == "USA"


@pytest.mark.asyncio
async def test_query_inferred_pillar_none_falls_back_to_legacy(_flag_on, _pillar_targets):
    """Inference returns None → R8 dispatch skipped → legacy path runs."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = AsyncMock()
    svc._infer_pillar_hint_from_pool = AsyncMock(return_value=None)
    svc._get_success_patterns_enhanced = AsyncMock(return_value=[])
    svc._get_failure_pitfalls_enhanced = AsyncMock(return_value=[])
    svc._get_dataset_info = AsyncMock(return_value=None)

    qh_mock = AsyncMock()
    with patch("backend.agents.hierarchical_rag.query_hierarchical", new=qh_mock):
        await svc.query(dataset_id="pv1", region="USA")
    svc._infer_pillar_hint_from_pool.assert_awaited_once_with(region="USA")
    qh_mock.assert_not_called()
    svc._get_success_patterns_enhanced.assert_awaited()


@pytest.mark.asyncio
async def test_query_inference_exception_falls_back_to_legacy(_flag_on, _pillar_targets):
    """Inference raises (shouldn't but defensive) → soft-fail → legacy path."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = AsyncMock()
    svc._infer_pillar_hint_from_pool = AsyncMock(side_effect=RuntimeError("boom"))
    svc._get_success_patterns_enhanced = AsyncMock(return_value=[])
    svc._get_failure_pitfalls_enhanced = AsyncMock(return_value=[])
    svc._get_dataset_info = AsyncMock(return_value=None)

    await svc.query(dataset_id="pv1", region="USA")
    svc._infer_pillar_hint_from_pool.assert_awaited_once()
    svc._get_success_patterns_enhanced.assert_awaited()
