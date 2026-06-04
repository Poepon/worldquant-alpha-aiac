# Sprint 0 Baseline Spike Report

**Run at (UTC)**: 2026-05-19T15:44:33+00:00

## 1. R12 author baseline PASS rate (last 30d)

```
finalized_n       = 8,658
pass_n            = 131
author_pass_rate  = 0.0151  (1.51%)
```

## 2. R14 PASS_RATE_FLOOR calibration (last 30d round-level)

```
round_n         = 28
p5  (R14 floor) = 0.0000  (0.00%)
p10             = 0.0000  (0.00%)
p50 (median)    = 0.0000  (0.00%)
```

## 3. Sentinel stamp presence (last 7d, PR0.6 verification)

```
r10_family_cap_dropped         = 0
g3_ast_originality_blocked     = 0
g5_crossover_parent_ids        = 0
r1b_mutation_triggered         = 0
hypothesis_forest_reference    = 0
simulation_cache_hit           = 0
total_alpha                    = 1,368
```

## Action items

- **R12 GO gate (plan v5 §6.1)**: `assistant_pass_rate >= author_pass_rate * 0.90` 
  for 30d obs (bootstrap effect-size CI 不跨 0).
- **R14 PASS_RATE_FLOOR (plan v5 §6.2 / config TASK_STOP_LOSS_PASS_RATE_FLOOR)**: set to 
  `p5` value above (currently default 0.05 → may need adjustment).
- **PR0.6 verification**: ALL 6 sentinel stamp counts above MUST be > 0 within 7d 
  of Sprint 1 R12 ship; if `r1b_mutation_triggered` / `hypothesis_forest_reference` / 
  `simulation_cache_hit` stay at 0, the corresponding source-of-truth path is broken.
