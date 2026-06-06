"""
Celery Application for Background Tasks
Handles mining tasks, feedback loops, and scheduled jobs
"""

# 2026-05-25: ensure the repo root is on sys.path for Celery worker processes.
# backend/ and scripts/ are both namespace packages (no __init__.py). celery
# -A backend.celery_app loads `backend` at startup, but a task doing
# `from scripts.X import ...` at RUN time (q10_tasks' telemetry beat) failed
# with "No module named 'scripts'" because the worker's runtime sys.path did
# not include the repo root. Inserting it here — executed once when the worker
# imports celery_app — makes every root-level module importable from tasks.
import sys as _sys
from pathlib import Path as _Path

_PROJECT_ROOT = str(_Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in _sys.path:
    _sys.path.insert(0, _PROJECT_ROOT)

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


@worker_process_init.connect
def _reset_brain_sim_slot_counter(**_kwargs):  # pragma: no cover - signal hook
    """Clear the cross-process BRAIN sim slot counter on worker startup.

    A prior worker force-kill / round-timeout cancellation that leaked slots
    (orphaned sim never reached _release_sim_slot) would otherwise leave
    ``brain:concurrent_sims`` pinned at the limit, blocking EVERY sim on
    _acquire_sim_slot until the 10-min TTL expires (the 2026-05-31 cascade that
    wedged the pool and survived manual resets). Safe to clear here: no sim is
    in-flight at process init. Belt-and-suspenders with the cancellation-safe
    release in BrainAdapter.simulate_alpha (which prevents the leak going fwd).
    """
    try:
        from backend.tasks.redis_pool import get_redis_client
        _r = get_redis_client()
        # Guarded reset (Phase 1b 终审 #5): under the resident HG/S/E pool, sibling
        # S workers may legitimately hold brain:concurrent_sims when ONE pool
        # process restarts — clearing it then would zero a shared counter that
        # other workers' in-flight sims still own, letting the pool exceed the
        # BRAIN cap → 429/wedge. Only clear when NO pool worker is registered
        # alive (the supervisor SADD/SREMs pool:workers:alive in B6). In the
        # FLAT-only world the registry is never written → scard()==0 → clears
        # exactly as before. Literal key mirrors BrainAdapter._SLOT_COUNTER_KEY.
        try:
            _alive = int(_r.scard("pool:workers:alive") or 0)
        except Exception:
            _alive = 0
        if _alive == 0:
            _r.delete("brain:concurrent_sims")
        else:
            from loguru import logger
            logger.info(
                f"[celery_app] {_alive} pool worker(s) alive — skip brain:concurrent_sims reset"
            )
    except Exception as e:
        from loguru import logger
        logger.warning(f"[celery_app] BRAIN sim slot counter reset failed: {e}")


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
    # NOTE (2026-05-21): task_time_limit does NOT fire on Windows --pool=solo
    # (no signals/subprocess). Real per-call/per-round deadlines live in-task via
    # asyncio.wait_for (llm_service.call + mining_tasks.pipeline round).
    worker_prefetch_multiplier=1,  # Fair scheduling
    # Phase 1c-delete: the long-running FLAT run_mining_task (and its dedicated
    # `mining` queue route) was retired — the HG/S/E pool runs as standalone
    # processes under the supervisor, not as a Celery task. Beat maintenance now
    # runs on the single default `celery` queue.
    task_default_queue="celery",
)

