"""Phase 4 Sprint 1 A2 — R14 task_stop_loss unit + integration tests.

Coverage:
  - check_should_pause:
    - flag OFF → never pauses
    - warmup (< MIN_ROUNDS) → never pauses even on all-zero rounds
    - 3 consecutive zero rounds → pauses with reason='consecutive_zero'
    - 1 PASS in middle → consecutive counter resets
    - EMA floor trigger with non-zero PASS counts
    - race fix: skipped_due_to_circuit_breaker=True → counter NOT advanced
  - apply_stop_loss_decision: real in-memory aiosqlite INSERT + task.status
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_task(initial_state: Dict[str, Any] = None):
    """Minimal task stub with a mutable .config dict — matches what the
    service reads/writes."""
    t = MagicMock()
    t.id = 42
    cfg = {}
    if initial_state is not None:
        cfg["stop_loss_state"] = dict(initial_state)
    t.config = cfg
    return t


def _make_settings(**overrides):
    """Build a Settings-shaped namespace; tests use kwargs to override."""
    defaults = dict(
        ENABLE_TASK_STOP_LOSS=True,
        TASK_STOP_LOSS_EMA_ALPHA=0.3,
        TASK_STOP_LOSS_MIN_ROUNDS=3,    # smaller warmup for faster tests
        TASK_STOP_LOSS_PASS_RATE_FLOOR=0.005,
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=3,
        TASK_STOP_LOSS_EXCLUDE_CB_SKIPPED=True,
    )
    defaults.update(overrides)
    ns = MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# check_should_pause — flag gating
# ---------------------------------------------------------------------------


def test_flag_off_never_pauses():
    """ENABLE_TASK_STOP_LOSS=False → service returns no_pause immediately
    even on 100 consecutive zero rounds."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(ENABLE_TASK_STOP_LOSS=False)
    task = _make_task()
    for _ in range(100):
        d = check_should_pause(
            task, round_pass_count=0, round_alpha_count=10, settings=s,
        )
        assert d.should_pause is False
    # State should never have been written
    assert "stop_loss_state" not in (task.config or {})


# ---------------------------------------------------------------------------
# consecutive_zero trigger (the main trigger per spike calibration)
# ---------------------------------------------------------------------------


def test_consecutive_zero_triggers_after_warmup():
    """After MIN_ROUNDS rounds with consecutive_zero >= cap, pause."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(
        TASK_STOP_LOSS_MIN_ROUNDS=3,
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=3,
    )
    task = _make_task()
    # Rounds 1-2: warmup, never triggers
    for _ in range(2):
        d = check_should_pause(
            task, round_pass_count=0, round_alpha_count=10, settings=s,
        )
        assert d.should_pause is False
    # Round 3: warmup satisfied + consecutive_zero=3 → TRIGGER
    d = check_should_pause(
        task, round_pass_count=0, round_alpha_count=10, settings=s,
    )
    assert d.should_pause is True
    assert d.reason == "consecutive_zero"
    assert d.consecutive_zero_rounds == 3
    assert d.rounds_completed == 3


def test_warmup_blocks_trigger():
    """Even with all-zero rounds, MIN_ROUNDS warmup must hold."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(
        TASK_STOP_LOSS_MIN_ROUNDS=5,
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=3,
    )
    task = _make_task()
    # 4 consecutive zero rounds, but MIN_ROUNDS=5 means no trigger yet
    for _ in range(4):
        d = check_should_pause(
            task, round_pass_count=0, round_alpha_count=10, settings=s,
        )
        assert d.should_pause is False, f"warmup violated at round {d.rounds_completed}"
    # 5th zero round → both warmup and consecutive_zero satisfied
    d = check_should_pause(
        task, round_pass_count=0, round_alpha_count=10, settings=s,
    )
    assert d.should_pause is True


