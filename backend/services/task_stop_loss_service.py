"""Phase 4 Sprint 1 A2 — R14 task_stop_loss service.

Plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.2

Pattern: Millennium 5%/7.5% hard stop-loss — pause a mining task whose
recent rounds are degenerate (EMA PASS rate < floor OR N consecutive
zero-PASS rounds). AIAC pre-Sprint-1 had no such guard; tasks could burn
LLM + BRAIN budget forever on a dead hypothesis space.

Spike-calibrated (2026-05-19, scripts/sprint0_baseline_spike.py):
  - production p50 round PASS rate = 0 (28 rounds, hypothesis_round_stats)
  - therefore: EMA floor is too noisy as primary trigger; main trigger is
    CONSECUTIVE_FAIL_ROUNDS=3 (3 consecutive zero-PASS rounds → pause).
    EMA floor=0.005 acts as a slow-degeneration backstop.

Race fix (Round S0-A finding):
  flat loop _run_one_round_inline returns skipped=True with
  skipped_reason='brain_auth_circuit_open' when BRAIN auth is mid-storm;
  flat loop already `continue`s before calling stop_loss_service, so
  CB-skipped rounds are naturally excluded from counters. The service
  ALSO defensively reads round_state["skipped_due_to_circuit_breaker"]
  and skips counter updates if True (defense in depth — other callers
  might not have the `continue` short-circuit).

State persistence:
  EMA + counter state lives in MiningTask.config["stop_loss_state"] dict
  ({"ema": float, "consecutive_zero": int, "rounds_completed": int,
    "ema_window_pass": int}) — flag_modified() ensures JSONB writes
  survive worker restart.

Soft-fail:
  Every helper swallows DB errors → returns NoPause() → never blocks
  round. The cost of a false negative (one extra wasted round on a dying
  task) is much lower than a false positive (auto-paused production task
  during transient DB blip).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger("services.task_stop_loss")


@dataclass(frozen=True)
class StopLossDecision:
    """Outcome of a single check_should_pause() call."""
    should_pause: bool
    reason: Optional[str] = None        # 'pass_rate_floor' / 'consecutive_zero' / None
    ema_pass_rate: Optional[float] = None
    consecutive_zero_rounds: Optional[int] = None
    rounds_completed: int = 0
    ema_window_pass_count: int = 0

    @classmethod
    def no_pause(cls, state: Optional[Dict[str, Any]] = None) -> "StopLossDecision":
        state = state or {}
        return cls(
            should_pause=False,
            reason=None,
            ema_pass_rate=state.get("ema"),
            consecutive_zero_rounds=int(state.get("consecutive_zero", 0) or 0),
            rounds_completed=int(state.get("rounds_completed", 0) or 0),
            ema_window_pass_count=int(state.get("ema_window_pass", 0) or 0),
        )


_STATE_KEY = "stop_loss_state"


def _read_state(task) -> Dict[str, Any]:
    """Pull the persisted EMA/counter state out of task.config; default to
    a fresh zero-state dict."""
    try:
        cfg = getattr(task, "config", None) or {}
        s = cfg.get(_STATE_KEY)
        if isinstance(s, dict):
            return {
                "ema": float(s.get("ema", 0.0) or 0.0),
                "consecutive_zero": int(s.get("consecutive_zero", 0) or 0),
                "rounds_completed": int(s.get("rounds_completed", 0) or 0),
                "ema_window_pass": int(s.get("ema_window_pass", 0) or 0),
            }
    except Exception:  # noqa: BLE001
        pass
    return {
        "ema": 0.0,
        "consecutive_zero": 0,
        "rounds_completed": 0,
        "ema_window_pass": 0,
    }


def _persist_state(task, state: Dict[str, Any]) -> None:
    """Write the updated state back to task.config + flag_modified.

    Caller is expected to db.commit() after this; we don't commit here
    because the calling round may have other pending writes to bundle.
    Soft-fail: any error logged + swallowed.
    """
    try:
        cfg = dict(getattr(task, "config", None) or {})
        cfg[_STATE_KEY] = dict(state)
        task.config = cfg
        try:
            flag_modified(task, "config")
        except Exception:  # noqa: BLE001
            pass
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[task_stop_loss] persist state failed for task=%s: %s",
            getattr(task, "id", "?"), ex,
        )


def check_should_pause(
    task,
    *,
    round_pass_count: int,
    round_alpha_count: int,
    round_state: Optional[Dict[str, Any]] = None,
    settings=None,
) -> StopLossDecision:
    """Update R14 EMA + consecutive_zero counter based on a finished round
    and decide whether to pause the task.

    Args:
      task: MiningTask ORM row — the EMA/counter state lives on
            task.config[_STATE_KEY] and is persisted on every call.
      round_pass_count: PASS alpha count in the round just finished.
      round_alpha_count: total alpha attempts in the round (PASS+FAIL).
      round_state: free-form dict the caller can use to signal "skip
                   counter update this round" — currently honours
                   skipped_due_to_circuit_breaker=True (race fix).
      settings: Settings instance to read tunables (PASS_RATE_FLOOR /
                CONSECUTIVE_FAIL_ROUNDS / MIN_ROUNDS / EMA_ALPHA /
                EXCLUDE_CB_SKIPPED). None → import lazily.

    Returns:
      StopLossDecision — should_pause=True means caller MUST:
        1. INSERT task_stop_loss_events row with the snapshot
        2. set task.status='PAUSED'
        3. exit the mining loop
      should_pause=False means continue + caller should _persist_state via
      the side-effect this function already performed on task.config.
    """
    if settings is None:
        from backend.config import settings as _stg
        settings = _stg

    if not bool(getattr(settings, "ENABLE_TASK_STOP_LOSS", False)):
        return StopLossDecision.no_pause()

    state = _read_state(task)
    round_state = round_state or {}

    # Race fix: CB-skipped round must NOT advance the counter.
    if (
        bool(getattr(settings, "TASK_STOP_LOSS_EXCLUDE_CB_SKIPPED", True))
        and bool(round_state.get("skipped_due_to_circuit_breaker"))
    ):
        # State unchanged; persist nothing.
        return StopLossDecision.no_pause(state)

    # Update EMA + consecutive_zero from this round
    state["rounds_completed"] = int(state["rounds_completed"]) + 1
    alpha = max(0.0, min(1.0, float(getattr(settings, "TASK_STOP_LOSS_EMA_ALPHA", 0.3))))
    rate_this_round = (
        float(round_pass_count) / float(round_alpha_count)
        if round_alpha_count > 0
        else 0.0
    )
    # EMA: ema' = α·rate + (1-α)·ema
    state["ema"] = alpha * rate_this_round + (1.0 - alpha) * float(state["ema"])
    state["ema_window_pass"] = int(state["ema_window_pass"]) + int(round_pass_count)

    if round_pass_count <= 0:
        state["consecutive_zero"] = int(state["consecutive_zero"]) + 1
    else:
        state["consecutive_zero"] = 0

    # Persist updated state via side-effect on task.config (caller commits)
    _persist_state(task, state)

    # Warmup: don't trigger before MIN_ROUNDS rounds completed
    min_rounds = int(getattr(settings, "TASK_STOP_LOSS_MIN_ROUNDS", 5))
    if state["rounds_completed"] < min_rounds:
        return StopLossDecision.no_pause(state)

    # Trigger 1: consecutive zero rounds (main trigger per spike calibration)
    consec_cap = int(getattr(settings, "TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS", 3))
    if state["consecutive_zero"] >= consec_cap:
        return StopLossDecision(
            should_pause=True,
            reason="consecutive_zero",
            ema_pass_rate=float(state["ema"]),
            consecutive_zero_rounds=int(state["consecutive_zero"]),
            rounds_completed=int(state["rounds_completed"]),
            ema_window_pass_count=int(state["ema_window_pass"]),
        )

    # Trigger 2: EMA floor (slow-degeneration backstop)
    floor = float(getattr(settings, "TASK_STOP_LOSS_PASS_RATE_FLOOR", 0.005))
    if floor > 0 and float(state["ema"]) < floor:
        return StopLossDecision(
            should_pause=True,
            reason="pass_rate_floor",
            ema_pass_rate=float(state["ema"]),
            consecutive_zero_rounds=int(state["consecutive_zero"]),
            rounds_completed=int(state["rounds_completed"]),
            ema_window_pass_count=int(state["ema_window_pass"]),
        )

    return StopLossDecision.no_pause(state)


async def apply_stop_loss_decision(
    db,
    task,
    decision: StopLossDecision,
    *,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """When decision.should_pause=True, INSERT a task_stop_loss_events row
    + set task.status='PAUSED' + commit. Returns True on success.

    Soft-fail: any error logs + returns False (caller can decide whether
    to keep mining or break out anyway based on the decision).
    """
    if not decision.should_pause:
        return False
    try:
        from backend.models import TaskStopLossEvent
        evt = TaskStopLossEvent(
            task_id=getattr(task, "id", None),
            trigger_reason=str(decision.reason or "unknown"),
            ema_pass_rate=decision.ema_pass_rate,
            consecutive_zero_rounds=decision.consecutive_zero_rounds,
            rounds_completed=decision.rounds_completed,
            ema_window_pass_count=decision.ema_window_pass_count,
            meta_data=dict(extra_meta or {}),
        )
        db.add(evt)
        # Best-effort status transition — leave the actual MiningStatus
        # validation to MiningTask.status setter (the model owns the enum).
        try:
            task.status = "PAUSED"
        except Exception:  # noqa: BLE001
            pass
        await db.commit()
        logger.warning(
            "[task_stop_loss] PAUSED task_id=%s reason=%s ema=%.4f consec=%d rounds=%d",
            getattr(task, "id", "?"),
            decision.reason,
            decision.ema_pass_rate or 0.0,
            decision.consecutive_zero_rounds or 0,
            decision.rounds_completed,
        )
        return True
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "[task_stop_loss] apply_stop_loss_decision failed task=%s: %s",
            getattr(task, "id", "?"), ex,
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return False


__all__ = [
    "StopLossDecision",
    "check_should_pause",
    "apply_stop_loss_decision",
]
