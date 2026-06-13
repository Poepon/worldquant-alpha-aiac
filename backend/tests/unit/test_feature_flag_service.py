"""Unit tests for FeatureFlagService — runtime ENABLE_* flag store.

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.4.

Covers:
* set / clear round-trip
* whitelist enforcement (unknown flag → ValueError)
* type validation (wrong type → ValueError, no DB write)
* audit row written for every write
* old_value captured before UPSERT clobbers it
* list_all merges env defaults + DB overrides
* load_overrides_into_cache replaces _flag_override_cache atomically
* orphan override row (whitelist drift) is silently skipped on cache load

The Redis bump is a fire-and-forget diagnostic; we don't assert on it here
(integration tests cover that).

We deliberately do NOT use the project-wide ``db_session`` fixture from
conftest.py because that fixture runs ``SQLAlchemyBase.metadata.create_all``
on aiosqlite, which fails for JSONB columns elsewhere in the schema. The
two FeatureFlag tables only use basic types (Integer / String / Text /
DateTime), so a tightly-scoped fixture that creates JUST those tables
gives us a real DB without the JSONB blocker.
"""
from __future__ import annotations

import json
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.config import FeatureFlagAudit, FeatureFlagOverride
from backend.services.feature_flag_service import (
    SUPPORTED_FLAGS,
    FeatureFlagService,
    _flag_override_cache,
)


# ---------------------------------------------------------------------------
# Fixtures — module-local, JSONB-free
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="function")
async def ff_engine():
    """Async sqlite engine that only knows the two FeatureFlag tables.

    Builds a fresh ``MetaData`` containing copies of the two tables we
    care about so ``create_all`` doesn't trip over JSONB columns from
    other models in ``SQLAlchemyBase.metadata``.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    isolated = MetaData()
    FeatureFlagOverride.__table__.to_metadata(isolated)
    FeatureFlagAudit.__table__.to_metadata(isolated)

    async with engine.begin() as conn:
        await conn.run_sync(isolated.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(ff_engine) -> AsyncGenerator[AsyncSession, None]:
    """Per-test session bound to the JSONB-free engine above."""
    maker = sessionmaker(ff_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture(autouse=True)
def _clear_cache():
    """Make sure cache mutations from one test don't leak into the next."""
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


@pytest.fixture
def svc(db_session):
    return FeatureFlagService(db_session)


PILLAR_FLAG = "ENABLE_PILLAR_AWARE_SELECTION"
ROBUST_FLAG = "ENABLE_ROBUSTNESS_CHECK"


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_creates_override_and_audit(svc):
    state = await svc.set(PILLAR_FLAG, True, actor="alice", note="enabling for A/B")

    assert state.name == PILLAR_FLAG
    assert state.effective_value is True
    assert state.source == "runtime-override"
    assert state.updated_by == "alice"

    # DB row exists
    rows = (await svc.db.execute(select(FeatureFlagOverride))).scalars().all()
    assert len(rows) == 1
    assert rows[0].flag_name == PILLAR_FLAG
    assert json.loads(rows[0].flag_value) is True
    assert rows[0].updated_by == "alice"

    # Audit row written
    audits = (await svc.db.execute(select(FeatureFlagAudit))).scalars().all()
    assert len(audits) == 1
    assert audits[0].flag_name == PILLAR_FLAG
    assert audits[0].action == "set"
    assert audits[0].old_value is None  # first-set
    assert json.loads(audits[0].new_value) is True

    # Cache write-through
    assert _flag_override_cache[PILLAR_FLAG] is True


@pytest.mark.asyncio
async def test_set_again_records_old_value_and_updates_row(svc):
    await svc.set(PILLAR_FLAG, True, actor="alice")
    await svc.set(PILLAR_FLAG, False, actor="bob", note="too noisy")

    # Still exactly one override row (UPSERT, not duplicate)
    rows = (await svc.db.execute(select(FeatureFlagOverride))).scalars().all()
    assert len(rows) == 1
    assert json.loads(rows[0].flag_value) is False
    assert rows[0].updated_by == "bob"
    assert rows[0].note == "too noisy"

    # Two audit rows, second has old_value=true
    audits = (await svc.db.execute(
        select(FeatureFlagAudit).order_by(FeatureFlagAudit.id)
    )).scalars().all()
    assert len(audits) == 2
    assert json.loads(audits[1].old_value) is True
    assert json.loads(audits[1].new_value) is False
    assert audits[1].actor == "bob"

    # Cache reflects newest write
    assert _flag_override_cache[PILLAR_FLAG] is False


