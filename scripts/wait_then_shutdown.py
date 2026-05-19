"""Wait for batch 308-311 to complete, then shut down the computer.

Polls mining_tasks table every 60s. When all 4 tasks reach a terminal
state (anything not RUNNING/PENDING), waits 60s grace for post-task
hooks (refresh_can_submit, KB writes), runs run.bat --stop to gracefully
stop services, then issues `shutdown /s /t 60` for a 60s warning before
hard power-off.

Hard cap: 2 hours. If tasks hang past that, force-FAIL them and shut down
anyway — better than leaving the machine on indefinitely.

Cancel: while the 60s shutdown countdown is ticking, run `shutdown /a` from
any cmd window to abort.

Set up by Claude on user request 2026-05-08.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TASK_IDS = list(range(333, 347))  # 333 to 346 inclusive (14 tasks)
POLL_INTERVAL_SEC = 60
HARD_TIMEOUT_HOURS = 4.0  # 14 tasks × ~30 min serial / 3 workers ≈ 2.3h, give some buffer
GRACE_SEC_BEFORE_SHUTDOWN = 60

DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"


async def count_active() -> int:
    e = create_async_engine(DB_URL)
    try:
        async with e.begin() as c:
            r = await c.execute(text("""
                SELECT COUNT(*) FROM mining_tasks
                WHERE id = ANY(:ids) AND status IN ('RUNNING', 'PENDING')
            """), {"ids": TASK_IDS})
            return r.scalar() or 0
    finally:
        await e.dispose()


async def force_fail_remaining() -> int:
    e = create_async_engine(DB_URL)
    try:
        async with e.begin() as c:
            r = await c.execute(text("""
                UPDATE mining_tasks SET status='FAILED', updated_at=NOW()
                WHERE id = ANY(:ids) AND status IN ('RUNNING', 'PENDING')
                RETURNING id
            """), {"ids": TASK_IDS})
            ids = [row.id for row in r.fetchall()]
            return len(ids)
    finally:
        await e.dispose()


async def main():
    start = datetime.now()
    deadline = start + timedelta(hours=HARD_TIMEOUT_HOURS)
    print(f"[wait_then_shutdown] start={start.isoformat()} deadline={deadline.isoformat()}", flush=True)
    print(f"[wait_then_shutdown] watching tasks {TASK_IDS}, poll={POLL_INTERVAL_SEC}s", flush=True)

    timed_out = False
    while True:
        if datetime.now() >= deadline:
            print(f"[wait_then_shutdown] HARD TIMEOUT at {datetime.now().isoformat()}", flush=True)
            timed_out = True
            break
        n = await count_active()
        elapsed_min = (datetime.now() - start).total_seconds() / 60
        print(f"[wait_then_shutdown] elapsed={elapsed_min:.0f}min  active={n}/{len(TASK_IDS)}", flush=True)
        if n == 0:
            print(f"[wait_then_shutdown] all tasks done at {datetime.now().isoformat()}", flush=True)
            break
        await asyncio.sleep(POLL_INTERVAL_SEC)

    if timed_out:
        n_failed = await force_fail_remaining()
        print(f"[wait_then_shutdown] force-FAILED {n_failed} stuck task(s)", flush=True)

    print(f"[wait_then_shutdown] grace {GRACE_SEC_BEFORE_SHUTDOWN}s for post-task hooks ...", flush=True)
    await asyncio.sleep(GRACE_SEC_BEFORE_SHUTDOWN)

    # Stop services gracefully
    print(f"[wait_then_shutdown] running run.bat --stop ...", flush=True)
    try:
        subprocess.run(
            ["run.bat", "--stop"],
            cwd=str(Path(__file__).resolve().parent.parent),
            shell=True, timeout=120,
        )
    except Exception as e:
        print(f"[wait_then_shutdown] run.bat --stop failed (non-fatal): {e}", flush=True)

    # Shutdown — /s shut down, /t 60 sec warning, /c message, /f force close apps
    print(f"[wait_then_shutdown] issuing shutdown /s /t 60 (cancel: shutdown /a)", flush=True)
    try:
        subprocess.run(
            ["shutdown", "/s", "/t", "60", "/f", "/c", "AIAC mining batch complete"],
            shell=True, timeout=30,
        )
    except Exception as e:
        print(f"[wait_then_shutdown] shutdown command failed: {e}", flush=True)
        return 1

    print(f"[wait_then_shutdown] shutdown queued — system will power off in 60s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
