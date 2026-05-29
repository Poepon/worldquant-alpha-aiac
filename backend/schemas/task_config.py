"""Phase 1.5-Pydantic (plan v1.3 §4, 2026-05-17) — TaskConfig + sub-models.

Pydantic-typed view of ``MiningTask.config`` JSONB column. ENUMERATES all
known keys from Phase 0 + Phase 1. Used at boundary read sites
(workflow.py / _role_helpers.py); internal mutators continue using
``task.config[key] = value + flag_modified(task, "config")`` per MF-6
trap ([[feedback_r1a_dedicated_log_table]] — Pydantic instances do NOT
trigger SQLAlchemy JSONB dirty).

Key contract (plan v1.3 §4.4):
- Boundary reads use ``TaskConfig.model_validate(task.config or {})``
- Top-level ``extra='allow'`` — Phase 2+ may add keys without breaking
- ALL sub-models also explicit ``extra='allow'`` per V1.2-B5
  (Pydantic v2 default 'ignore' silently drops unknown sub-model keys)
- New fields are append-only (memory [[feedback_forward_compat_metadata_hook]])
- Never delete fields (data persists in historical rows)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class BrainRoleSnapshot(BaseModel):
    """Phase 0 P3-Brain role-switch snapshot — frozen at task start.

    Written by ``backend/tasks/mining_tasks.py:225`` (run_mining_task);
    read by ``backend/agents/graph/workflow.py:353``. Captures the
    effective brain capability config at the moment the task begins so
    later config flips don't affect in-flight task semantics.

    [V1.2-B5] explicit ``extra='allow'`` — Phase 3+ may add new role
    snapshot keys (e.g. ``effective_neutralization_overrides``) and the
    default Pydantic v2 'ignore' would silently drop them.
    """
    model_config = ConfigDict(extra="allow")

    brain_consultant_mode_at_start: bool
    effective_default_test_period: str  # e.g. "P0Y" / "P3Y"
    effective_sharpe_submit_min: float
    effective_region_universes: Dict[str, List[str]]


class ContextualBanditState(BaseModel):
    """Phase 1 R2/Q7 ContextualDirectionBandit persistence shape.

    Written by ``mining_agent._persist_bandit_state`` (mining_agent.py:991);
    deserialized by ``ContextualDirectionBandit.from_dict``
    (evolution_strategy.py:608).

    [V1.2-B5] explicit ``extra='allow'`` — Phase 2+ may add bandit fields
    (e.g. ``arm_metadata`` / ``segment_history`` / ``reward_buffer``).
    """
    model_config = ConfigDict(extra="allow")

    v: int = 1
    arm_names: List[str] = Field(default_factory=list)
    cold_threshold: int = 5
    global_arms: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    segments: Dict[str, Dict[str, Dict[str, Any]]] = Field(default_factory=dict)
    last_select: Optional[List[Any]] = None  # [[ctx_tuple_as_list], arm_name]


class WatchdogReviveInfo(BaseModel):
    """ExperimentRun.config_snapshot inheritance key — written by
    session_watchdog.py:233 when a task is revived from cascade death.

    [V1.2-B5] explicit ``extra='allow'`` — kind-specific payload keys
    (e.g. ``original_round_idx``) may be added without schema change.
    """
    model_config = ConfigDict(extra="allow")

    at: str  # ISO 8601 datetime when revive happened
    kind: str  # 'CONTINUOUS_CASCADE' or 'DISCRETE'
    prior_run_id: Optional[int] = None


class TaskConfig(BaseModel):
    """Pydantic-typed view of MiningTask.config JSONB.

    Phase 1.5-Pydantic boundary contract:
    - Read: ``TaskConfig.model_validate(task.config or {})``
    - Write: NOT through Pydantic — use ``task.config[key] = value``
      + ``flag_modified(task, "config")`` per MF-6 trap.
    - Unknown top-level keys tolerated via ``extra='allow'``; Phase 2+
      may tighten to ``'forbid'`` after auditing all writers.

    DO NOT add fields without updating this docstring + plan §4.3.
    Append-only schema — never rename / delete fields.
    """
    model_config = ConfigDict(extra="allow")

    # Phase 0 — P3-Brain role snapshot
    brain_role_snapshot: Optional[BrainRoleSnapshot] = None

    # Phase 0 — F-5 A/B variant assignment (int from F-5, str from older code)
    hypothesis_centric_variant: Optional[Union[int, str]] = None

    # Phase 1 R2/Q7 — ContextualDirectionBandit state
    contextual_bandit_v1: Optional[ContextualBanditState] = None

    # ExperimentRun.config_snapshot inherited keys (cascade watchdog)
    watchdog_revive: Optional[WatchdogReviveInfo] = None
    cascade_lock_token: Optional[str] = None

    # Orchestrator Sub-phase 1 (2026-05-29) — 标 task 是谁启的:
    #   "manual" (default,向后兼容历史 task)
    #   "orchestrator" (auto-orchestrator 启的,orchestrator 决策影响范围内)
    # orchestrator 只对 launched_by="orchestrator" 的 task 做让位决策,user
    # 手动启的 task 完全不动(Q6 DECIDED)。
    launched_by: Optional[str] = None
