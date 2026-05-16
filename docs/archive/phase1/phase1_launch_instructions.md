# Phase 1 Launch Instructions

> Plan v5+ §Phase 1 cross-dataset hypothesis go-live. After implementing
> A1-A5 (commits 12209c0, 47dc208), this runbook walks through worker
> restart, A/B verification, and the criteria for promoting Phase 1 to
> default.

## Pre-flight checklist

| Check | How to verify |
|---|---|
| Spike 2.0 收齐 | `docs/spike_baseline_report_2026-05-03.md` ✓ |
| V-12 + V-12.1 + V-15 修复 commit | `git log --oneline -5` shows ef6ec79, 5b962cc |
| Phase 1 A1-A5 commit | `git log --oneline -2` shows 47dc208 |
| Tests pass | `pytest backend/tests/test_phase1_cross_dataset.py` (16 cases) |
| Backend on :8001 | `netstat -ano \| findstr :8001` |
| 3 Celery workers running new code | restart procedure below |

## Step 1 — Restart 3 Celery workers

Workers must reload Python modules to pick up:
- `evaluation.py` (V-12.1 sign-flip gate)
- `validation.py` (V-15 semantic-error short-circuit)
- `mining_tasks.py` (Phase 1 dataset_pool flow)
- `mining_agent.py`, `workflow.py`, `generation.py` (Phase 1 plumbing)
- `prompts.yaml`, `hypothesis.py` (cross-dataset prompt)

In each of the 3 worker PowerShell windows:

```
Ctrl+C        # graceful stop (waits for in-flight simulate)
↑ Enter       # re-run the same celery command
```

Confirm with:

```powershell
Get-Process python, celery | Sort-Object StartTime -Descending |
  Select-Object Id, ProcessName, StartTime, @{N='MB';E={[math]::Round($_.WorkingSet64/1MB,1)}} |
  Select-Object -First 6 | Format-Table -AutoSize
```

3 celery + 3 python child processes should show recent StartTime.

## Step 2 — Verify Phase 1 path is live (no traffic yet)

This is a code-flag verification, no BRAIN simulate is consumed:

```powershell
docker exec aiac-db psql -U postgres -d alpha_gpt -c "SELECT 1"
.\venv\Scripts\python -c "from backend.config import settings; print('LEVEL:', settings.HYPOTHESIS_CENTRIC_LEVEL); print('K:', settings.PHASE1_COMPLEMENTARY_DATASET_K)"
```

Expected:
- `LEVEL: 0` (default — Phase 1 NOT enabled globally)
- `K: 3`

This is correct — we don't flip the global flag. The A/B launcher writes
`task.config.hypothesis_centric_variant=1` per task, and `mining_tasks`
prefers that over the global setting.

## Step 3 — Launch the A/B (8 tasks, 50/50 split)

```powershell
.\venv\Scripts\python scripts\phase1_ab_launch.py --n 8 --dry-run     # preview
.\venv\Scripts\python scripts\phase1_ab_launch.py --n 8                # execute
```

The launcher creates 8 T1 tasks (4 variant=0 legacy / 4 variant=1 Phase 1)
interleaved so worker affinity doesn't bias variants.

Output ends with two ID lists:

```
Variant 0 (legacy):  IDs ...
Variant 1 (Phase 1): IDs ...
```

Save these — they feed the comparison script.

## Step 4 — Wait for completion

8 tasks × 3 worker pool ≈ 1.5–2 hours wall clock (T1 daily_goal=4). Watch:

```powershell
docker exec redis redis-cli GET "brain:concurrent_sims"   # should hold near 3
docker exec aiac-db psql -U postgres -d alpha_gpt -c "
SELECT id, (config->>'hypothesis_centric_variant')::int AS variant, status,
  (SELECT COUNT(*) FROM alphas WHERE task_id = mining_tasks.id) AS pass
FROM mining_tasks WHERE config->>'phase1_ab' = 'true'
ORDER BY id"
```

## Step 5 — Compare variants

```powershell
.\venv\Scripts\python scripts\phase1_ab_compare.py `
  --legacy-ids 50,52,54,56 `
  --phase1-ids 51,53,55,57
```

Writes `docs/phase1_ab_report_<date>.md` with side-by-side metrics.

### Pass criteria for promoting Phase 1 to default

| Metric | Acceptable | Excellent |
|---|---|---|
| **Cross-dataset rate (Phase 1 / Legacy)** | ≥ 1.5× | ≥ 3× |
| **OS retention (test/train ≥ 0.4)** | both variants | Phase 1 ≥ legacy |
| **PASS rate** | within 30% of legacy | Phase 1 > legacy |
| **Distinct anchor datasets** | both ≥ 3 | Phase 1 ≥ 5 |
| **Suspected overfit count** | both = 0 | both = 0 |

If criteria met, set in `.env`:
```
HYPOTHESIS_CENTRIC_LEVEL=1
HYPOTHESIS_CENTRIC_CANDIDATE=1
```
Restart workers. Future tasks default to Phase 1 unless explicitly set
otherwise via `task.config.hypothesis_centric_variant`.

## Rollback

Per-task rollback (kill specific tasks):
```powershell
docker exec aiac-db psql -U postgres -d alpha_gpt -c "
UPDATE mining_tasks SET status='COMPLETED' WHERE id IN (...)"
```

Global rollback (force all new tasks to legacy until investigated):
```
HYPOTHESIS_CENTRIC_LEVEL=0
HYPOTHESIS_CENTRIC_CANDIDATE=0
```

In .env, then restart workers. No DB migration needed; existing alphas
keep their `dataset_id` (anchor-only column) regardless of variant.

## Known limitations of Phase 1 A1-A5

- **R7-1 missing aliases**: 9 plan aliases (`amount`, `book_value_per_share`,
  `cfo`, `ev`, `net_income`, `open_interest`, `total_assets`, `total_debt`,
  `total_equity`) need field_adapter on USA only. CHN/EUR/ASI/GLB blocked.
- **No hypothesis lifecycle yet**: Phase 2 work — `hypothesis_id` FK, typed
  Hypothesis class, KB hypothesis-keyed learning all pending (9-12 day).
- **LLM may still hallucinate operators**: V-10 design choice — validator +
  SELF_CORRECT recover; no preventative cheat-sheet injection in prompts.
