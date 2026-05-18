"""Phase 3 Q10 PR2d: daily telemetry beat task wrapper.

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md §9 + §15.2.

Wraps ``scripts/q10_layer_telemetry_report.py:main`` so the report can be
driven by Celery Beat (daily 09:00 Asia/Shanghai) instead of an external
cron line. The script's ``main(argv=[])`` is rc-safe: defaults to a
24-hour window, prints to stdout, and posts to Slack only when
``Q10_SLACK_WEBHOOK`` env var is set.

Idempotent: each beat tick is an independent read-only aggregation;
nothing in qlib_prescreen_log is mutated.
"""
from __future__ import annotations

from loguru import logger

from backend.celery_app import celery_app


@celery_app.task(name="backend.tasks.run_q10_layer_telemetry")
def run_q10_layer_telemetry(*, window_hours: int = 24) -> int:
    """Beat-triggered wrapper around ``q10_layer_telemetry_report.main``.

    Returns the script's exit code (0 = OK / INFO, 2 = ALERT path only when
    ``--exit-nonzero-on-alert`` is set — not used here so we stay rc=0 for
    Celery success). Soft-fails to rc=1 + log on import / runtime error to
    avoid retry storms.
    """
    try:
        from scripts.q10_layer_telemetry_report import main as q10_main
    except Exception as ex:
        logger.error(f"[q10_layer_telemetry] import failed: {ex}")
        return 1
    try:
        return int(q10_main(argv=["--window-hours", str(int(window_hours))]))
    except SystemExit as ex:
        # main() can argparse-exit; surface the code without raising.
        return int(getattr(ex, "code", 0) or 0)
    except Exception as ex:
        logger.error(f"[q10_layer_telemetry] runtime failed: {ex}")
        return 1
