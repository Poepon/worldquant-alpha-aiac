# Phase 3 trigger monitor — 2026-05-13_0204 UTC

Window: last 14 days.

**Overall verdict: ⏳ NOT YET**

## Trigger 1 — Phase 2 A/B PASS rate uplift

| cohort | alpha_n | PASS | PASS rate |
|---|---|---|---|
| LEVEL=2 (Phase 2) | 94 | 6 | 6.4% |
| LEVEL=0 (legacy) | 574 | 113 | 19.7% |

**Uplift: -13.3 pp** (threshold ≥ 5.0 pp)
Cohort sample sufficient: ✅ (both ≥ 20)

Trigger 1: ❌

## Trigger 2 — Hypothesis abandon rate sanity

| status | count |
|---|---|
| PROPOSED | 28 |
| ACTIVE | 275 |
| PROMOTED | 75 |
| ABANDONED | 0 |
| SUPERSEDED | 290 |

**Abandon rate: 0.0%** (target range [30%, 50%])

Trigger 2: ❌

## Trigger 3 — Cross-dataset alpha ratio

Hypothesis-linked alphas in window: 94
Multi-dataset (hypothesis.dataset_pool ≥ 2): 48
**Cross-dataset ratio: 51.1%** (target ≥ 30%)

Trigger 3: ✅

## Trigger 4 — IQC marginal-positive rate (observational, non-gating)

Auto-audited alphas (V-22.12): 0
+Δscore: 0
Positive rate: 0.0%

Sufficient signal (n ≥ 10): —

## Recommendation

**2 of 3 gating triggers NOT met. Phase 3 deferred.**

- Trigger 1: uplift -13.3 pp < 5.0 pp (or cohort_n insufficient: A=94, B=574)
- Trigger 2: abandon rate 0.0% outside [30%, 50%] (or n=668 < 20)

Re-run this monitor weekly. When all 3 gating triggers PASS for two 
consecutive weeks, escalate to Phase 3 implementation review.