def test_one_pass_resets_consecutive_counter():
    """A round with any PASS resets consecutive_zero to 0."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(
        TASK_STOP_LOSS_MIN_ROUNDS=2,
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=3,
        TASK_STOP_LOSS_PASS_RATE_FLOOR=0.0,  # disable EMA trigger
    )
    task = _make_task()
    # Round 1-2: zero (consec=2)
    for _ in range(2):
        d = check_should_pause(task, round_pass_count=0, round_alpha_count=10, settings=s)
        assert d.should_pause is False
    # Round 3: 1 PASS → consec resets to 0
    d = check_should_pause(task, round_pass_count=1, round_alpha_count=10, settings=s)
    assert d.should_pause is False
    assert d.consecutive_zero_rounds == 0
    # Round 4-6: 3 zeros again → trigger after the 3rd (consec=3)
    for _ in range(2):
        d = check_should_pause(task, round_pass_count=0, round_alpha_count=10, settings=s)
        assert d.should_pause is False
    d = check_should_pause(task, round_pass_count=0, round_alpha_count=10, settings=s)
    assert d.should_pause is True
    assert d.reason == "consecutive_zero"
    assert d.consecutive_zero_rounds == 3


# ---------------------------------------------------------------------------
# EMA floor trigger
# ---------------------------------------------------------------------------


def test_ema_floor_triggers_on_persistent_low_pass_rate():
    """Sustained low PASS rate (below floor) but no consecutive zeros →
    EMA floor trips. Use floor=0.05 + alternating 0-PASS / 1-PASS rounds
    (rate ~5% averaged, but EMA settles slightly below floor with α=0.3)."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(
        TASK_STOP_LOSS_MIN_ROUNDS=3,
        TASK_STOP_LOSS_EMA_ALPHA=0.3,
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=999,  # disable consec trigger
        TASK_STOP_LOSS_PASS_RATE_FLOOR=0.5,  # high floor to force EMA trip
    )
    task = _make_task()
    # 10 rounds, only 1 PASS per round of 10 → 10% < 50% floor
    triggered = False
    for r in range(20):
        d = check_should_pause(
            task, round_pass_count=1, round_alpha_count=10, settings=s,
        )
        if d.should_pause:
            triggered = True
            assert d.reason == "pass_rate_floor"
            assert d.ema_pass_rate is not None and d.ema_pass_rate < 0.5
            break
    assert triggered, "EMA floor should trip when 10% << 50% floor"


def test_ema_floor_zero_disables_trigger():
    """floor=0 → EMA never trips (only consecutive_zero can trigger)."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(
        TASK_STOP_LOSS_MIN_ROUNDS=2,
        TASK_STOP_LOSS_PASS_RATE_FLOOR=0.0,
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=999,  # disable too
    )
    task = _make_task()
    # 50 zero rounds, EMA → ~0; consec disabled; floor=0 disabled
    triggered = False
    for _ in range(50):
        d = check_should_pause(task, round_pass_count=0, round_alpha_count=10, settings=s)
        if d.should_pause:
            triggered = True
            break
    assert triggered is False, "floor=0 must disable EMA trigger entirely"


# ---------------------------------------------------------------------------
# Race fix — CB-skipped round must not advance counters
# ---------------------------------------------------------------------------


def test_race_fix_cb_skipped_round_not_counted():
    """skipped_due_to_circuit_breaker=True → counter NOT advanced even
    though round_pass_count=0. After 10 CB-skipped 'rounds', consecutive
    counter MUST still be 0."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(
        TASK_STOP_LOSS_MIN_ROUNDS=1,
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=3,
        TASK_STOP_LOSS_EXCLUDE_CB_SKIPPED=True,
    )
    task = _make_task()
    for _ in range(10):
        d = check_should_pause(
            task,
            round_pass_count=0,
            round_alpha_count=0,
            round_state={"skipped_due_to_circuit_breaker": True},
            settings=s,
        )
        assert d.should_pause is False
        # Counter never advanced
        assert d.consecutive_zero_rounds == 0
        assert d.rounds_completed == 0


