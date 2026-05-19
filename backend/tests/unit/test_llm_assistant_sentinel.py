"""Phase 4 Sprint 1 A1.2 — R12 sentinel guard + restore integration tests.

Coverage:
  - set(ENABLE_LLM_ASSISTANT_MODE, True) cascades 6 sentinel flags OFF
    + writes 7 audit rows (1 primary + 6 cascade)
  - set(ENABLE_LLM_ASSISTANT_MODE, False) does NOT cascade
  - restore_sentinel() reverses the cascade
  - restore_sentinel() preserves operator's prior override (if existed)
  - restore_sentinel() idempotent (second call audit_rows=0)
  - list_audit default excludes sentinel cascade rows
  - list_audit include_sentinel=True surfaces them
  - retired SUPPORTED_FLAGS entry → restore_sentinel skips it gracefully
"""
from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_set_r12_cascades_six_sentinel_flags_off(db_session):
    from backend.models import FeatureFlagAudit, FeatureFlagOverride
    from backend.services.feature_flag_service import FeatureFlagService
    from backend.config import settings
    from sqlalchemy import select

    svc = FeatureFlagService(db_session)
    # Seed: 1 sentinel flag has an existing True override; the other 5
    # have no override (env default). This lets us check both paths.
    svc.db.add(FeatureFlagOverride(
        flag_name="ENABLE_R1B_HYPOTHESIS_MUTATE",
        flag_value=json.dumps(True),
        flag_type="bool",
        updated_by="seed",
    ))
    await svc.db.commit()

    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="test_op")

    # Every sentinel flag MUST now have an override row with value=false
    sentinel_flags = settings.LLM_ASSISTANT_SENTINEL_FLAGS
    for sf in sentinel_flags:
        row = (await db_session.execute(
            select(FeatureFlagOverride).where(FeatureFlagOverride.flag_name == sf)
        )).scalar_one_or_none()
        assert row is not None, f"sentinel {sf} should have override row"
        assert row.flag_value == json.dumps(False), (
            f"sentinel {sf} flag_value should be 'false', got {row.flag_value!r}"
        )

    # Audit: 1 row for the primary set + 6 for the cascade = 7 total
    audit_rows = list((await db_session.execute(
        select(FeatureFlagAudit).order_by(FeatureFlagAudit.id)
    )).scalars().all())
    primary = [r for r in audit_rows if r.flag_name == "ENABLE_LLM_ASSISTANT_MODE"]
    cascade = [r for r in audit_rows if r.flag_name != "ENABLE_LLM_ASSISTANT_MODE"]
    assert len(primary) == 1 and primary[0].action == "set"
    assert len(cascade) == 6
    for r in cascade:
        assert r.action == "sentinel_set"
        assert r.sentinel_trigger_for == "ENABLE_LLM_ASSISTANT_MODE"
        assert r.flag_name in set(sentinel_flags)
        # The R1b row had a prior True override; the other 5 had nothing
        if r.flag_name == "ENABLE_R1B_HYPOTHESIS_MUTATE":
            assert r.old_value == json.dumps(True)
        else:
            assert r.old_value is None


@pytest.mark.asyncio
async def test_set_r12_false_does_not_cascade(db_session):
    """Only True cascades; setting False is a regular set."""
    from backend.models import FeatureFlagAudit
    from backend.services.feature_flag_service import FeatureFlagService
    from sqlalchemy import select

    svc = FeatureFlagService(db_session)
    await svc.set("ENABLE_LLM_ASSISTANT_MODE", False, actor="test_op")

    cascade_rows = (await db_session.execute(
        select(FeatureFlagAudit).where(
            FeatureFlagAudit.action == "sentinel_set"
        )
    )).scalars().all()
    assert list(cascade_rows) == []


