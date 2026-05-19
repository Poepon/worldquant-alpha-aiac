"""
Celery Application for Background Tasks
Handles mining tasks, feedback loops, and scheduled jobs
"""

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init, worker_process_shutdown
import asyncio

from backend.config import settings

# Create Celery app
celery_app = Celery(
    "aiac",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["backend.tasks"]
)


# P3 (2026-05-16): each worker process needs its own feature-flag override
# refresher because ``backend.config._flag_override_cache`` is per-process
# (a plain Python dict, not shared memory). Without this, an ops console
# flip via /ops/feature-flags would only take effect on the FastAPI side;
# Celery workers would keep using the env defaults until restart.
@worker_process_init.connect
def _start_feature_flag_refresher(**_kwargs):  # pragma: no cover - signal hook
    try:
        from backend.feature_flag_runtime import start_sync_refresher
        start_sync_refresher()
    except Exception as e:
        from loguru import logger
        logger.error(f"[celery_app] feature-flag refresher start failed: {e}")


@worker_process_shutdown.connect
def _stop_feature_flag_refresher(**_kwargs):  # pragma: no cover - signal hook
    try:
        from backend.feature_flag_runtime import stop_sync_refresher
        stop_sync_refresher()
    except Exception:
        pass

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
    # P1-C (2026-05-15): daily alpha-library health check at 08:00 Asia/Shanghai.
    # Output: docs/alpha_health_check/<sh-date>.json (read-only, no demotion).
    # Scheduled at 08:00 (not 07:00) to give 90min buffer after 06:30
    # refresh-os-correlation-cache + monitor-llm-op-hallucinations, since
    # the Windows Celery worker is --pool=solo (serial).
    "alpha-health-check": {
        "task": "backend.tasks.run_alpha_health_check",
        "schedule": crontab(hour=8, minute=0),
    },
    # P1-C part 2 (2026-05-15): daily hypothesis-health-check at 08:30
    # Asia/Shanghai. Scheduled AFTER 08:00 alpha-library-health-check so
    # any sync-induced metric refresh has settled before the hypothesis
    # aggregates JOIN runs. Output: docs/hypothesis_health_check/<sh-date>.json
    # (read-mostly — only mutates hypothesis trigger/scoring fields + audit
    # rows; never touches alphas / quality_status / KB).
    "hypothesis-health-check": {
        "task": "backend.tasks.run_hypothesis_health_check",
        "schedule": crontab(hour=8, minute=30),
    },
    # P2-B (2026-05-15): daily pillar-balance check at 09:00 Asia/Shanghai.
    # Runs AFTER both 08:00 alpha-library-health-check + 08:30 hypothesis-
    # health-check so any sync-induced refresh has settled before the
    # alpha+hypothesis outerjoin runs. Pure read-only — emits
    # docs/pillar_balance/<sh-date>.json (no DB mutation).
    "pillar-balance-check": {
        "task": "backend.tasks.run_pillar_balance_check",
        "schedule": crontab(hour=9, minute=0),
    },
    # P2-D (2026-05-15): daily negative-knowledge extract at 09:30
    # Asia/Shanghai. Aggregates 24h of failure signals (Alpha.metrics
    # findings / robustness / failed_tests, AlphaFailure error_type,
    # HypothesisRoundStats attribution='hypothesis') and UPSERTs to
    # knowledge_entries (entry_type='FAILURE_PITFALL'). Emits
    # docs/negative_knowledge/<sh-date>.json. Read-mostly: only mutates
    # knowledge_entries.
    "negative-knowledge-extract": {
        "task": "backend.tasks.run_negative_knowledge_extract",
        "schedule": crontab(hour=9, minute=30),
    },
    # P2-A (2026-05-16): daily macro-narrative extract at 10:00 Asia/Shanghai.
    # Runs AFTER 09:30 negative-knowledge-extract so KB writes don't compete.
    # Phase 1 (seed UPSERT) is unconditional + idempotent; Phase 2 (LLM
    # batch fill-in) is gated by ENABLE_MACRO_NARRATIVE_EXTRACT (default
    # OFF). Emits docs/macro_narratives/<sh-date>.json. Read-mostly:
    # only mutates knowledge_entries (entry_type='MACRO_NARRATIVE').
    "macro-narrative-extract": {
        "task": "backend.tasks.run_macro_narrative_extract",
        "schedule": crontab(hour=10, minute=0),
    },
    # P2-C (2026-05-16): daily regime-inference task at 10:30 Asia/Shanghai.
    # Reads docs/alpha_health_check/<sh-date>.json for the last 7 days per
    # active region (USA/CHN/EUR/ASI/GLB), EWMA-smooths the GREEN+YELLOW
    # pass-rate into a 5-bucket regime, and writes the result to Redis
    # (aiac:current_regime:{region}) + docs/regime_state/<sh-date>.json.
    # Read-mostly: no DB writes — only Redis SETEX + on-disk archive.
    # Gated by ENABLE_REGIME_INFERENCE (default OFF, S1). Runs LAST so
    # all upstream JSON sources have settled.
    "regime-infer": {
        "task": "backend.tasks.run_regime_infer",
        "schedule": crontab(hour=10, minute=30),
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
    # V-22.12.1 (2026-05-13): fallback sweep for IQC marginal audits.
    # The V-22.12 enqueue inside refresh_can_submit_for_alpha can miss alphas
    # that flipped can_submit=true via other paths (BRAIN sync, broker outage,
    # pre-V-22.12 worker). Sweep runs every 30 minutes, capped at 50 alphas
    # per sweep, and enqueues audits idempotently (alphas with
    # metrics._iqc_marginal are skipped).
    "iqc-audit-backfill-sweep": {
        "task": "backend.tasks.iqc_audit_backfill_sweep",
        "schedule": 1800,   # every 30 minutes
    },
    # V-27.147: portfolio-skeleton cache refresh fallback. submit_alpha
    # refreshes the cache inline on each successful submit, but that is
    # best-effort — this beat sweep every 6h is the safety net so a failed
    # inline refresh can't leave the cache stale indefinitely.
    "refresh-portfolio-skeletons": {
        "task": "backend.tasks.refresh_portfolio_skeletons_all",
        "schedule": crontab(hour="*/6", minute=45),
    },
    # P3-Q10 PR2d (2026-05-18): daily Q10 pyqlib-prescreen telemetry report
    # at 09:00 Asia/Shanghai. Aggregates the last 24h of qlib_prescreen_log
    # (verdict / mode / engine / latency / cost_saved / fn_rate) and prints
    # to Celery worker stdout — also posts to Slack when Q10_SLACK_WEBHOOK
    # env var is set. Pure read aggregation (no DB mutation). Co-located at
    # 09:00 with pillar-balance-check; on the Windows solo-pool worker they
    # serialize, which is fine since both are read-only and well under the
    # 1h task_time_limit.
    "q10-layer-telemetry": {
        "task": "backend.tasks.run_q10_layer_telemetry",
        "schedule": crontab(hour=9, minute=0),
    },
    # P3-R1b.3 review LOW (2026-05-18): weekly 90-day pruner for
    # FAILURE_PITFALL entries with meta_data->'failure_tree'. R1b.3 writes
    # one KnowledgeEntry per unique root_skeleton at mining-round boundaries;
    # at 50 alpha/round × N rounds × multi-root mutations the table grows
    # linearly with no TTL. Pruner DELETEs rows older than
    # ``R1B_FAILURE_TREE_RETENTION_DAYS`` (default 90). Sunday 04:00
    # Asia/Shanghai — off-peak, weekly cadence is fine for a 90-day TTL.
    "r1b-failure-tree-pruner": {
        "task": "backend.tasks.run_failure_tree_pruner",
        "schedule": crontab(hour=4, minute=0, day_of_week=0),
    },
    # P3-R8 query log review LOW (2026-05-18): weekly 90-day pruner for
    # r8_query_log table. Per-query telemetry row written by
    # query_hierarchical when ENABLE_R8_QUERY_LOG flag is ON; with
    # default OFF this is a no-op safety net, but long-term ON promotion
    # would let the table grow unbounded. Pruner DELETEs rows older than
    # ``R8_QUERY_LOG_RETENTION_DAYS`` (default 90). Sunday 04:30
    # Asia/Shanghai — staggered 30min after r1b-failure-tree-pruner
    # (04:00 SH) so they don't fight for DB resources on the
    # Windows --pool=solo worker.
    "r8-query-log-pruner": {
        "task": "backend.tasks.run_r8_query_log_pruner",
        "schedule": crontab(hour=4, minute=30, day_of_week=0),
    },
    # Phase 4 Sprint 3 A5.1 G10 (2026-05-20): Sunday 03:00 SH weekly distill
    # of past 7d PASS alphas into distilled_logic_library. flag-gated by
    # ENABLE_G10_LOGIC_DISTILL (default OFF → task fires but no-ops).
    # Cost-capped at LOGIC_DISTILL_MAX_COST_USD_PER_WEEK ($5 default).
    # Scheduled 1h before r1b-failure-tree-pruner (04:00) so DB writes
    # finish before the pruner sweep.
    "g10-weekly-logic-distill": {
        "task": "backend.tasks.run_weekly_logic_distill",
        "schedule": crontab(hour=3, minute=0, day_of_week=0),
    },
    # Canary monitoring (2026-05-18): every-6h red-flag check post v1.3
    # ship. Runs the 5 SQL checks from docs/production_canary_sop_2026_05_18.md
    # §4 against the trailing 6h window. Red rows ERROR-log with rollback
    # target so operator greps .celery.err. Slot at */6:15 so it's between
    # update-operator-stats (*/6:00) and refresh-portfolio-skeletons (*/6:45)
    # without DB contention on the Windows --pool=solo worker.
    "canary-redflag-check": {
        "task": "backend.tasks.run_canary_redflag_check",
        "schedule": crontab(hour="*/6", minute=15),
    },
}
