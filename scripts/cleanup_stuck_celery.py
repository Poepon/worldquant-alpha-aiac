"""Clean up stuck Celery state after a worker crash / kill.

Symptoms this script fixes:
  - Worker was killed (or `--pool=solo` got SIGKILL'd) while a
    backend.tasks.run_mining_task was in-flight.
  - On restart, the new worker pulls the unacked task and re-runs it,
    blocking newly queued mining tasks behind it.
  - BRAIN's `concurrent_sims` Redis counter may have been left at >0
    because simulate_alpha's finally never ran.
  - mining_tasks rows for those tasks may be stuck at status=RUNNING
    even though no worker is actually processing them anymore.

What this does (default = preview-only; pass --confirm to apply):
  1. List pending one-off tasks in the `celery` broker queue.
  2. List unacked task entries in the `unacked` hash.
  3. List mining_tasks rows currently RUNNING.
  4. Show brain:concurrent_sims counter value.
  5. With --confirm:
     - DEL `celery`, `unacked`, `unacked_index` redis keys
     - mark stuck mining_tasks as STOPPED (with audit transition)
     - mark stuck experiment_runs as STOPPED with error_message
     - reset brain:concurrent_sims to 0

This is destructive — only run when you're certain no celery worker
is actively processing useful work. Recommended sequence:
  1. powershell: Stop-Process -Force <celery worker pids>
  2. python -m scripts.cleanup_stuck_celery --confirm
  3. start a fresh worker

Usage:
  python -m scripts.cleanup_stuck_celery               # preview
  python -m scripts.cleanup_stuck_celery --confirm     # apply
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys

import redis
from sqlalchemy import text


def _broker_redis() -> "redis.Redis":
    from backend.config import settings
    return redis.from_url(settings.REDIS_URL, decode_responses=True)


def _decode_celery_msg(raw: str) -> dict:
    """Best-effort decode of a celery broker message."""
    try:
        envelope = json.loads(raw)
        body_b64 = envelope.get("body")
        if not body_b64:
            return {"raw": envelope}
        body = json.loads(base64.b64decode(body_b64))
        cid = (envelope.get("properties") or {}).get("correlation_id")
        # body format: [args, kwargs, options]
        args = body[0] if isinstance(body, list) and body else None
        return {"correlation_id": cid, "args": args}
    except Exception as e:
        return {"raw_preview": raw[:80], "decode_error": str(e)}


async def survey() -> dict:
    """Read-only inspection of all stuck-state indicators."""
    r = _broker_redis()
    out = {}

    # Pending in celery queue (FIFO list)
    pending_raw = []
    for i in range(r.llen("celery")):
        msg = r.lindex("celery", i)
        if msg:
            pending_raw.append(_decode_celery_msg(msg))
    out["pending"] = pending_raw

    # Unacked tasks (hash keyed by tag)
    unacked = []
    for field, val in (r.hgetall("unacked") or {}).items():
        try:
            d = json.loads(val)
            # unacked entries are [msg, ...] tuples; take first if list
            msg = d[0] if isinstance(d, list) and d else d
            decoded = _decode_celery_msg(msg) if isinstance(msg, dict) and "body" in msg else {"raw": msg}
            unacked.append({"tag": field, **decoded})
        except Exception as e:
            unacked.append({"tag": field, "decode_error": str(e)})
    out["unacked"] = unacked

    # Brain slot counter
    out["brain_concurrent_sims"] = r.get("brain:concurrent_sims")

    # mining_tasks stuck at RUNNING
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        rs = await db.execute(text(
            "SELECT id, task_name, status, current_iteration, max_iterations, "
            "EXTRACT(EPOCH FROM (NOW() - updated_at))::int AS age_sec "
            "FROM mining_tasks WHERE status = 'RUNNING'"
        ))
        out["running_tasks"] = [
            {"id": row[0], "name": row[1], "iter": f"{row[3]}/{row[4]}", "age_sec": row[5]}
            for row in rs.all()
        ]
        rs = await db.execute(text(
            "SELECT id, task_id, status FROM experiment_runs WHERE status = 'RUNNING'"
        ))
        out["running_runs"] = [
            {"id": row[0], "task_id": row[1]} for row in rs.all()
        ]
    return out


async def apply_cleanup(running_task_ids: list[int], running_run_ids: list[int]) -> None:
    """Destructive cleanup. Caller must have already surveyed."""
    r = _broker_redis()

    print("Clearing celery + unacked queues + brain slot counter...")
    r.delete("celery")
    r.delete("unacked")
    r.delete("unacked_index")
    r.set("brain:concurrent_sims", 0)

    # Also clear any rate-limit cooldown / strike keys that were left mid-flight.
    cleared = 0
    for k in list(r.scan_iter("brain:rl_cooldown:*")):
        r.delete(k)
        cleared += 1
    if cleared:
        print(f"  cleared {cleared} rate-limit keys")

    if not running_task_ids:
        return

    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("UPDATE mining_tasks SET status='STOPPED', updated_at=NOW() "
                 "WHERE id = ANY(:ids) AND status='RUNNING'"),
            {"ids": running_task_ids},
        )
        if running_run_ids:
            await db.execute(
                text("UPDATE experiment_runs SET status='STOPPED', "
                     "finished_at=NOW(), error_message='cleanup_stuck_celery' "
                     "WHERE id = ANY(:ids)"),
                {"ids": running_run_ids},
            )
        await db.commit()
    print(f"Marked {len(running_task_ids)} task(s) STOPPED, {len(running_run_ids)} run(s) STOPPED.")


def _print_survey(s: dict) -> None:
    print("=" * 70)
    print("Celery stuck-state survey")
    print("=" * 70)

    print(f"\nPending in `celery` queue: {len(s['pending'])}")
    for i, item in enumerate(s["pending"]):
        cid = (item.get("correlation_id") or "?")[:8]
        args = item.get("args")
        print(f"  [{i}] cid={cid} args={args}")

    print(f"\nUnacked tasks: {len(s['unacked'])}")
    for item in s["unacked"]:
        cid = (item.get("correlation_id") or "?")[:8]
        args = item.get("args")
        print(f"  tag={item.get('tag','?')[:8]} cid={cid} args={args}")

    print(f"\nbrain:concurrent_sims = {s['brain_concurrent_sims']}")

    print(f"\nmining_tasks at status=RUNNING: {len(s['running_tasks'])}")
    for t in s["running_tasks"]:
        age_min = t["age_sec"] / 60 if t["age_sec"] else 0
        print(f"  #{t['id']} {t['name']:<32} iter={t['iter']} age={age_min:.1f}min")

    print(f"\nexperiment_runs at status=RUNNING: {len(s['running_runs'])}")
    for run in s["running_runs"]:
        print(f"  run #{run['id']} task=#{run['task_id']}")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true",
                        help="Actually apply cleanup (default: dry-run survey)")
    args = parser.parse_args()

    survey_result = asyncio.run(survey())
    _print_survey(survey_result)

    if not args.confirm:
        print()
        print("Pass --confirm to clear everything above.")
        return

    if not (survey_result["pending"] or survey_result["unacked"]
            or survey_result["running_tasks"]
            or (survey_result["brain_concurrent_sims"] not in (None, "0"))):
        print()
        print("Nothing to clean up.")
        return

    print()
    print("Applying cleanup...")
    task_ids = [t["id"] for t in survey_result["running_tasks"]]
    run_ids = [r["id"] for r in survey_result["running_runs"]]
    asyncio.run(apply_cleanup(task_ids, run_ids))
    print()
    print("Cleanup complete. Now (re)start celery worker.")


if __name__ == "__main__":
    cli()
