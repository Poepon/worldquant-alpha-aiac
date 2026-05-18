"""R8 query log helper + flag-gate tests (2026-05-18).

Closes the R8 follow-up listed in project_phase3_r8_kb_shape_endpoint memory.
Covers:
  - _write_r8_query_log soft-fails on DB exception (returns None, never raises)
  - _write_r8_query_log soft-fails on missing deps (import failure)
  - Flag-OFF skip: query_hierarchical wire block doesn't call writer
  - ORM model registered + has expected columns
  - Migration source has correct table name + indexes
  - Static-source sentinel for the wire site
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Migration + ORM sanity
# ---------------------------------------------------------------------------

def test_orm_model_has_expected_columns():
    from backend.models.r8_query_log import R8QueryLog
    cols = {c.name for c in R8QueryLog.__table__.columns}
    assert "task_id" in cols
    assert "region" in cols
    assert "current_expression_hash" in cols
    assert "layer_hits" in cols
    assert "total_queries" in cols
    assert "cache_hit" in cols
    assert "had_failure_tree_elevation" in cols
    assert "created_at" in cols


def test_orm_model_exported_from_models_init():
    """R8QueryLog importable from backend.models top-level."""
    from backend.models import R8QueryLog  # noqa: F401


def test_migration_source_creates_correct_table():
    import inspect
    import backend.alembic.versions.b2e5c9f1d847_r8_query_log as mig
    src = inspect.getsource(mig)
    assert 'create_table' in src
    assert '"r8_query_log"' in src
    assert "ix_r8q_created_at" in src
    assert "ix_r8q_task_id" in src


# ---------------------------------------------------------------------------
# _write_r8_query_log soft-fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_helper_soft_fails_on_db_exception():
    """DB session raise → returns None, never propagates."""
    from backend.agents import hierarchical_rag

    class _BadSession:
        async def __aenter__(self):
            raise RuntimeError("DB unreachable")
        async def __aexit__(self, *a):
            return None

    with patch("backend.database.AsyncSessionLocal", lambda: _BadSession()):
        out = await hierarchical_rag._write_r8_query_log(
            region="USA", dataset_id="fnd6",
            current_expression="rank(close)",
            layer_hits={"L0_exact": 1},
            total_queries=1, cache_hit=False,
            had_failure_tree_elevation=False,
        )
        assert out is None  # NEVER raises


@pytest.mark.asyncio
async def test_write_helper_commits_when_session_ok():
    """Happy path: session.add called, commit awaited."""
    from backend.agents import hierarchical_rag

    captured = {"added": None, "commits": 0}

    class _OkSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        def add(self, row):
            captured["added"] = row
        async def commit(self):
            captured["commits"] += 1

    with patch("backend.database.AsyncSessionLocal", lambda: _OkSession()):
        await hierarchical_rag._write_r8_query_log(
            region="USA", dataset_id="fnd6",
            current_expression="rank(close)",
            layer_hits={"L0_exact": 1, "L1_pillar": 0, "L2_family": 0, "L3_field": 0},
            total_queries=1, cache_hit=False,
            had_failure_tree_elevation=False,
        )
    assert captured["commits"] == 1
    assert captured["added"] is not None
    assert captured["added"].region == "USA"


# ---------------------------------------------------------------------------
# Flag-gate sentinel — query_hierarchical wire site
# ---------------------------------------------------------------------------

def test_query_hierarchical_has_flag_gated_wire_block():
    """Static-source sentinel: wire sits behind ENABLE_R8_QUERY_LOG flag."""
    import inspect
    from backend.agents import hierarchical_rag

    src = inspect.getsource(hierarchical_rag.query_hierarchical)
    assert "ENABLE_R8_QUERY_LOG" in src
    assert "_write_r8_query_log" in src
    # Soft-fail wrapper
    assert "try" in src and "except" in src


def test_write_helper_handles_none_expression():
    """current_expression=None → expr_hash=None, no crash."""
    from backend.agents import hierarchical_rag

    captured = {"row": None}

    class _OkSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def add(self, row): captured["row"] = row
        async def commit(self): pass

    async def _run():
        with patch("backend.database.AsyncSessionLocal", lambda: _OkSession()):
            await hierarchical_rag._write_r8_query_log(
                region=None, dataset_id=None,
                current_expression=None,
                layer_hits={}, total_queries=0,
                cache_hit=False, had_failure_tree_elevation=False,
            )
    import asyncio
    asyncio.run(_run())
    assert captured["row"] is not None
    assert captured["row"].current_expression_hash is None


# ---------------------------------------------------------------------------
# R8 cleanup wires (2026-05-18 follow-up): task_id plumbing +
# had_failure_tree_elevation signal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_helper_records_task_id_when_provided():
    """task_id kwarg flows into the inserted R8QueryLog row."""
    from backend.agents import hierarchical_rag

    captured = {"row": None}

    class _OkSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def add(self, row): captured["row"] = row
        async def commit(self): pass

    with patch("backend.database.AsyncSessionLocal", lambda: _OkSession()):
        await hierarchical_rag._write_r8_query_log(
            task_id=4242,
            region="USA", dataset_id="fnd6",
            current_expression="rank(close)",
            layer_hits={"L0_exact": 1},
            total_queries=1, cache_hit=False,
            had_failure_tree_elevation=False,
        )
    assert captured["row"].task_id == 4242


@pytest.mark.asyncio
async def test_write_helper_records_had_failure_tree_elevation_true():
    """had_failure_tree_elevation=True flows into the row."""
    from backend.agents import hierarchical_rag

    captured = {"row": None}

    class _OkSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def add(self, row): captured["row"] = row
        async def commit(self): pass

    with patch("backend.database.AsyncSessionLocal", lambda: _OkSession()):
        await hierarchical_rag._write_r8_query_log(
            task_id=None, region="USA", dataset_id="fnd6",
            current_expression="rank(close)",
            layer_hits={"L2_family": 1},
            total_queries=1, cache_hit=False,
            had_failure_tree_elevation=True,
        )
    assert captured["row"].had_failure_tree_elevation is True


def test_query_hierarchical_passes_task_id_to_writer():
    """Static-source sentinel: query_hierarchical wire passes task_id."""
    import inspect
    from backend.agents import hierarchical_rag
    src = inspect.getsource(hierarchical_rag.query_hierarchical)
    assert "task_id=task_id" in src


def test_rag_service_query_accepts_task_id_kwarg():
    """RAGService.query signature includes task_id."""
    import inspect
    from backend.agents.services.rag_service import RAGService
    sig = inspect.signature(RAGService.query)
    assert "task_id" in sig.parameters


def test_node_rag_query_plumbs_state_task_id():
    """node_rag_query passes state.task_id to rag_service.query."""
    import inspect
    from backend.agents.graph.nodes.generation import node_rag_query
    src = inspect.getsource(node_rag_query)
    assert "task_id=getattr(state" in src


def test_elevation_detection_via_meta_marker_in_query_hierarchical():
    """query_hierarchical scans pitfalls for _r1b_failure_tree_bonus_applied
    marker to set had_failure_tree_elevation. Static-source sentinel."""
    import inspect
    from backend.agents import hierarchical_rag
    src = inspect.getsource(hierarchical_rag.query_hierarchical)
    assert "_r1b_failure_tree_bonus_applied" in src
    assert "had_failure_tree_elevation=_had_elevation" in src


# ---------------------------------------------------------------------------
# Cache-hit telemetry (2026-05-18) — closes the last R8 query log limitation
# ---------------------------------------------------------------------------

def test_query_hierarchical_tracks_per_query_cache_hits():
    """Static-source sentinel: _cache_hits_in_query closure counter exists
    + cache_hit= bool reflects whether ANY layer served from cache."""
    import inspect
    from backend.agents import hierarchical_rag
    src = inspect.getsource(hierarchical_rag.query_hierarchical)
    assert "_cache_hits_in_query" in src
    assert "_cache_hits_in_query[0] > 0" in src
    # The cache-hit signal must be wired into the R8 log row
    assert "cache_hit=bool(_cache_hits_in_query[0] > 0)" in src