# Scheduled tasks (Celery Beat)
celery_app.conf.beat_schedule = {
    # Phase 1b B5 (four-pool decoupling): pool scheduler + lease-recycle. Both
    # gate on ENABLE_POOL_PIPELINE (default OFF) → these fire but no-op until
    # 1c-flip, so registration here is inert. Scheduler feeds hyp_intent every
    # 5 min; lease-recycle reclaims dead-worker rows every 2 min.
    "pool-scheduler": {
        "task": "backend.tasks.run_pool_scheduler",
        "schedule": crontab(minute="*/5"),
    },
    "pool-lease-recycle": {
        "task": "backend.tasks.run_pool_lease_recycle",
        "schedule": crontab(minute="*/2"),
    },
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
    # P2.C (2026-05-20): sync user alphas from BRAIN every 6h. Closes the
    # local-vs-BRAIN parity gap that otherwise drifts (it was sync'd only on
    # manual /alphas/sync clicks — last ran 7 days stale). Off-minute :50
    # avoids the 06:00/06:15/06:30 daily BRAIN-touching cluster. Skips
    # cleanly when BRAIN_AUTH_CIRCUIT is open (sync_user_alphas guard).
    "sync-user-alphas": {
        "task": "backend.tasks.sync_user_alphas",
        "schedule": crontab(hour="*/6", minute=50),
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
    # P2-C regime-inference beat retired in Phase 1c-delete (regime cluster removed).
    # V-19.7 watchdog-revive beat retired in Phase 1c-delete (lease-recycle is the
    # pool's sole recovery path; double-revive risk eliminated).
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
    # P3-R1b.3 failure-tree pruner beat retired in Phase 1c-delete (R1b machine removed).
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
    # Phase 4 Tier E E1 (2026-05-20): Sunday 04:45 SH cognitive-layer bandit
    # reward update. Aggregates _cognitive_layer_used PASS/FAIL → per-layer
    # Beta posterior so COGNITIVE_LAYER_SELECT_MODE='bandit' works. Staggered
    # 04:45 — after r1b-pruner (04:00) + r8-query-pruner (04:30), before the
    # 06:00 sync jobs. flag-gated → no-op when R8-v3 OFF.
    "r8v3-cognitive-layer-bandit-update": {
        "task": "backend.tasks.run_cognitive_layer_bandit_update",
        "schedule": crontab(hour=4, minute=45, day_of_week=0),
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
    # Breadth dataset-steering bandit (2026-05-22): daily 05:15 SH refresh of
    # DatasetMetadata.mining_weight from the discounted Beta-Bernoulli posterior
    # over per-dataset book-marginal yield. flag-gated by
    # ENABLE_DATASET_VALUE_BANDIT (default OFF → task fires but no-ops). Slotted
    # 05:15 — before sync_datasets (06:00, which does NOT touch mining_weight,
    # verified) so the freshly-sampled weights steer the day's FLAT picks. Pure
    # DB read/write, well under the 1h task_time_limit on the solo-pool worker.
    "dataset-weight-refresh": {
        "task": "backend.tasks.run_dataset_weight_refresh",
        "schedule": crontab(hour=5, minute=15),
    },
    # Orchestrator periodic-scan beat retired in Phase 1c-delete (resident pool
    # needs no relaunch — supervisor keeps HG/S/E processes alive).
    # R1b outcome-reconcile beat retired in Phase 1c-delete (R1b machine removed).
    # Self-healing data-field prune (2026-05-22): daily 06:20 SH deactivate
    # datafields BRAIN rejects as "Invalid data field" (stale catalog rows the
    # dataset bandit surfaces by steering onto dormant datasets). Slotted
    # AFTER sync_datasets (06:00) so a fresh sync that re-adds a stale field is
    # re-pruned the same morning. Deterministic + reversible; never raises.
    "datafield-prune": {
        "task": "backend.tasks.prune_invalid_datafields",
        "schedule": crontab(hour=6, minute=20),
    },
    # Phase 16-A optimization closure Stage A (2026-05-28): every 6h at
    # :15 past the hour (Asia/Shanghai 02:15 / 08:15 / 14:15 / 20:15).
    # minute=15 dodges the 06:00 sync-datasets / 06:15 refresh-kb / 06:20
    # datafield-prune cluster. 08:15 SH falls 15min after the UTC 00:00
    # BRAIN quota reset — fresh budget on the morning cycle. Gated by
    # ENABLE_OPTIMIZATION_LOOP (default OFF). Per plan §6 + §8 Q5.
    "run-optimization-cycle": {
        "task": "backend.tasks.run_optimization_cycle",
        "schedule": crontab(hour="*/6", minute=15),
    },
    # Auto-submit beat (2026-06-04): every 6h at :35 past the hour (dodges the
    # :00 sync / :15 opt / :20 prune / :30 corr-cache cluster). Automates the
    # orthogonal backlog drain. Gated by ENABLE_AUTO_SUBMIT (default OFF) +
    # AUTO_SUBMIT_MODE (default 'shadow' — logs would-submit, never submits).
    "run-auto-submit-cycle": {
        "task": "backend.tasks.run_auto_submit_cycle",
        "schedule": crontab(hour="*/6", minute=35),
    },
    # can_submit periodic refresh (2026-06-04): every 6h at :50, re-checks the
    # can_submit=True backlog against BRAIN (stalest-first) so the verdict + its
    # _brain_can_submit_at freshness stamp stay < the auto-submit G4 window and
    # stale/correlated alphas get demoted out of the backlog. Gated by
    # ENABLE_CAN_SUBMIT_REFRESH (default OFF). Read-only BRAIN GETs, paced 1/s.
    "run-can-submit-refresh": {
        "task": "backend.tasks.run_can_submit_refresh",
        "schedule": crontab(hour="*/6", minute=50),
    },
}
