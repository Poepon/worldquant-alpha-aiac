# Spike Launch Instructions — Plan v5+ V-2

## Pre-flight checklist

| Check | How to verify | Status |
|---|---|---|
| Postgres `aiac-db` running | `docker ps \| grep aiac-db` | ✅ Up (healthy) |
| Redis running | `docker ps \| grep redis` | ✅ Up (healthy) |
| Quasi-T1 deployed | `pytest backend/tests/test_factor_tier_classifier.py` | ✅ 143 PASS |
| Pilot baseline collected | `docs/spike_baseline_report_2026-05-02.md` | ✅ Done |
| Backend on :8001 | `netstat -ano \| findstr :8001` | ⏳ NOT RUNNING |
| Celery worker | `Get-Process python` | ⏳ NOT RUNNING |
| BRAIN credentials | `.env` `BRAIN_EMAIL`/`BRAIN_PASSWORD` | ✅ Set |

## Step 1 — Start backend + Celery worker

In two separate terminals (or use `run.bat --start` to launch both windowed):

**Terminal A — Backend**
```powershell
cd E:\WorldQuant\worldquant-alpha-aiac
.\venv\Scripts\activate
uvicorn backend.main:app --reload --port 8001
```

**Terminal B — Celery worker** (Windows requires `--pool=solo`)
```powershell
cd E:\WorldQuant\worldquant-alpha-aiac
.\venv\Scripts\activate
celery -A backend.celery_app worker --loglevel=info --pool=solo
```

Or one-shot:
```powershell
cd E:\WorldQuant\worldquant-alpha-aiac
.\run.bat --start
```

Wait until you see `Uvicorn running on http://0.0.0.0:8001` and `[celery] ready`.

## Step 2 — Dry-run the Spike plan

```powershell
.\venv\Scripts\python scripts\spike_launch.py --n 20 --dry-run
```

Default mix (50% T1, 30% T2, 20% T3) → 10 T1 + 6 T2 + 4 T3 = 20 tasks. Adjust with `--tier-mix 60,30,10` etc.

## Step 3 — Launch the Spike

```powershell
.\venv\Scripts\python scripts\spike_launch.py --n 20
```

Saves the created task IDs in stdout — copy them into `docs/spike_run_2026-05-02.md` for traceability.

Expected behavior:
- Tasks queue in `mining_tasks` table with `status='RUNNING'` after start
- Celery worker picks up `run_mining_task(task_id)` for each
- Per-task `daily_goal=4` means worker tries 4 PASS alphas per iteration; `max_iterations=10` (config default) means up to 40 candidates per task
- BRAIN simulator is rate-limited at `MAX_SIMULATIONS_PER_DAY=100` — the 20-task batch will spread across 2-3 days
- Each task individually completes when `progress_current >= daily_goal` for that day

## Step 4 — Monitor progress

One-shot snapshot:
```powershell
.\venv\Scripts\python scripts\spike_monitor.py --since-date 2026-05-02
```

Live watch (every 5 minutes):
```powershell
.\venv\Scripts\python scripts\spike_monitor.py --since-date 2026-05-02 --watch --interval 300
```

The monitor compares post-Quasi-T1 metrics against the pre-Quasi-T1 baseline. Watch for:

| Signal | Interpretation |
|---|---|
| `tier=None pct` ↓ | Quasi-T1 admitting more two-field arithmetic as T1 (good) |
| `T1 count` ↑ | Quasi-T1 working at the classifier level |
| `T1 PASS rate` ↑↓ | Whether Quasi-T1 admits actually pay off (the test) |
| `cross-dataset rate` | D1 alone vs Phase 1 need — check at end |
| `total alpha` reaching 1200-2000 | Spike target met → run Gate evaluation |

## Step 5 — Spike completion criteria

Stop tracking and run final evaluation when:
- Total alphas in window ≥ 1200, OR
- 7 days elapsed (whichever comes first)

Final evaluation (run after Spike completes):
```powershell
.\venv\Scripts\python scripts\spike_monitor.py --since-date 2026-05-02
```

Manually update `docs/spike_baseline_report_2026-05-02.md` with the post-Quasi-T1 numbers and the Gate decision tree output.

## Notes

- Quasi-T1 only reclassifies *new* alphas. The 4061 existing `tier=None` alphas in DB stay tier=None unless a backfill is run. To check Quasi-T1 retroactive coverage, run `--since-date` filtering on the monitor.
- If Celery worker dies during the run, restart it — tasks resume from their last `progress_current`. Use `docker logs aiac-db` and Celery stdout for diagnostics.
- BRAIN 401 re-auth is patched (commit 8a3497d), so credential-expiry doesn't kill the run.

## Abort plan

To stop the run prematurely:
```powershell
.\run.bat --stop                           # kills backend + Celery
# or via API for individual tasks:
curl -X POST http://localhost:8001/api/v1/tasks/<id>/intervene -d '{"action":"PAUSE"}'
```

Intervened tasks keep their PASS alphas — partial run data still gets you part of the way to the Gate decision.
