# Phase 3 trigger monitor — 2026-05-13_0531 UTC

Window: last 14 days.

**Overall verdict: ⏳ NOT YET**

## Trigger 1 — Phase 2 A/B PASS rate uplift

| cohort | alpha_n | PASS | PASS rate |
|---|---|---|---|
| LEVEL=2 (Phase 2) | 99 | 7 | 7.1% |
| LEVEL=0 (legacy) | 183 | 3 | 1.6% |

**Uplift: +5.4 pp** (threshold ≥ 5.0 pp)
Cohort sample sufficient: ✅ (both ≥ 20)

Trigger 1: ✅

## Trigger 2 — Hypothesis abandon rate sanity

| status | count |
|---|---|
| PROPOSED | 30 |
| ACTIVE | 278 |
| PROMOTED | 78 |
| ABANDONED | 0 |
| SUPERSEDED | 0 |

**Retirement rate (ABANDONED + SUPERSEDED): 0.0%** (target range [30%, 50%])

  - ABANDONED only: 0 (0.0%) — strict B6 path
  - SUPERSEDED only: 0 — replaced by node_hypothesis upstream (functionally retired)
  - Retired total: 0 / 386

Trigger 2: ❌

## Trigger 3 — Cross-dataset alpha ratio

Hypothesis-linked alphas in window: 99
Multi-dataset (hypothesis.dataset_pool ≥ 2): 51
**Cross-dataset ratio: 51.5%** (target ≥ 30%)

Trigger 3: ✅

## Trigger 4 — IQC marginal-positive rate (observational, non-gating)

Auto-audited alphas (V-22.12): 42
+Δscore: 0
Positive rate: 0.0%
mean Δscore: -1220.5  median: -1087.0

Sufficient signal (n ≥ 10): —

⚠ All audited alphas have Δscore ≤ 0 — the can_submit gate approves alphas that hurt the IQC portfolio. **Gate calibration is the real bottleneck**, not Phase 3.

## Recommendation

**1 of 3 gating triggers NOT met. Phase 3 deferred.**

- Trigger 2: retirement rate 0.0% (ABANDONED=0 + SUPERSEDED=0) outside [30%, 50%] (or n=386 < 20)

Re-run this monitor weekly. When all 3 gating triggers PASS for two 
consecutive weeks, escalate to Phase 3 implementation review.