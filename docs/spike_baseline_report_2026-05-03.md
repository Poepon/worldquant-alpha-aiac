# Spike 2.0 Baseline Report — 2026-05-03

> Validation of V-12 (IS/OS overfit gate) + V-13 (dataset 平权) on a
> minimal 6-task batch, plus discovery of two follow-up bugs (V-12.1
> sign-flip gap and V-15 semantic-error short-circuit).

## Sample

- **Time window**: 2026-05-03 07:53 → 10:25 (~2.5 hours wall clock)
- **Tasks**: 6 (3 T1 / 2 T2 / 1 T3)
- **Total alphas**: 790 (20 PASS / 770 FAIL)
- **Worker fleet**: 3 workers, 3 BRAIN sim slots saturated

## Per-task summary

| Task | Mode | Status | Rounds | PASS | FAIL |
|---|---|---|---|---|---|
| 42 | T1 | COMPLETED | 8 | 4 | 64 |
| 43 | T1 | COMPLETED | 4 | 10 | 26 |
| 44 | T1 | COMPLETED | 5 | 2 | 44 |
| 45 | T2 | COMPLETED | 6 | 2 | 333 |
| 46 | T2 | COMPLETED | 4 | 2 | 233 |
| 47 | T3 | COMPLETED | 4 | 0 | 70 |

## V-12 (IS/OS overfit gate) — main path verified ✓

| Tier | Status | n | train sharpe | test sharpe | retain ratio |
|---|---|---|---|---|---|
| T1 | PASS | 8 | 2.01 | 1.05 | **0.52** ✓ |
| T1 | PROVISIONAL | 8 | 0.95 | 1.31 | inverse-pass |
| T2 | PASS | 1 | 1.43 | 1.00 | **0.70** ✓ |
| T2 | PROVISIONAL | 3 | 1.51 | 0.90 | **0.60** ✓ |

vs Spike 1.0 (V-12 前):
- T2 PASS train_avg = 5.52 / test_avg = 0.40 (90% decay, severe overfit)
- T2 max sharpe = 16.20 (now 1.43 — IS extremes rejected)

**Conclusion**: hard_gate_pass with `is_overfit_safe` rejects all sharpe>5 IS-only alphas in spike 2.0 main path.

### V-12.1 sign-flip gap (discovered, fixed)

One alpha (YP2QnnVW, train=8.37 / test=0) leaked through the **sign-flip
retry path** (mining_agent.py PR5 logic), which had its own mini-gate
sans `is_overfit_safe`. Fixed at commit `ef6ec79`. Backfill demoted the
1 leaked PASS to OPTIMIZE.

After fix: zero IS-overfit PASSes expected on next worker restart.

## V-13 (dataset 平权) — verified ✓

| Anchor dataset | Spike 1.0 (旧 ORDER BY 隐式)| Spike 2.0 (V-13 random 二级排序)|
|---|---|---|
| model16 | 88 (75%) | **4** |
| model51 | 23 | 0 |
| fundamental6 | 0 | **10** (50%) |
| fundamental2 | 0 | 2 |
| news12 | 0 | 2 |
| pv96 | 0 | 2 |
| **Distinct datasets** | 2 | **5** |

`func.random()` secondary sort restored dataset diversity. Spike 1.0's
T1 daily_goal=4 broke out of dataset loop on first iteration → only
model16 ever explored. Spike 2.0 reaches 5 different anchor datasets.

## Cross-dataset alphas (D1 universal-PV merge effect)

```
Anchor dataset  fundamental6  with fields (returns, revenue):
Anchor dataset  fundamental6  with fields (returns, cashflow_fin):
Anchor dataset  fundamental6  with fields (returns, debt_st):
```

3 spike 2.0 alphas use `returns` (pv1) + fundamental6 fields → real
cross-dataset alpha output. Full Phase 1 (LLM-chosen `selected_datasets`)
is deferred (HYPOTHESIS_CENTRIC_LEVEL=0); cross-dataset rate computed at
field-set level still shows 3/14 = 21% (vs Spike 1.0 11% on PASS subset).

## V-15 (semantic-error short-circuit) — discovered, fixed

**Symptom**: T2 tasks 45/46 accumulated 566 SIMULATION_ERROR (out of 770
total FAIL) — `nws12_*` VECTOR fields wrapped in `ts_zscore` /
`ts_decay_linear` were sent to BRAIN which rejected with
"Operator does not support event inputs", wasting ~50% of T2 sim budget.

**Root cause**: `validation.py:130-132` appended `sem_result.errors`
to the `warnings` list but didn't set `is_valid=False` — the
`alpha_semantic_validator.strict_type_check=True` setting was effectively
dead code. SELF_CORRECT never triggered, so LLM never got a chance to
rewrite VECTOR-misuse expressions.

**Fix**: validation.py:130-145 — `is_valid = False` + populated `error`
on semantic errors, so failures fall through to SELF_CORRECT path.

Expected effect after worker restart: 30-40% reduction in T2
SIMULATION_ERROR (the VECTOR-misuse subset; other BRAIN rejections
remain).

## Decision implications for Plan v5+

### Gate evaluation (V-12 / V-13 corrected)

| Gate | Spike 2.0 reading | Decision |
|---|---|---|
| Gate 1 (PASS rate) | 2.53% (raw) / ~2% OS-validated | **completes Plan v5** ✓ |
| Gate 2 (cross-dataset) | 21% on 14 PASS | Phase 1 必做 ✓ |
| Gate 3 (can_submit) | TBD (BRAIN async) | Phase 2 选做 |
| Gate 4 (per-tier weak) | T1 healthy / T2 PASS rate低 / T3 0 PASS | T3 wrap & trade_when 推迟 |

**Conclusion unchanged from Spike 1.0**: Plan v5 full launch justified.
But the actual ratio of legitimate PASS is much higher in spike 2.0
(post-V-12) — meaning Phase 1 starts with cleaner baseline.

## Pre-Phase-1 checklist (updated)

- [x] V-7 Quasi-T1 whitelist
- [x] V-3 Pilot baseline (Spike 1.0)
- [x] V-1 Gate 1+2 joint decision tree
- [x] V-4 BRAIN check FAIL demote (sync + backfill)
- [x] V-12 IS/OS overfit gate (main path)
- [x] V-12.1 sign-flip retry path
- [x] V-13 dataset RANDOM secondary sort
- [x] V-15 semantic-error short-circuit
- [x] R7-0 operator audit
- [x] R7-1 datafield audit
- [x] R7-2 field_adapter (alias → BRAIN real names)
- [x] R1 Golden Set v0.1 (30/40 draft)
- [x] Spike 1.0 + Spike 2.0 V-12/V-13 verified
- [ ] Worker restart to load V-12.1 + V-15 fixes
- [ ] Phase 1 A2-A6 (8-10 day) — main HGE implementation

## Open backlog

- R1 v0.1 → v1.0 expansion (10 entries to add, alternative_data paradigm)
- V-14 (LLM hallucination prevention via prompt cheat sheet) — deferred
  per plan §V-10 design choice
