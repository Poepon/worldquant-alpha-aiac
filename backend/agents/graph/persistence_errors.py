"""V-19.2 (2026-05-05): persistent error log for mining persistence failures.

Bypasses the loguru → stderr → Celery `--logfile` truncation chain by writing
directly to logs/persistence_errors.log. Used by workflow.run_with_persistence
and nodes/persistence._incremental_save_alphas when a per-row savepoint rolls
back so we can see the IntegrityError / DataError that would otherwise be
invisible.
"""
from __future__ import annotations

import os
import traceback as _tb
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_LOG_PATH = Path(__file__).resolve().parents[3] / "logs" / "persistence_errors.log"


def log_persistence_error(
    *,
    task_id: Optional[int],
    phase: str,
    exc: BaseException,
    alpha_id: Optional[str] = None,
    expression: Optional[str] = None,
    quality_status: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Append one entry to logs/persistence_errors.log.

    phase: where the failure happened — "alpha_insert" / "failure_insert" /
    "outer_commit" / "incremental_alpha_insert" / "incremental_commit" / etc.
    """
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().isoformat() + "Z"
        lines = [
            "=" * 80,
            f"[{ts}] phase={phase} task={task_id} pid={os.getpid()}",
            f"  exception: {type(exc).__name__}: {exc}",
        ]
        if alpha_id:
            lines.append(f"  alpha_id={alpha_id}")
        if expression:
            lines.append(f"  expression={expression[:300]!r}")
        if quality_status:
            lines.append(f"  quality_status={quality_status}")
        if extra:
            for k, v in extra.items():
                lines.append(f"  {k}={v}")
        lines.append("  traceback:")
        lines.append(_tb.format_exc())
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        # Never let logging failure mask the original error. Caller already
        # logged via loguru — file log is best-effort.
        pass
