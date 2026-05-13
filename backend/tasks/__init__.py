"""
Tasks Module - Celery background tasks

This module organizes Celery tasks by category:
- mining_tasks: Mining task execution
- feedback_tasks: Feedback analysis and learning
- sync_tasks: Data synchronization with BRAIN

Common utilities are provided here.
"""

import asyncio
from backend.celery_app import celery_app

# Common utility for running async code in Celery
def run_async(coro):
    """Helper to run async functions in Celery tasks."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Re-export all tasks for backward compatibility
from backend.tasks.mining_tasks import run_mining_task
from backend.tasks.feedback_tasks import (
    run_daily_feedback,
    update_operator_stats,
    learn_from_alpha,
)
from backend.tasks.sync_tasks import (
    sync_datasets,
    sync_datasets_from_brain,
    sync_operators_from_brain,
    sync_fields_from_brain,
    sync_user_alphas,
    refresh_os_correlation_cache,
)
# PR2: tier-system refresh beat
from backend.tasks.refresh_tasks import refresh_kb_referenced_alphas
# V-22.12: auto IQC marginal-contribution audit after can_submit flips True
from backend.tasks.refresh_tasks import audit_iqc_marginal_for_alpha
# Phase 3 prep T02: weekly readiness check
from backend.tasks.phase3_tasks import run_phase3_readiness_check
# V-19.7: persistent mining service watchdog + BRAIN quota guard
from backend.tasks.session_watchdog import (
    watchdog_revive_dead_sessions,
    quota_guard_pause_at_threshold,
)
# V-22.3 long-term: daily LLM-op-hallucination monitor
from backend.tasks.llm_op_monitor import monitor_llm_op_hallucinations

__all__ = [
    # Utilities
    "run_async",
    "celery_app",
    # Mining
    "run_mining_task",
    # Feedback
    "run_daily_feedback",
    "update_operator_stats",
    "learn_from_alpha",
    # Sync
    "sync_datasets",
    "sync_datasets_from_brain",
    "sync_operators_from_brain",
    "sync_fields_from_brain",
    "sync_user_alphas",
    "refresh_os_correlation_cache",
    # PR2: tier system
    "refresh_kb_referenced_alphas",
    # V-22.12: IQC marginal audit hook
    "audit_iqc_marginal_for_alpha",
    # V-19.7: persistent mining service watchdog
    "watchdog_revive_dead_sessions",
    "quota_guard_pause_at_threshold",
    # V-22.3 long-term: LLM op hallucination monitor
    "monitor_llm_op_hallucinations",
]
