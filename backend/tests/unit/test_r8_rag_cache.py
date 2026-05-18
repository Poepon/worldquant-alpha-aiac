"""Phase 3 R8-v2 #2: per-layer Redis cache (2026-05-18).

All tests mock ``backend.agents.hierarchical_rag._get_rag_redis`` so they
run without a live Redis. Cache key building + JSON round-trip + soft-fail
on redis errors covered.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.hierarchical_rag import (
    RAGEntry,
    _cache_get,
    _cache_set,
    _make_layer_cache_key,
    query_hierarchical,
)


# ---------------------------------------------------------------------------
# Key stability
# ---------------------------------------------------------------------------

def test_cache_key_stable_for_same_params():
    k1 = _make_layer_cache_key("L0", {"region": "USA", "expr": "rank(close)", "budget": 5})
    k2 = _make_layer_cache_key("L0", {"budget": 5, "expr": "rank(close)", "region": "USA"})
    assert k1 == k2  # sorted JSON guarantees order independence


def test_cache_key_differs_per_layer():
    p = {"expr": "rank(close)", "region": "USA"}
    assert _make_layer_cache_key("L0", p) != _make_layer_cache_key("L1", p)


def test_cache_key_differs_per_params():
    a = _make_layer_cache_key("L1", {"pillar": "momentum"})
    b = _make_layer_cache_key("L1", {"pillar": "reversal"})
    assert a != b


def test_cache_key_has_prefix():
    k = _make_layer_cache_key("L0", {})
    assert k.startswith("ragcache:")


# ---------------------------------------------------------------------------
# _cache_get / _cache_set round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_get_returns_none_when_redis_unavailable():
    """_get_rag_redis returning None → _cache_get returns None."""
    with patch(
        "backend.agents.hierarchical_rag._get_rag_redis",
        new=AsyncMock(return_value=None),
    ):
        assert await _cache_get("ragcache:foo") is None


@pytest.mark.asyncio
async def test_cache_set_swallows_when_redis_unavailable():
    """_get_rag_redis returning None → _cache_set is a no-op."""
    with patch(
        "backend.agents.hierarchical_rag._get_rag_redis",
        new=AsyncMock(return_value=None),
    ):
        # Just verify no exception
        await _cache_set("ragcache:foo", [], [], 60)


@pytest.mark.asyncio
async def test_cache_set_then_get_roundtrip():
    """Set + get on the same mock redis returns the same entries."""
    fake_store: Dict[str, str] = {}

    class FakeRedis:
        async def get(self, k):
            return fake_store.get(k)

        async def setex(self, k, ttl, v):
            fake_store[k] = v

    fake = FakeRedis()
    succ_in = [RAGEntry(
        pattern_hash="h1", pattern="rank(close)",
        entry_type="SUCCESS_PATTERN", description="d",
        meta_data={"foo": "bar"},
        source_layer="L0_exact", relevance_score=0.9,
    )]
    fail_in = [RAGEntry(
        pattern_hash="h2", pattern="ts_mean(close, 5)",
        entry_type="FAILURE_PITFALL", description="bad",
        meta_data={}, source_layer="L0_exact", relevance_score=0.5,
    )]

    with patch(
        "backend.agents.hierarchical_rag._get_rag_redis",
        new=AsyncMock(return_value=fake),
    ):
        await _cache_set("ragcache:rt", succ_in, fail_in, 60)
        out = await _cache_get("ragcache:rt")

    assert out is not None
    succ_out, fail_out = out
    assert len(succ_out) == 1
    assert len(fail_out) == 1
    assert succ_out[0].pattern_hash == "h1"
    assert succ_out[0].pattern == "rank(close)"
    assert succ_out[0].relevance_score == 0.9
    assert succ_out[0].meta_data == {"foo": "bar"}
    assert fail_out[0].entry_type == "FAILURE_PITFALL"


@pytest.mark.asyncio
async def test_cache_get_swallows_json_decode_error():
    """Corrupt cache payload → return None, do not crash."""

    class CorruptRedis:
        async def get(self, k):
            return "{not valid json"

    with patch(
        "backend.agents.hierarchical_rag._get_rag_redis",
        new=AsyncMock(return_value=CorruptRedis()),
    ):
        assert await _cache_get("ragcache:bad") is None


@pytest.mark.asyncio
async def test_cache_get_swallows_redis_get_error():
    """redis.get raising → return None, do not crash."""

    class RaisingRedis:
        async def get(self, k):
            raise RuntimeError("connection refused")

    with patch(
        "backend.agents.hierarchical_rag._get_rag_redis",
        new=AsyncMock(return_value=RaisingRedis()),
    ):
        assert await _cache_get("ragcache:err") is None


# ---------------------------------------------------------------------------
# Orchestrator dispatch — flag OFF skips cache; flag ON consults cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_flag_off_does_not_consult_cache(monkeypatch):
    """Default flag OFF → _cache_get / _cache_set never called."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG_CACHE", False, raising=False)

    layer_mock = AsyncMock(return_value=([], []))
    get_mock = AsyncMock(return_value=None)
    set_mock = AsyncMock(return_value=None)
    with patch("backend.agents.hierarchical_rag.layer0_exact_match", new=layer_mock), \
         patch("backend.agents.hierarchical_rag.layer1_pillar", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag.layer2_family", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag.layer3_field_level", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag._cache_get", new=get_mock), \
         patch("backend.agents.hierarchical_rag._cache_set", new=set_mock):
        await query_hierarchical(
            db=None, current_expression="rank(close)", region="USA",
        )
    assert get_mock.await_count == 0
    assert set_mock.await_count == 0
    assert layer_mock.await_count == 1  # L0 still called


@pytest.mark.asyncio
async def test_orchestrator_flag_on_cache_miss_calls_layer_and_sets(monkeypatch):
    """Flag ON + cache miss → layer called + _cache_set called per layer."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG_CACHE", True, raising=False)
    monkeypatch.setattr(settings, "RAG_HIER_CACHE_TTL_SEC", 60, raising=False)

    l0_mock = AsyncMock(return_value=([], []))
    get_mock = AsyncMock(return_value=None)  # always miss
    set_mock = AsyncMock(return_value=None)
    with patch("backend.agents.hierarchical_rag.layer0_exact_match", new=l0_mock), \
         patch("backend.agents.hierarchical_rag.layer1_pillar", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag.layer2_family", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag.layer3_field_level", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag._cache_get", new=get_mock), \
         patch("backend.agents.hierarchical_rag._cache_set", new=set_mock):
        await query_hierarchical(
            db=None, current_expression="rank(close)", region="USA",
        )
    # 4 layers all queried + all written through (L0, L1, L2, L3 all eligible
    # with expression provided)
    assert get_mock.await_count == 4
    assert set_mock.await_count == 4
    assert l0_mock.await_count == 1


@pytest.mark.asyncio
async def test_orchestrator_flag_on_cache_hit_skips_layer(monkeypatch):
    """Flag ON + cache hit on L0 → layer0_exact_match NOT called."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG_CACHE", True, raising=False)
    monkeypatch.setattr(settings, "RAG_HIER_CACHE_TTL_SEC", 60, raising=False)

    cached_entry = RAGEntry(
        pattern_hash="hX", pattern="rank(close)",
        entry_type="SUCCESS_PATTERN", description="cached",
        meta_data={}, source_layer="L0_exact", relevance_score=1.0,
    )
    # L0 hit returns cached, others miss
    async def get_side(k):
        if "L0" in k or k.startswith("ragcache:"):
            # we don't know the exact hash; return cached on first call only
            return None
        return None

    # Simpler: make _cache_get return a hit on the very first call (L0)
    call_state = {"n": 0}

    async def get_seq(k):
        call_state["n"] += 1
        if call_state["n"] == 1:
            # Serialize a single SUCCESS to mimic real cached payload format
            import json
            payload = json.dumps({
                "succ": [{
                    "pattern_hash": cached_entry.pattern_hash,
                    "pattern": cached_entry.pattern,
                    "entry_type": cached_entry.entry_type,
                    "description": cached_entry.description,
                    "meta_data": cached_entry.meta_data,
                    "source_layer": cached_entry.source_layer,
                    "relevance_score": cached_entry.relevance_score,
                }],
                "fail": [],
            })
            return ([cached_entry], [])  # already-rehydrated tuple matches _cache_get contract
        return None

    l0_mock = AsyncMock(return_value=([], []))
    set_mock = AsyncMock(return_value=None)
    with patch("backend.agents.hierarchical_rag.layer0_exact_match", new=l0_mock), \
         patch("backend.agents.hierarchical_rag.layer1_pillar", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag.layer2_family", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag.layer3_field_level", new=AsyncMock(return_value=([], []))), \
         patch("backend.agents.hierarchical_rag._cache_get", new=AsyncMock(side_effect=get_seq)), \
         patch("backend.agents.hierarchical_rag._cache_set", new=set_mock):
        result = await query_hierarchical(
            db=None, current_expression="rank(close)", region="USA",
        )

    # L0 hit → layer0_exact_match NOT called; L0 write-through SKIPPED
    assert l0_mock.await_count == 0
    # cached entry surfaces in patterns
    assert any(p.pattern_hash == "hX" for p in result.patterns)
    # L1, L2, L3 still miss → fetched + write-through (3 sets, not 4)
    assert set_mock.await_count == 3