@pytest.mark.asyncio
async def test_restore_sentinel_reverts_cascade(db_session):
    """After R12 cascade fires + restore_sentinel: 5 sentinel overrides
    are DELETED (no prior state) and 1 is REVERTED to its prior True."""
    from backend.models import FeatureFlagAudit, FeatureFlagOverride
    from backend.services.feature_flag_service import FeatureFlagService
    from backend.config import settings
    from sqlalchemy import select

    svc = FeatureFlagService(db_session)
    # Seed prior R1b True override
    svc.db.add(FeatureFlagOverride(
        flag_name="ENABLE_R1B_HYPOTHESIS_MUTATE",
        flag_value=json.dumps(True),
        flag_type="bool",
        updated_by="seed",
    ))
    await svc.db.commit()

    # Trigger cascade
    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="test_op")
    # Restore
    result = await svc.restore_sentinel(actor="restore_op")

    assert result["sentinel_for"] == "ENABLE_LLM_ASSISTANT_MODE"
    assert set(result["restored_flags"]) == set(settings.LLM_ASSISTANT_SENTINEL_FLAGS)
    assert result["skipped"] == []
    assert result["audit_rows"] == 6

    # The 5 sentinel flags that had no prior override → DELETE
    for sf in settings.LLM_ASSISTANT_SENTINEL_FLAGS:
        row = (await db_session.execute(
            select(FeatureFlagOverride).where(FeatureFlagOverride.flag_name == sf)
        )).scalar_one_or_none()
        if sf == "ENABLE_R1B_HYPOTHESIS_MUTATE":
            # Reverted to prior True
            assert row is not None
            assert row.flag_value == json.dumps(True)
        else:
            assert row is None, f"sentinel {sf} should be DELETED, got {row}"

    # All 6 cascade audit rows now have restored_at + restored_by stamps
    cascade_rows = list((await db_session.execute(
        select(FeatureFlagAudit).where(
            FeatureFlagAudit.action == "sentinel_set",
            FeatureFlagAudit.sentinel_trigger_for == "ENABLE_LLM_ASSISTANT_MODE",
        )
    )).scalars().all())
    assert len(cascade_rows) == 6
    for r in cascade_rows:
        assert r.restored_at is not None
        assert r.restored_by == "restore_op"

    # 6 new sentinel_restore audit rows (one per flag)
    restore_rows = (await db_session.execute(
        select(FeatureFlagAudit).where(
            FeatureFlagAudit.action == "sentinel_restore"
        )
    )).scalars().all()
    assert len(list(restore_rows)) == 6


@pytest.mark.asyncio
async def test_restore_sentinel_idempotent_second_call_is_noop(db_session):
    """Second restore call sees restored_at IS NOT NULL → 0 rows to revert."""
    from backend.services.feature_flag_service import FeatureFlagService

    svc = FeatureFlagService(db_session)
    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="test_op")
    first = await svc.restore_sentinel(actor="op1")
    assert first["audit_rows"] == 6

    second = await svc.restore_sentinel(actor="op2")
    assert second["audit_rows"] == 0
    assert second["restored_flags"] == []
    assert second["skipped"] == []


@pytest.mark.asyncio
async def test_restore_sentinel_empty_when_no_cascade(db_session):
    """No prior cascade → restore_sentinel returns empty result."""
    from backend.services.feature_flag_service import FeatureFlagService

    svc = FeatureFlagService(db_session)
    result = await svc.restore_sentinel(actor="curious_op")
    assert result["audit_rows"] == 0
    assert result["restored_flags"] == []


@pytest.mark.asyncio
async def test_list_audit_default_excludes_sentinel_cascade(db_session):
    """Default list_audit hides the 6 sentinel cascade rows (anti-spam)."""
    from backend.services.feature_flag_service import FeatureFlagService

    svc = FeatureFlagService(db_session)
    # Trigger cascade (1 primary + 6 cascade audit rows)
    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="test_op")

    rows = await svc.list_audit(limit=100)
    # Default: only the 1 primary row visible
    assert len(rows) == 1
    assert rows[0].flag_name == "ENABLE_LLM_ASSISTANT_MODE"
    assert rows[0].sentinel_trigger_for is None


@pytest.mark.asyncio
async def test_list_audit_include_sentinel_shows_all(db_session):
    """include_sentinel=True surfaces every cascade row."""
    from backend.services.feature_flag_service import FeatureFlagService

    svc = FeatureFlagService(db_session)
    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="test_op")

    rows = await svc.list_audit(limit=100, include_sentinel=True)
    # 1 primary + 6 cascade
    assert len(rows) == 7
    sentinel_rows = [r for r in rows if r.sentinel_trigger_for is not None]
    assert len(sentinel_rows) == 6


@pytest.mark.asyncio
async def test_restore_sentinel_skips_retired_flag_gracefully(db_session):
    """F5 + S1-C MUST: retired SUPPORTED_FLAGS skip path was docstring-
    declared but never tested. Patch SUPPORTED_FLAGS to drop one of the
    sentinel flags, then verify restore_sentinel skips it (records in
    `skipped` list) while restoring the other 5."""
    from backend.models import FeatureFlagAudit, FeatureFlagOverride
    from backend.services.feature_flag_service import (
        FeatureFlagService, SUPPORTED_FLAGS,
    )
    from sqlalchemy import select

    svc = FeatureFlagService(db_session)
    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="test_op")

    # Simulate retirement of one sentinel flag: pop from SUPPORTED_FLAGS
    # for the duration of this test, then restore after.
    retired_flag = "ENABLE_R1B_HYPOTHESIS_MUTATE"
    saved_spec = SUPPORTED_FLAGS.pop(retired_flag, None)
    try:
        result = await svc.restore_sentinel(actor="restore_op")
    finally:
        if saved_spec is not None:
            SUPPORTED_FLAGS[retired_flag] = saved_spec

    # Retired flag → in `skipped`, others → in `restored_flags`
    assert retired_flag in result["skipped"]
    assert len(result["restored_flags"]) == 5  # 6 - 1 retired
    # Sentinel audit row for retired flag still gets restored_at stamp
    # (otherwise repeated restore_sentinel would loop on it forever)
    retired_audit = (await db_session.execute(
        select(FeatureFlagAudit).where(
            FeatureFlagAudit.flag_name == retired_flag,
            FeatureFlagAudit.action == "sentinel_set",
        )
    )).scalar_one_or_none()
    assert retired_audit is not None
    assert retired_audit.restored_at is not None


