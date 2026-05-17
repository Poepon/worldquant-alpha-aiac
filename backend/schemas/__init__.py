"""backend.schemas — Pydantic schemas for type-safe boundary I/O.

Phase 1.5-Pydantic (plan v1.3 §4) introduces this module to provide
typed views into JSONB columns like MiningTask.config / ExperimentRun.
config_snapshot — without forcing internal dict-access code to refactor.
Use at boundaries (router / workflow / agent ingress) for IDE auto-
complete + validation; internal writes still use dict + flag_modified()
per MF-6 (Pydantic instances do NOT trigger SQLAlchemy dirty).
"""
from backend.schemas.task_config import (
    BrainRoleSnapshot,
    ContextualBanditState,
    TaskConfig,
    WatchdogReviveInfo,
)

__all__ = [
    "BrainRoleSnapshot",
    "ContextualBanditState",
    "TaskConfig",
    "WatchdogReviveInfo",
]