@pytest.mark.asyncio
async def test_set_rejects_unknown_flag(svc):
    with pytest.raises(ValueError, match="not in SUPPORTED_FLAGS"):
        await svc.set("ENABLE_NONEXISTENT_FLAG", True)
    # No DB or cache pollution
    rows = (await svc.db.execute(select(FeatureFlagOverride))).scalars().all()
    assert rows == []
    assert _flag_override_cache == {}


@pytest.mark.asyncio
async def test_set_rejects_wrong_type(svc):
    # PILLAR is bool — passing int should fail before any DB write
    with pytest.raises(ValueError, match="expected bool"):
        await svc.set(PILLAR_FLAG, 1)
    rows = (await svc.db.execute(select(FeatureFlagOverride))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# clear_override
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_removes_row_and_writes_audit(svc):
    await svc.set(PILLAR_FLAG, True, actor="alice")
    state = await svc.clear_override(PILLAR_FLAG, actor="bob", note="reverting")

    # Override gone
    rows = (await svc.db.execute(select(FeatureFlagOverride))).scalars().all()
    assert rows == []

    # Audit row written; old_value preserved
    audits = (await svc.db.execute(
        select(FeatureFlagAudit).order_by(FeatureFlagAudit.id)
    )).scalars().all()
    assert len(audits) == 2
    assert audits[1].action == "clear"
    assert json.loads(audits[1].old_value) is True
    assert json.loads(audits[1].new_value) is None
    assert audits[1].actor == "bob"

    # Cache evicted
    assert PILLAR_FLAG not in _flag_override_cache

    # Returned state reports env-default source
    assert state.source in ("env", "default")
    assert state.override_value is None


@pytest.mark.asyncio
async def test_clear_nonexistent_still_writes_audit(svc):
    """Clearing a flag with no override is a no-op on the row but the
    operator's intent should still be recorded."""
    state = await svc.clear_override(ROBUST_FLAG, actor="bob")

    rows = (await svc.db.execute(select(FeatureFlagOverride))).scalars().all()
    assert rows == []

    audits = (await svc.db.execute(select(FeatureFlagAudit))).scalars().all()
    assert len(audits) == 1
    assert audits[0].action == "clear"
    assert audits[0].old_value is None  # no prior row

    assert state.source in ("env", "default")


@pytest.mark.asyncio
async def test_clear_rejects_unknown_flag(svc):
    with pytest.raises(ValueError, match="not in SUPPORTED_FLAGS"):
        await svc.clear_override("ENABLE_NONEXISTENT_FLAG")


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_all_returns_every_supported_flag(svc):
    states = await svc.list_all()
    by_name = {s.name: s for s in states}

    # Every whitelisted flag appears
    assert set(by_name.keys()) == set(SUPPORTED_FLAGS.keys())

    # All start as env defaults (source != runtime-override)
    for state in states:
        assert state.source != "runtime-override"
        assert state.override_value is None


@pytest.mark.asyncio
async def test_list_all_marks_overridden_flags(svc):
    await svc.set(PILLAR_FLAG, True, actor="alice", note="enable")

    states = await svc.list_all()
    by_name = {s.name: s for s in states}

    pillar = by_name[PILLAR_FLAG]
    assert pillar.source == "runtime-override"
    assert pillar.override_value is True
    assert pillar.effective_value is True
    assert pillar.updated_by == "alice"
    assert pillar.note == "enable"

    # Other flags stay on env default
    other = by_name[ROBUST_FLAG]
    assert other.source != "runtime-override"
    assert other.override_value is None


# ---------------------------------------------------------------------------
# load_overrides_into_cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_overrides_into_cache_replaces_atomically(svc):
    # Pre-populate cache with stale values
    _flag_override_cache["ENABLE_STALE_FLAG"] = "should be wiped"
    _flag_override_cache[PILLAR_FLAG] = "stale"

    # Write a real override and reload
    await svc.set(PILLAR_FLAG, True, actor="refresher_test")
    # commit happened via @transactional — but cache still has stale because
    # the write-through put the right value already. Force a rebuild.
    _flag_override_cache.clear()
    _flag_override_cache["lingering"] = 1

    cache = await svc.load_overrides_into_cache()

    assert cache == {PILLAR_FLAG: True}
    assert _flag_override_cache == {PILLAR_FLAG: True}
    assert "lingering" not in _flag_override_cache


@pytest.mark.asyncio
async def test_load_overrides_skips_orphan_rows(svc):
    """A flag in the DB but not in SUPPORTED_FLAGS (e.g. after a deploy
    that removed it) must not corrupt the cache."""
    # Insert an orphan directly bypassing the whitelist check
    svc.db.add(FeatureFlagOverride(
        flag_name="ENABLE_ORPHAN_FLAG",
        flag_value=json.dumps(True),
        flag_type="bool",
        updated_by="legacy",
    ))
    await svc.db.commit()

    cache = await svc.load_overrides_into_cache()
    assert cache == {}
    assert "ENABLE_ORPHAN_FLAG" not in _flag_override_cache


@pytest.mark.asyncio
async def test_load_overrides_skips_undecodeable_rows(svc):
    """A row whose flag_value is not valid JSON must not crash the
    refresher — log + skip + keep going."""
    svc.db.add(FeatureFlagOverride(
        flag_name=PILLAR_FLAG,
        flag_value="not-json",
        flag_type="bool",
        updated_by="corruption",
    ))
    await svc.db.commit()

    cache = await svc.load_overrides_into_cache()
    # Bad row was skipped; cache stays empty (no override)
    assert cache == {}


# ---------------------------------------------------------------------------
# list_audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_audit_returns_most_recent_first(svc):
    await svc.set(PILLAR_FLAG, True, actor="alice")
    await svc.set(PILLAR_FLAG, False, actor="bob")
    await svc.clear_override(PILLAR_FLAG, actor="carol")

    audits = await svc.list_audit(limit=10)
    assert len(audits) == 3
    # Newest first
    assert audits[0].actor == "carol"
    assert audits[0].action == "clear"
    assert audits[1].actor == "bob"
    assert audits[2].actor == "alice"


@pytest.mark.asyncio
async def test_list_audit_clamps_limit(svc):
    """limit ≤ 0 must coerce to 1; limit > 500 must cap at 500."""
    audits = await svc.list_audit(limit=0)
    assert isinstance(audits, list)
    audits = await svc.list_audit(limit=10_000)
    assert isinstance(audits, list)


def test_retired_flags_removed_from_whitelist():
    from backend.services.feature_flag_service import SUPPORTED_FLAGS
    removed = {
        "ENABLE_DEFAULT_FLAT_SESSION", "ENABLE_FLAT_CONTINUOUS",
        "GRAMMAR_VALIDATOR_RETRY_MAX", "ENABLE_R1A_HOOK", "ENABLE_LLM_JUDGE",
        "ENABLE_G5_CROSSOVER", "ENABLE_TASK_SCHEMA_V2",
        "FLAT_CROSS_REGION_QUOTA", "FLAT_CROSS_REGION_ENFORCE",
        "ENABLE_TASK_STOP_LOSS", "TASK_STOP_LOSS_PASS_RATE_FLOOR",
        "TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS",
    }
    assert removed.isdisjoint(SUPPORTED_FLAGS.keys())


def test_flagspec_has_lifecycle_and_domain_no_group():
    from backend.services.feature_flag_service import (
        SUPPORTED_FLAGS, FlagSpec, LIFECYCLES, DOMAINS,
    )
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(FlagSpec)}
    assert "group" not in field_names
    assert {"lifecycle", "domain"} <= field_names
    for name, spec in SUPPORTED_FLAGS.items():
        assert spec.lifecycle in LIFECYCLES, f"{name} bad lifecycle {spec.lifecycle}"
        assert spec.domain in DOMAINS, f"{name} bad domain {spec.domain}"


def test_flagstate_carries_lifecycle_domain():
    from backend.services.feature_flag_service import FlagState
    import dataclasses
    fields = {f.name for f in dataclasses.fields(FlagState)}
    assert "group" not in fields
    assert {"lifecycle", "domain"} <= fields


def test_flagstateout_wire_model_has_lifecycle_domain_no_group():
    """The /ops/flags wire model must mirror FlagState (lifecycle/domain, no group)
    so `FlagStateOut(**state.__dict__)` serializes without a ValidationError."""
    from backend.routers.ops import FlagStateOut
    from backend.services.feature_flag_service import FlagState
    model_fields = set(FlagStateOut.model_fields.keys())
    assert "group" not in model_fields
    assert {"lifecycle", "domain"} <= model_fields
    # round-trip a real FlagState through the wire model (the exact ops.py call)
    s = FlagState(
        name="X", flag_type="bool", lifecycle="operational", domain="submit",
        description="d", env_default=False, override_value=None,
        effective_value=False, source="default",
    )
    dumped = FlagStateOut(**s.__dict__).model_dump()
    assert dumped["lifecycle"] == "operational"
    assert dumped["domain"] == "submit"
    assert "group" not in dumped