@pytest.mark.asyncio
async def test_restore_sentinel_preserves_operator_manual_set(db_session):
    """F3 (S1-B race fix): cascade fires, operator manually sets a
    sentinel flag, then restore is called. The operator's intent must
    win — restore SKIPS the manually-overridden flag + records reason."""
    import asyncio
    from backend.models import FeatureFlagOverride
    from backend.services.feature_flag_service import FeatureFlagService
    from sqlalchemy import select

    svc = FeatureFlagService(db_session)
    # 1. Cascade: R8_L0 forced to False (via R12 = True)
    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="cascade_op")
    # 2. Need a tiny delay so created_at differs (SQLite timestamp resolution)
    await asyncio.sleep(0.01)
    # 3. Operator manually re-enables R8_L0 to debug something
    await svc.set("ENABLE_R8_L0", True, actor="operator_jane")
    # 4. Restore is called — should preserve operator_jane's intent
    result = await svc.restore_sentinel(actor="restore_op")

    # R8_L0 should be in skipped, not restored
    assert "ENABLE_R8_L0" in result["skipped"], result
    assert result["skipped_reasons"].get("ENABLE_R8_L0") == "operator_manual_intervention"
    # The actual override row still has operator's True
    row = (await db_session.execute(
        select(FeatureFlagOverride).where(
            FeatureFlagOverride.flag_name == "ENABLE_R8_L0"
        )
    )).scalar_one_or_none()
    assert row is not None
    import json
    assert row.flag_value == json.dumps(True)
    # Other 5 sentinel flags should still be reverted
    assert len(result["restored_flags"]) == 5


@pytest.mark.asyncio
async def test_restore_sentinel_drains_active_task_residue(db_session):
    """F2 (S1-A Seam 1 fix): after restore, RUNNING tasks have their
    cross-mode residue keys drained from task.config so re-enabled
    flag-gated consumers don't read stale payload (silent zombie)."""
    from backend.models import MiningTask
    from backend.services.feature_flag_service import FeatureFlagService
    from sqlalchemy import select

    svc = FeatureFlagService(db_session)
    # Seed: a RUNNING task with residue keys
    task = MiningTask(
        task_name="t_zombie",
        region="USA",
        universe="TOP3000",
        status="RUNNING",
        config={
            "g5_pending_offspring": [{"expr": "stale"}],
            "__r1b_consumed_pending_hypothesis": {"statement": "stale"},
            "brain_role_snapshot": {"role": "consultant"},  # MUST be preserved
        },
    )
    db_session.add(task)
    await db_session.commit()

    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="cascade_op")
    result = await svc.restore_sentinel(actor="restore_op")

    assert result["drained_tasks"] >= 1
    assert result["drained_keys_total"] >= 2  # ≥ 2 residue keys cleared

    # Verify the task's config: residue gone, but brain_role_snapshot kept
    await db_session.refresh(task)
    assert "g5_pending_offspring" not in task.config
    assert "__r1b_consumed_pending_hypothesis" not in task.config
    assert task.config["brain_role_snapshot"]["role"] == "consultant"


@pytest.mark.asyncio
async def test_cache_writethrough_on_cascade_and_restore(db_session):
    """_flag_override_cache reflects post-cascade + post-restore values."""
    from backend.services.feature_flag_service import FeatureFlagService
    from backend.config import settings, _flag_override_cache

    # Clean cache to avoid pollution from other tests
    cache_keys_to_clear = (
        list(settings.LLM_ASSISTANT_SENTINEL_FLAGS) + ["ENABLE_LLM_ASSISTANT_MODE"]
    )
    for k in cache_keys_to_clear:
        _flag_override_cache.pop(k, None)

    svc = FeatureFlagService(db_session)
    await svc.set("ENABLE_LLM_ASSISTANT_MODE", True, actor="test_op")

    # Cascade flags should now show as False in cache
    for sf in settings.LLM_ASSISTANT_SENTINEL_FLAGS:
        assert _flag_override_cache.get(sf) is False, (
            f"cache miss for {sf}: got {_flag_override_cache.get(sf)!r}"
        )

    await svc.restore_sentinel(actor="restore_op")
    # All sentinel flags removed from cache (since they had no prior override)
    for sf in settings.LLM_ASSISTANT_SENTINEL_FLAGS:
        assert sf not in _flag_override_cache, (
            f"cache for {sf} should have been popped on restore"
        )
