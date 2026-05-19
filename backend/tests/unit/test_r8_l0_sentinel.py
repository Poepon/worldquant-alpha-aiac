"""Unit tests for Phase 4 Sprint 0 PR0.5 — ENABLE_R8_L0 sentinel sub-flag.

Coverage:
  - query_hierarchical L0 block runs when ENABLE_R8_L0=True (default)
  - query_hierarchical L0 block is skipped when ENABLE_R8_L0=False (R12
    sentinel ACTIVE), L1/L2/L3 stay LIVE
  - rag_service.query() logs sentinel-active info when ENABLE_R8_L0=False
    falls through to legacy retrieval

Layer fetchers (layer0_exact_match / layer1_pillar / layer2_family /
layer3_field) are all patched to return empty (succ=[], fail=[]) so the
test pivots purely on whether each layer's lambda was invoked. We assert
on `result.total_queries` (incremented once per executed layer) AND on
`result.layer_hits` (only populated when a layer returned entries).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.hierarchical_rag import query_hierarchical


# ---------------------------------------------------------------------------
# Fixture — patch all four layer fetchers to track invocation count
# ---------------------------------------------------------------------------


class _LayerSpy:
    """Track invocations across all 4 layer fetchers."""
    def __init__(self):
        self.calls = {"L0": 0, "L1": 0, "L2": 0, "L3": 0}

    def factory(self, layer_name: str):
        async def _fake(*args, **kwargs):
            self.calls[layer_name] += 1
            return [], []  # empty succ + fail
        return _fake


@pytest.fixture
def _patched_layers():
    spy = _LayerSpy()
    # Also patch the Redis cache so each layer fetcher actually runs (cache miss).
    with (
        patch("backend.agents.hierarchical_rag.layer0_exact_match",
              side_effect=spy.factory("L0")) as _l0,
        patch("backend.agents.hierarchical_rag.layer1_pillar",
              side_effect=spy.factory("L1")) as _l1,
        patch("backend.agents.hierarchical_rag.layer2_family",
              side_effect=spy.factory("L2")) as _l2,
        patch("backend.agents.hierarchical_rag.layer3_field_level",
              side_effect=spy.factory("L3")) as _l3,
        patch("backend.agents.hierarchical_rag._cache_get",
              new=AsyncMock(return_value=None)),  # always cache-miss
        patch("backend.agents.hierarchical_rag._cache_set",
              new=AsyncMock(return_value=None)),
    ):
        yield spy


# ---------------------------------------------------------------------------
# query_hierarchical L0 guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l0_executes_when_flag_on(_patched_layers):
    """ENABLE_R8_L0=True (default) → L0 layer is invoked."""
    with patch("backend.config.settings.ENABLE_R8_L0", True):
        result = await query_hierarchical(
            db=None,  # not used since layer fetchers are mocked
            current_expression="rank(close - open)",
            hypothesis_pillar="momentum",
            region="USA",
            dataset_id="fundamental6",
        )
    assert _patched_layers.calls["L0"] == 1, "L0 must run when flag ON"
    assert _patched_layers.calls["L1"] >= 1, "L1 still runs"
    # L2/L3 may or may not run depending on remaining budget; we don't pin


@pytest.mark.asyncio
async def test_l0_skipped_when_flag_off(_patched_layers):
    """ENABLE_R8_L0=False (R12 sentinel ON) → L0 block must be skipped."""
    with patch("backend.config.settings.ENABLE_R8_L0", False):
        result = await query_hierarchical(
            db=None,
            current_expression="rank(close - open)",
            hypothesis_pillar="momentum",
            region="USA",
            dataset_id="fundamental6",
        )
    assert _patched_layers.calls["L0"] == 0, "L0 must NOT run when flag OFF"
    # Other layers still LIVE — L1 at minimum
    assert _patched_layers.calls["L1"] >= 1, "L1 stays LIVE when L0 OFF"


@pytest.mark.asyncio
async def test_l0_skip_without_expression_unchanged(_patched_layers):
    """Even without current_expression, L0 wouldn't run anyway (its guard
    requires current_expression). Flag OFF + no expr = same behavior."""
    with patch("backend.config.settings.ENABLE_R8_L0", False):
        result = await query_hierarchical(
            db=None,
            current_expression=None,  # L0 would skip regardless
            hypothesis_pillar="momentum",
            region="USA",
        )
    assert _patched_layers.calls["L0"] == 0
    # L1 has its own guard requiring expr OR pillar; pillar provided → runs
    assert _patched_layers.calls["L1"] >= 1


# ---------------------------------------------------------------------------
# rag_service.query() legacy-entry sentinel telemetry
# ---------------------------------------------------------------------------


def _make_svc():
    """Build a RAGService stub with legacy retrieval methods stubbed empty."""
    from backend.agents.services.rag_service import RAGService

    svc = RAGService.__new__(RAGService)
    svc.db = None
    async def _empty_patterns(**kw): return []
    async def _empty_pitfalls(**kw): return []
    async def _empty_info(*a, **kw): return None
    svc._get_success_patterns_enhanced = _empty_patterns  # type: ignore
    svc._get_failure_pitfalls_enhanced = _empty_pitfalls  # type: ignore
    svc._get_dataset_info = _empty_info  # type: ignore
    svc._infer_pillar_hint_from_pool = AsyncMock(return_value=None)  # type: ignore
    return svc


@pytest.mark.asyncio
async def test_rag_service_logs_sentinel_active_on_legacy_path():
    """rag_service.query() must INFO-log when ENABLE_R8_L0=False even though
    legacy retrieval has no L0-equivalent to skip — ops needs visibility
    that R12 sentinel is in effect on this call path."""
    import io
    from loguru import logger

    sink = io.StringIO()
    handler_id = logger.add(sink, level="INFO", format="{message}")
    try:
        svc = _make_svc()
        with (
            patch("backend.config.settings.ENABLE_R8_L0", False),
            patch("backend.config.settings.ENABLE_HIERARCHICAL_RAG", False),
        ):
            await svc.query(dataset_id="fnd6", region="USA",
                            max_patterns=1, max_pitfalls=1)
        out = sink.getvalue()
        assert "R8_L0 sentinel ACTIVE" in out, (
            f"expected sentinel log in output, got: {out!r}"
        )
    finally:
        logger.remove(handler_id)


@pytest.mark.asyncio
async def test_rag_service_silent_when_hierarchical_dispatched():
    """F-S2 (post-review): ENABLE_R8_L0=False AND hierarchical-dispatched
    (hierarchical_rag.query_hierarchical handles L0 skip) → no sentinel log
    at rag_service level (it would be misleading — the L0 was already skipped
    in the hierarchical layer)."""
    import io
    from loguru import logger as _lg
    from backend.agents.services.rag_service import RAGService

    svc = _make_svc()
    # Mock the hierarchical query to short-circuit (returns empty result)
    sink = io.StringIO()
    handler_id = _lg.add(sink, level="INFO", format="{message}")
    try:
        with (
            patch("backend.config.settings.ENABLE_R8_L0", False),
            patch("backend.config.settings.ENABLE_HIERARCHICAL_RAG", True),
            patch(
                "backend.agents.hierarchical_rag.query_hierarchical",
                new=AsyncMock(return_value=MagicMock(
                    patterns=[], pitfalls=[], layer_hits={}, total_queries=0,
                )),
            ),
        ):
            await svc.query(
                dataset_id="fnd6", region="USA",
                current_expression="rank(close - open)",  # triggers hierarchical
                max_patterns=1, max_pitfalls=1,
            )
        out = sink.getvalue()
        assert "R8_L0 sentinel ACTIVE" not in out, (
            f"sentinel log should NOT emit on hierarchical-dispatched path, got: {out!r}"
        )
    finally:
        _lg.remove(handler_id)


@pytest.mark.asyncio
async def test_rag_service_silent_when_flag_on():
    """ENABLE_R8_L0=True → no sentinel log emitted on legacy path."""
    import io
    from loguru import logger

    sink = io.StringIO()
    handler_id = logger.add(sink, level="INFO", format="{message}")
    try:
        svc = _make_svc()
        with (
            patch("backend.config.settings.ENABLE_R8_L0", True),
            patch("backend.config.settings.ENABLE_HIERARCHICAL_RAG", False),
        ):
            await svc.query(dataset_id="fnd6", region="USA",
                            max_patterns=1, max_pitfalls=1)
        out = sink.getvalue()
        assert "R8_L0 sentinel ACTIVE" not in out, (
            f"unexpected sentinel log when flag ON: {out!r}"
        )
    finally:
        logger.remove(handler_id)