def test_race_fix_disabled_via_flag():
    """EXCLUDE_CB_SKIPPED=False → CB-skipped round DOES advance counter
    (defense-in-depth flag turned off)."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(
        TASK_STOP_LOSS_MIN_ROUNDS=1,
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=3,
        TASK_STOP_LOSS_EXCLUDE_CB_SKIPPED=False,  # disable race fix
    )
    task = _make_task()
    for _ in range(3):
        d = check_should_pause(
            task,
            round_pass_count=0,
            round_alpha_count=0,
            round_state={"skipped_due_to_circuit_breaker": True},
            settings=s,
        )
    assert d.should_pause is True  # advanced regardless of skip


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_state_persists_across_calls():
    """task.config[stop_loss_state] should accumulate rounds across calls."""
    from backend.services.task_stop_loss_service import check_should_pause
    s = _make_settings(
        TASK_STOP_LOSS_MIN_ROUNDS=10,  # never trigger in this test
        TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS=999,
    )
    task = _make_task()
    check_should_pause(task, round_pass_count=2, round_alpha_count=10, settings=s)
    assert task.config["stop_loss_state"]["rounds_completed"] == 1
    check_should_pause(task, round_pass_count=3, round_alpha_count=10, settings=s)
    assert task.config["stop_loss_state"]["rounds_completed"] == 2
    # EMA reflects α·rate + (1-α)·ema
    # round1: ema = 0.3*0.2 + 0.7*0 = 0.06
    # round2: ema = 0.3*0.3 + 0.7*0.06 = 0.132
    assert task.config["stop_loss_state"]["ema"] == pytest.approx(0.132, abs=0.001)


# ---------------------------------------------------------------------------
# apply_stop_loss_decision — integration with real in-memory DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_stop_loss_inserts_event_and_pauses_task(db_session):
    """Real in-memory aiosqlite: apply_stop_loss_decision INSERTs row +
    sets task.status=PAUSED + commits."""
    from backend.models import MiningTask, TaskStopLossEvent
    from backend.services.task_stop_loss_service import (
        StopLossDecision, apply_stop_loss_decision,
    )
    from sqlalchemy import select

    task = MiningTask(
        task_name="test_task_r14",
        status="RUNNING",
        region="USA",
        universe="TOP3000",
        config={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    decision = StopLossDecision(
        should_pause=True,
        reason="consecutive_zero",
        ema_pass_rate=0.0,
        consecutive_zero_rounds=3,
        rounds_completed=8,
        ema_window_pass_count=0,
    )
    ok = await apply_stop_loss_decision(db_session, task, decision)
    assert ok is True

    # Event row inserted
    rows = (await db_session.execute(select(TaskStopLossEvent))).scalars().all()
    assert len(rows) == 1
    evt = rows[0]
    assert evt.task_id == task.id
    assert evt.trigger_reason == "consecutive_zero"
    assert evt.consecutive_zero_rounds == 3
    assert evt.rounds_completed == 8

    # Task status now PAUSED
    await db_session.refresh(task)
    assert task.status == "PAUSED"


@pytest.mark.asyncio
async def test_apply_no_pause_decision_is_noop(db_session):
    """should_pause=False → apply returns False, no INSERT, no status flip."""
    from backend.models import MiningTask, TaskStopLossEvent
    from backend.services.task_stop_loss_service import (
        StopLossDecision, apply_stop_loss_decision,
    )
    from sqlalchemy import select

    task = MiningTask(
        task_name="test_task_r14_noop",
        status="RUNNING",
        region="USA",
        universe="TOP3000",
        config={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    decision = StopLossDecision.no_pause({"ema": 0.5})
    ok = await apply_stop_loss_decision(db_session, task, decision)
    assert ok is False
    rows = (await db_session.execute(select(TaskStopLossEvent))).scalars().all()
    assert len(rows) == 0
    await db_session.refresh(task)
    assert task.status == "RUNNING"
