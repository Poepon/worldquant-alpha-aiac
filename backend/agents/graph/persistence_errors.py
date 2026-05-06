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

# T04 (2026-05-06): in-process logrotate. Avoids unbounded growth without
# requiring an external logrotate cron / Windows Task Scheduler dependency.
# When the log exceeds _ROTATE_THRESHOLD_BYTES we move it to .1 (and .1 → .2
# etc up to _ROTATE_KEEP files). Cheap: only checks size on the same write
# that crosses the threshold.
_ROTATE_THRESHOLD_BYTES = 5 * 1024 * 1024  # 5 MB per file
_ROTATE_KEEP = 5  # keep .log + .1 .. .5 = ~30MB max


def _rotate_if_needed(log_path: Path) -> None:
    """Best-effort rotation. Silent on any error so caller's persistence
    error log path is never broken by rotation issues."""
    try:
        if not log_path.exists():
            return
        if log_path.stat().st_size < _ROTATE_THRESHOLD_BYTES:
            return
        # Rotate .{N-1} → .{N}, ... .1 → .2, current .log → .1
        for i in range(_ROTATE_KEEP - 1, 0, -1):
            old = log_path.with_suffix(log_path.suffix + f".{i}")
            new = log_path.with_suffix(log_path.suffix + f".{i + 1}")
            if old.exists():
                if new.exists():
                    new.unlink()
                old.rename(new)
        first = log_path.with_suffix(log_path.suffix + ".1")
        if first.exists():
            first.unlink()
        log_path.rename(first)
    except Exception:
        # Never let rotation failure mask the actual write
        pass


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
        # T04: rotate if file too big
        _rotate_if_needed(_LOG_PATH)
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
