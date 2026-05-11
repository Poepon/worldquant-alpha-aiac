"""
Celery Application for Background Tasks
Handles mining tasks, feedback loops, and scheduled jobs
"""

from celery import Celery
from celery.schedules import crontab
import asyncio

from backend.config import settings

# Create Celery app
celery_app = Celery(
    "aiac",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["backend.tasks"]
)

# Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hour max per task
    worker_prefetch_multiplier=1,  # Fair scheduling
)

# Scheduled tasks (Celery Beat)
celery_app.conf.beat_schedule = {
    # Daily feedback analysis at 23:00
    "daily-feedback-analysis": {
        "task": "backend.tasks.run_daily_feedback",
        "schedule": crontab(hour=23, minute=0),
    },
    # Update operator stats every 6 hours
    "update-operator-stats": {
        "task": "backend.tasks.update_operator_stats",
        "schedule": crontab(hour="*/6", minute=0),
    },
    # Sync datasets from BRAIN daily at 06:00
    "sync-datasets": {
        "task": "backend.tasks.sync_datasets",
        "schedule": crontab(hour=6, minute=0),
    },
    # W0.5: refresh OS-alpha PnL cache daily at 06:30 (after dataset sync)
    "refresh-os-correlation-cache": {
        "task": "backend.tasks.refresh_os_correlation_cache",
        "schedule": crontab(hour=6, minute=30),
    },
    # PR2: refresh KB-referenced alpha metrics + demote drifters at 06:15
    # (between sync-datasets and refresh-os-correlation-cache to avoid BRAIN
    # rate-limit overlap with sync_datasets, which can run for ~10 minutes).
    "refresh-kb-referenced-alphas": {
        "task": "backend.tasks.refresh_kb_referenced_alphas",
        "schedule": crontab(hour=6, minute=15),
    },
    # Plan v5+ §Phase 3 prep T02: weekly readiness check at Mon 04:00.
    # Output written to docs/phase3_readiness/<date>.json so the trajectory
    # of GO/NO-GO is visible over the May→July observation period.
    "phase3-readiness-check": {
        "task": "backend.tasks.run_phase3_readiness_check",
        "schedule": crontab(day_of_week="mon", hour=4, minute=0),
    },
    # V-19.7: revive dead CONTINUOUS_CASCADE sessions. Detects worker crash /
    # silent stalls via task.last_alpha_persisted_at < NOW()-15min and
    # re-dispatches a fresh celery worker. Grace period skips fresh sessions.
    "watchdog-revive-dead-sessions": {
        "task": "backend.tasks.watchdog_revive_dead_sessions",
        "schedule": 300,   # every 5 minutes (in seconds)
    },
    # V-19.7: BRAIN daily simulate quota guard. Counts today's alpha rows;
    # at >= 90% of BRAIN_DAILY_SIMULATE_LIMIT pauses every active
    # CONTINUOUS_CASCADE session to avoid hitting BRAIN rate-limit walls.
    "quota-guard-pause-at-threshold": {
        "task": "backend.tasks.quota_guard_pause_at_threshold",
        "schedule": 600,   # every 10 minutes
    },
    # V-22.3 long-term enforcement (2026-05-11): scan active KB entries for
    # hallucinated op names (LLM emits ops not in BRAIN registry). Catches
    # writes that bypass the canonicalize chain + drift after sync_datasets.
    # Soft-deactivates affected entries + writes daily report under
    # docs/llm_op_monitor/<date>.md. Runs at 06:30 after sync_datasets
    # (06:00) and refresh_kb_referenced_alphas (06:15) so the Operator
    # whitelist is fresh.
    "monitor-llm-op-hallucinations": {
        "task": "backend.tasks.monitor_llm_op_hallucinations",
        "schedule": crontab(hour=6, minute=30),
    },
}