@pytest.mark.asyncio
async def test_orchestrator_flag_on_layer_call_uses_cache_call_returns():
    """Sanity: cache wrapper returns layer fetch result on miss path."""
    from backend.config import settings
    monkeypatch_attrs = {
        "ENABLE_HIERARCHICAL_RAG_CACHE": True,
        "RAG_HIER_CACHE_TTL_SEC": 60,
    }
    old_vals = {k: getattr(settings, k, None) for k in monkeypatch_attrs}
    for k, v in monkeypatch_attrs.items():
        setattr(settings, k, v)
    try:
        entry = RAGEntry(
            pattern_hash="h", pattern="rank(open)",
            entry_type="SUCCESS_PATTERN", description="ok",
            meta_data={}, source_layer="L0_exact", relevance_score=1.0,
        )
        l0_mock = AsyncMock(return_value=([entry], []))
        with patch("backend.agents.hierarchical_rag.layer0_exact_match", new=l0_mock), \
             patch("backend.agents.hierarchical_rag.layer1_pillar", new=AsyncMock(return_value=([], []))), \
             patch("backend.agents.hierarchical_rag.layer2_family", new=AsyncMock(return_value=([], []))), \
             patch("backend.agents.hierarchical_rag.layer3_field_level", new=AsyncMock(return_value=([], []))), \
             patch("backend.agents.hierarchical_rag._cache_get", new=AsyncMock(return_value=None)), \
             patch("backend.agents.hierarchical_rag._cache_set", new=AsyncMock(return_value=None)):
            result = await query_hierarchical(
                db=None, current_expression="rank(open)", region="USA",
            )
        assert any(p.pattern_hash == "h" for p in result.patterns)
    finally:
        for k, v in old_vals.items():
            setattr(settings, k, v)
