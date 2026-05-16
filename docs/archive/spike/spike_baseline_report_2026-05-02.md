# Spike Baseline Report — 2026-05-02

> Pilot baseline for Plan v5+ Spike decision gate (V-3 mitigation: data-driven threshold calibration before launching Spike run).

## Sample

- **Time window**: 2026-04-29 08:03 → 2026-05-01 12:57 (~3 days)
- **Distinct tasks**: 7
- **Total alphas**: 4500 (~640 alpha/task, ~1500 alpha/day)
- **State**: Quasi-T1 NOT YET deployed in production (this snapshot is pre-Quasi-T1)

## Aggregate metrics

| Metric | Value |
|---|---|
| Total alphas | 4500 |
| PASS | 52 (**1.16%**) |
| PASS_PROVISIONAL | 72 |
| PASS+PROV combined | 124 (**2.76%**) |
| PASS ∧ can_submit=true | 17 (**32.69% of PASS**) |

## Per-tier breakdown

| Tier | Total | PASS | Rate | Avg sharpe (PASS) |
|---|---|---|---|---|
| T1 | 218 | 8 | **3.67%** | 1.615 |
| T2 | 156 | 26 | **16.67%** | 1.854 |
| T3 | 65 | 1 | **1.54%** | 2.120 |
| tier=None | 4061 | 17 | **0.42%** | 1.935 |

**Critical observation**: 90% (4061/4500) of all alphas classify as `tier=None`. Of the 52 PASS alphas, **17 (32.7%) are tier=None** — this is exactly the failure mode Quasi-T1 v1.0 is designed to fix. After Quasi-T1 deployment, a portion of these tier=None PASS alphas should reclassify as T1 (double-field arithmetic patterns now admitted).

## Cross-dataset

| Metric | Value |
|---|---|
| Alphas with non-empty fields_used | 3881 |
| Cross-dataset (≥2 datasets in fields_used) | 706 (**18.19%**) |
| Cross-dataset PASS | 6 (11.54% of PASS) |

D1 universal-PV-merge fix is producing meaningful cross-dataset coverage (18%), but PASS rate within cross-dataset is roughly comparable to overall (cross PASS 6/706=0.85% vs overall 1.16%) — D1 alone hasn't unlocked the high-Sharpe cross-domain alpha class yet.

## Gate evaluation (Plan v5+ thresholds, V-3 calibration)

### Gate 1 — PASS rate (absolute scale)

| PASS count | PASS rate | Decision |
|---|---|---|
| ≥ 50 (with PROV ≥ 100) | ≥ 8% | ROI 极差 → 极简版 |
| 25-49 | 4-8% | Phase 1 only |
| 12-24 | 2-4% | 缩减版 |
| **< 12 PASS or < 2%** | **< 2%** | **完整 Plan v5 启动有据** ✅ |

**Observed**: 52 PASS @ 1.16% rate — note the count exceeds the "<12 PASS" threshold but the rate is below the 2% floor. Rate-based reading dominates: **完整 Plan v5 启动有据**.

### Gate 2 — Cross-dataset rate

| Range | Decision |
|---|---|
| < 10% | Phase 1 必做 (D1 修复未到位) |
| **10-30%** | **Phase 1 选做 — Gate 1 决定** ✅ |
| ≥ 30% | Phase 1 跳过 (D1 已充分实现) |

**Observed**: 18.19% — middle band. Combined with Gate 1's "完整启动" verdict + V-1 mitigation (Gate 1 alone insufficient when cross_dataset_rate < 30%), **Phase 1 做**.

### Gate 3 — can_submit rate (KB closed-loop validation)

| Range | Decision |
|---|---|
| < 30% | Phase 2 KB 闭环必做 |
| **30-60%** | **Phase 2 选做 — Gate 1 决定** ✅ |
| ≥ 60% | Phase 2 KB 闭环 backlog |

**Observed**: 32.69% — middle band. Gate 1 says "完整启动" → **Phase 2 做**.

### Gate 4 — Per-tier breakdown (V-3 corrected thresholds: high-tier should be harder)

| Tier | V-3 threshold | Observed | Verdict |
|---|---|---|---|
| T1 | > 10% (else weak) | 3.67% | **Weak T1 — invest in Quasi-T1 + Layer 1 Core Golden Set** ✅ |
| T2 | > 5% (else weak) | 16.67% | T2 healthy — 60/40 dual-track is optional |
| T3 | > 2% (else weak) | 1.54% | **Weak T3 — invest in trade_when theme conditions** ✅ |

(Plan v1 had T1<5%/T2<10%/T3<15% which was inverted; V-3 corrects the logic so high-tier has higher bar.)

## Joint decision (V-1 mitigation: Gate 1+2 联合, not Gate 1 alone)

| Gate | Reading | Implication |
|---|---|---|
| 1 (PASS rate) | 1.16% | 完整 Plan v5 |
| 2 (cross-dataset) | 18.19% | Phase 1 做 |
| 3 (can_submit) | 32.69% | Phase 2 做 |
| 4 (T1 weak) | 3.67% | Quasi-T1 + Core Golden Set |
| 4 (T3 weak) | 1.54% | trade_when theme library (Q3 backlog item) |

**→ 完整 Plan v5 Final 启动 (24-31 day) is justified.**

## Spike run plan (V-2 mitigation: N_task 10-15 → 20-25)

Now that Quasi-T1 v1.0 is deployed (commit pending) + baseline thresholds fall in expected ranges:

1. Launch **20-25 mining tasks** (mixed T1/T2/T3, daily_goal=4-6, max_iter=2)
2. Expected alpha output: 1200-2000 candidates (effective N: 200-400 after ICC adjustment)
3. Compare post-Quasi-T1 PASS rate / tier distribution against this baseline
4. If post-Quasi-T1 PASS rate ≥ 2x this baseline (≥ 2.3%), Quasi-T1 alone has high marginal value — re-evaluate plan v5 sub-step priorities (V-1 says don't conflate Quasi-T1 wins with cross-dataset wins, but a 2x lift is signal)
5. Specifically watch tier=None → T1 reclassification ratio (current tier=None=4061/4500=90%; expect drop after Quasi-T1)

## Pre-Spike checklist

- [x] V-3 Pilot baseline data (this report)
- [x] V-1 decision tree corrected (Gate 1+2 joint)
- [x] V-7 Quasi-T1 implemented with mini-AST matching (177 tests pass)
- [x] V-4 monthly audit script stub (`scripts/quasi_t1_candidates_audit.py`)
- [ ] V-2 Spike run launch (20-25 tasks) — **NEXT STEP, requires user**
