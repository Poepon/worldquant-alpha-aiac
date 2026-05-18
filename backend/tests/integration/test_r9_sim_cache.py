"""Phase 3 R9 integration tests for sim_cache (2026-05-18).

Uses pg_session (live PG) since SimulationCache has JSON cols and
test exercises real INSERT/SELECT.

Coverage:
  - compute_cache_key: stability, region-sensitive, settings-projection
  - get_cached: miss → None, hit → result, expired → None
  - set_cached: insert + update (UPSERT), success-only guard
  - cached_simulate_batch: 100% miss → 100% BRAIN, mixed hit/miss,
    100% hit → 0 BRAIN call, BRAIN failure soft-fall, settings_only_success guard
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.config import _flag_override_cache  # noqa: E402
from backend.agents.sim_cache import (  # noqa: E402
    SETTINGS_KEYS,
    cached_simulate_batch,
    compute_cache_key,
    get_cached,
    set_cached,
)
from backend.models.simulation_cache import SimulationCache  # noqa: E402


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


_TAG = f"r9_test_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_session() -> AsyncGenerator[AsyncSession, None]:
    from backend.config import settings
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                # Clean up rows tagged with _TAG (in expression)
                await s.execute(text(
                    "DELETE FROM simulation_cache WHERE expression LIKE :p"
                ), {"p": f"{_TAG}%"})
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


# ---------------------------------------------------------------------------
# compute_cache_key (pure, no DB)
# ---------------------------------------------------------------------------

def test_cache_key_stable_across_calls():
    k1 = compute_cache_key(region="USA", universe="TOP3000",
                            expression="rank(close)",
                            settings={"delay": 1, "decay": 4})
    k2 = compute_cache_key(region="USA", universe="TOP3000",
                            expression="rank(close)",
                            settings={"delay": 1, "decay": 4})
    assert k1 == k2


def test_cache_key_region_sensitive():
    k_usa = compute_cache_key(region="USA", universe="TOP3000",
                               expression="rank(close)", settings={})
    k_chn = compute_cache_key(region="CHN", universe="TOP3000",
                               expression="rank(close)", settings={})
    assert k_usa != k_chn


def test_cache_key_ignores_unknown_settings_keys():
    """Only SETTINGS_KEYS project — extra keys must not change key."""
    k1 = compute_cache_key(region="USA", universe="TOP3000",
                            expression="x", settings={"delay": 1})
    k2 = compute_cache_key(region="USA", universe="TOP3000",
                            expression="x",
                            settings={"delay": 1, "extra_garbage": True})
    assert k1 == k2


def test_cache_key_settings_sensitive():
    """Different sim settings → different keys."""
    base = {"delay": 1, "decay": 4, "neutralization": "SUBINDUSTRY",
            "truncation": 0.08, "test_period": "P2Y0M"}
    k1 = compute_cache_key(region="USA", universe="TOP3000",
                            expression="rank(close)", settings=base)
    k2 = compute_cache_key(region="USA", universe="TOP3000",
                            expression="rank(close)",
                            settings={**base, "delay": 0})
    assert k1 != k2


def test_cache_key_case_normalizes_region():
    k_upper = compute_cache_key(region="USA", universe="TOP3000",
                                 expression="x", settings={})
    k_lower = compute_cache_key(region="usa", universe="TOP3000",
                                 expression="x", settings={})
    assert k_upper == k_lower


# ---------------------------------------------------------------------------
# get_cached / set_cached
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_cached_miss_returns_none(pg_session):
    assert await get_cached(pg_session, "no_such_key_xyz") is None


@pytest.mark.asyncio
async def test_set_then_get_round_trip(pg_session):
    key = compute_cache_key(region="USA", universe="TOP3000",
                             expression=f"{_TAG}_alpha", settings={"delay": 1})
    result = {"success": True, "sharpe": 1.5, "alpha_id": "abc"}
    ok = await set_cached(pg_session,
                           cache_key=key, region="USA", universe="TOP3000",
                           expression=f"{_TAG}_alpha",
                           settings={"delay": 1}, result=result)
    assert ok
    hit = await get_cached(pg_session, key)
    assert hit is not None
    assert hit["sharpe"] == 1.5
    assert hit["alpha_id"] == "abc"


@pytest.mark.asyncio
async def test_set_cached_skips_failures_by_default(pg_session):
    """SIMULATION_CACHE_ONLY_SUCCESS default True — failures NOT cached."""
    key = compute_cache_key(region="USA", universe="TOP3000",
                             expression=f"{_TAG}_fail", settings={})
    ok = await set_cached(pg_session, cache_key=key, region="USA",
                           universe="TOP3000",
                           expression=f"{_TAG}_fail", settings={},
                           result={"success": False, "error": "BRAIN_5xx"})
    assert ok is False
    assert await get_cached(pg_session, key) is None


@pytest.mark.asyncio
async def test_set_cached_upsert_first_writer_wins(pg_session):
    """Bug-#7: ON CONFLICT DO UPDATE preserves the first writer's result.

    Two concurrent workers that both miss in get_cached and race to
    set_cached must NOT clobber each other's result_json — the second
    INSERT collapses to an access-stats bump (access_count + accessed_at)
    only. This avoids wasted BRAIN spend + the cache flapping between
    different "first" results when two races hit the same expression.
    """
    from sqlalchemy import text as _sql_text
    key = compute_cache_key(region="USA", universe="TOP3000",
                             expression=f"{_TAG}_upsert", settings={"delay": 1})
    await set_cached(pg_session, cache_key=key, region="USA",
                      universe="TOP3000",
                      expression=f"{_TAG}_upsert", settings={"delay": 1},
                      result={"success": True, "sharpe": 1.0})
    await set_cached(pg_session, cache_key=key, region="USA",
                      universe="TOP3000",
                      expression=f"{_TAG}_upsert", settings={"delay": 1},
                      result={"success": True, "sharpe": 2.5})
    hit = await get_cached(pg_session, key)
    # First writer wins — sharpe stays 1.0, not 2.5.
    assert hit["sharpe"] == 1.0
    # Conflicting INSERT bumped access_count. get_cached itself also
    # bumps once per call, so we expect ≥ 2 (1 from racing set + 1 from
    # the get above).
    row_count = (await pg_session.execute(
        _sql_text("SELECT access_count FROM simulation_cache WHERE cache_key = :k"),
        {"k": key},
    )).scalar_one()
    assert row_count >= 2


@pytest.mark.asyncio
async def test_get_cached_expired_returns_none(pg_session):
    """cached_at older than TTL → treat as miss."""
    key = compute_cache_key(region="USA", universe="TOP3000",
                             expression=f"{_TAG}_expired", settings={})
    await set_cached(pg_session, cache_key=key, region="USA",
                      universe="TOP3000",
                      expression=f"{_TAG}_expired", settings={},
                      result={"success": True, "sharpe": 1.0})
    # Manually backdate the row
    await pg_session.execute(text(
        "UPDATE simulation_cache SET cached_at = :old WHERE cache_key = :k"
    ), {"old": datetime.now(timezone.utc) - timedelta(days=30), "k": key})
    await pg_session.commit()
    # With default TTL 14 days → expired
    assert await get_cached(pg_session, key, ttl_days=14) is None


# ---------------------------------------------------------------------------
# cached_simulate_batch — wrapper around BRAIN
# ---------------------------------------------------------------------------

class _FakeBrain:
    """Mock BrainAdapter with simulate_batch returning preset results."""

    def __init__(self, results_per_expr=None, raise_on_call=False):
        self.results_per_expr = results_per_expr or {}
        self.raise_on_call = raise_on_call
        self.calls = 0
        self.expressions_seen: list = []

    async def simulate_batch(self, *, expressions, **kwargs):
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("BRAIN down")
        self.expressions_seen.extend(expressions)
        return [
            self.results_per_expr.get(e, {"success": True, "sharpe": 1.0,
                                            "alpha_id": f"id-{hash(e) % 1000}"})
            for e in expressions
        ]


@pytest.mark.asyncio
async def test_cached_batch_all_miss_calls_brain(pg_session):
    brain = _FakeBrain()
    exprs = [f"{_TAG}_miss_{i}" for i in range(3)]
    results = await cached_simulate_batch(
        pg_session, brain, expressions=exprs,
        region="USA", universe="TOP3000",
    )
    assert len(results) == 3
    assert all(r["success"] for r in results)
    assert brain.calls == 1
    assert brain.expressions_seen == exprs


@pytest.mark.asyncio
async def test_cached_batch_all_hit_skips_brain(pg_session):
    """Pre-warm cache; second call should make zero BRAIN calls."""
    brain = _FakeBrain()
    exprs = [f"{_TAG}_hit_{i}" for i in range(2)]
    # First call — populates cache
    await cached_simulate_batch(pg_session, brain, expressions=exprs,
                                  region="USA", universe="TOP3000")
    assert brain.calls == 1
    # Second call — should be all hits
    results2 = await cached_simulate_batch(pg_session, brain, expressions=exprs,
                                             region="USA", universe="TOP3000")
    assert len(results2) == 2
    assert brain.calls == 1  # no new BRAIN call


@pytest.mark.asyncio
async def test_cached_batch_partial_hit_calls_brain_for_uncached(pg_session):
    """Mixed hit/miss — BRAIN only called for uncached."""
    brain = _FakeBrain()
    exprs_first = [f"{_TAG}_mix_a", f"{_TAG}_mix_b"]
    await cached_simulate_batch(pg_session, brain, expressions=exprs_first,
                                  region="USA", universe="TOP3000")
    assert brain.calls == 1

    # Second batch: 1 cached + 2 new
    exprs_second = [f"{_TAG}_mix_a", f"{_TAG}_mix_c", f"{_TAG}_mix_d"]
    brain.expressions_seen.clear()
    results = await cached_simulate_batch(pg_session, brain, expressions=exprs_second,
                                            region="USA", universe="TOP3000")
    assert len(results) == 3
    # BRAIN called only for the 2 new ones
    assert brain.expressions_seen == [f"{_TAG}_mix_c", f"{_TAG}_mix_d"]


@pytest.mark.asyncio
async def test_cached_batch_brain_failure_returns_failures(pg_session):
    """BRAIN throws → returns failure dicts for uncached, doesn't blow up."""
    brain = _FakeBrain(raise_on_call=True)
    exprs = [f"{_TAG}_brfail_{i}" for i in range(2)]
    results = await cached_simulate_batch(pg_session, brain, expressions=exprs,
                                            region="USA", universe="TOP3000")
    assert len(results) == 2
    assert all(not r["success"] for r in results)
    assert all("sim_failed" in r.get("error", "") for r in results)


@pytest.mark.asyncio
async def test_cached_batch_empty_input(pg_session):
    brain = _FakeBrain()
    assert await cached_simulate_batch(pg_session, brain, expressions=[]) == []
    assert brain.calls == 0
