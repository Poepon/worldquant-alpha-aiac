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
# Phase 1c-delete: mining_tasks.py (run_mining_task / FLAT / ONESHOT) deleted.
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
    refresh_portfolio_skeletons_all,
)
# PR2: tier-system refresh beat
from backend.tasks.refresh_tasks import refresh_kb_referenced_alphas
# V-22.12: auto IQC marginal-contribution audit after can_submit flips True
from backend.tasks.refresh_tasks import audit_iqc_marginal_for_alpha
# V-22.12.1: beat fallback sweep — backfills audits missed by the
# refresh_can_submit_for_alpha hook (BRAIN sync paths, broker outages, etc.)
from backend.tasks.refresh_tasks import iqc_audit_backfill_sweep
# V-19.7: BRAIN quota guard (Phase 1c-delete: watchdog_revive_dead_sessions
# retired — lease-recycle is the pool's sole recovery path).
from backend.tasks.session_watchdog import (
    quota_guard_pause_at_threshold,
)
# V-22.3 long-term: daily LLM-op-hallucination monitor
from backend.tasks.llm_op_monitor import monitor_llm_op_hallucinations
# P1-C (2026-05-15): daily alpha-library health check task
from backend.tasks.alpha_health_check import run_alpha_health_check
# P1-C part 2 (2026-05-15): daily hypothesis-health-check task
from backend.tasks.hypothesis_health_check import run_hypothesis_health_check
# P2-B (2026-05-15): daily pillar-balance-check task
from backend.tasks.pillar_balance_check import run_pillar_balance_check
# P2-D (2026-05-15): daily negative-knowledge extract task
from backend.tasks.negative_knowledge_extract import run_negative_knowledge_extract
# P2-A (2026-05-16): daily macro-narrative extract task
from backend.tasks.macro_narrative_extract import run_macro_narrative_extract
# P2-C regime-inference task retired in Phase 1c-delete (regime cluster removed).
# P3-Q10 PR2d (2026-05-18): daily Q10 telemetry report beat task
from backend.tasks.q10_tasks import run_q10_layer_telemetry
# P3-R1b.3 failure_tree pruner retired in Phase 1c-delete (R1b machine removed).
# P3-R8 query log review LOW (2026-05-18): weekly 90-day r8_query_log pruner
from backend.tasks.r8_tasks import run_r8_query_log_pruner
# Canary monitoring (2026-05-18): every-6h red-flag check post v1.3 ship
from backend.tasks.canary_tasks import run_canary_redflag_check
# Phase 4 Sprint 3 A5.1 G10 (2026-05-20): Sunday 03:00 SH weekly logic distill
from backend.tasks.logic_distill_tasks import run_weekly_logic_distill  # noqa: F401
# Phase 4 Tier E E1 (2026-05-20): Sunday 04:45 SH cognitive-layer bandit reward
from backend.tasks.cognitive_layer_bandit_tasks import run_cognitive_layer_bandit_update  # noqa: F401
# Breadth (2026-05-22): daily dataset-steering value-bandit mining_weight refresh
from backend.tasks.dataset_weight_refresh import run_dataset_weight_refresh  # noqa: F401
# Orchestrator retired in Phase 1c-delete (resident pool needs no relaunch).
# R1b outcome-reconcile retired in Phase 1c-delete (R1b machine removed).
# Data quality (2026-05-22): self-heal invalid (BRAIN-rejected) data fields
from backend.tasks.datafield_prune import prune_invalid_datafields  # noqa: F401
# Phase 16-A optimization closure Stage A (2026-05-28) — 6h beat task
# that scans near-gate alphas and runs SettingsSweepGenerator cycles.
# Gated by ENABLE_OPTIMIZATION_LOOP (default OFF).
from backend.tasks.optimization_tasks import (  # noqa: F401
    run_optimization_cycle,
    # Manual blueprint-optimization (2026-06-03): user picks an alpha in the
    # UI → POST /alphas/{id}/optimize dispatches this single-alpha cycle.
    run_manual_optimization_cycle,
)

# Auto-submit beat (2026-06-04): automates the orthogonal backlog drain.
# Default OFF + default mode 'shadow' (logs would-submit list, no real submit).
# run_can_submit_refresh keeps the backlog's can_submit + _brain_can_submit_at fresh.
from backend.tasks.auto_submit_tasks import (  # noqa: F401
    run_auto_submit_cycle,
    run_can_submit_refresh,
)

# Phase 1b B5 (four-pool decoupling): pool scheduler + lease-recycle beats.
# Both gate on ENABLE_POOL_PIPELINE (default OFF) → inert until 1c-flip.
from backend.tasks.pool_tasks import (  # noqa: F401
    run_pool_scheduler,
    run_pool_lease_recycle,
)

# Pool Phase 2 (1c): cognitive reconcile beat (gated on
# ENABLE_POOL_COGNITIVE_RECONCILE, default OFF → inert until flipped).
from backend.tasks.cognitive_reconcile_tasks import run_pool_cognitive_reconcile  # noqa: F401

# Regime-turn monitor beat (greenfield branch B; gated on ENABLE_REGIME_MONITOR,
# default OFF → inert until flipped). Re-sims submitted winners on current data.
from backend.tasks.regime_monitor_tasks import run_regime_monitor  # noqa: F401

from backend.tasks.resim_backlog_tasks import resim_backlog_current  # noqa: F401

from backend.tasks.field_ledger_refresh import run_field_ledger_refresh  # noqa: F401

__all__ = [
    # Utilities
    "run_async",
    "celery_app",
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
    # V-27.147: portfolio-skeleton cache refresh beat fallback
    "refresh_portfolio_skeletons_all",
    # PR2: tier system
    "refresh_kb_referenced_alphas",
    # V-22.12: IQC marginal audit hook
    "audit_iqc_marginal_for_alpha",
    # V-22.12.1: IQC audit beat fallback sweep
    "iqc_audit_backfill_sweep",
    # V-19.7: BRAIN quota guard (watchdog_revive retired Phase 1c-delete)
    "quota_guard_pause_at_threshold",
    # V-22.3 long-term: LLM op hallucination monitor
    "monitor_llm_op_hallucinations",
    # P1-C: daily alpha-library health check
    "run_alpha_health_check",
    # P1-C part 2: daily hypothesis-health-check
    "run_hypothesis_health_check",
    # P2-B: daily pillar-balance-check
    "run_pillar_balance_check",
    # P2-D: daily negative-knowledge extract
    "run_negative_knowledge_extract",
    # P2-A: daily macro-narrative extract
    "run_macro_narrative_extract",
    # P3-Q10 PR2d: daily Q10 telemetry report
    "run_q10_layer_telemetry",
    # P3-R8 query log review LOW: weekly r8_query_log pruner
    "run_r8_query_log_pruner",
    # Canary monitoring: every-6h red-flag check
    "run_canary_redflag_check",
    # Phase 4 Sprint 3 A5.1 G10: Sunday 03:00 SH weekly logic distill
    "run_weekly_logic_distill",
    # Phase 4 Tier E E1: Sunday 04:45 SH cognitive-layer bandit reward
    "run_cognitive_layer_bandit_update",
    "run_dataset_weight_refresh",
    "prune_invalid_datafields",
    # Phase 1b B5: pool scheduler + lease-recycle beats (gated on ENABLE_POOL_PIPELINE)
    "run_pool_scheduler",
    "run_pool_lease_recycle",
    # Pool Phase 2 (1c): cognitive reconcile beat (gated on ENABLE_POOL_COGNITIVE_RECONCILE)
    "run_pool_cognitive_reconcile",
]
