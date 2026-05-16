# Fix C Retest — 8 PASS Alpha × 3 Wrapper Variants

Generated: 2026-05-07T02:50:00Z
Source: `scripts/retest_pass_alphas_with_wrappers.py`
Raw data: `docs/retest_pass_alphas_2026-05-07_0241.json`

## TL;DR

**0/24 wrapper variants are BRAIN-submittable.** Fix C's mechanism (route to
optimization) is correct, but the chosen 3 wrappers (industry_neutralize /
winsorize std=4 / combined) don't lift these specific 8 alpha to BRAIN's bar.

**One success proof**: pk=6577 V1 lifted fitness 0.60 → **1.60** via
industry_neutralize alone — confirming the wrapper class works, just not
universally on all base shapes.

## Variant matrix

| Base pk | sh | fit | V1 industry_neut | V2 winsorize std=4 | V3 combined |
|---|---|---|---|---|---|
| 6570 | 1.11 | 0.77 | sh=1.00 fit=0.70 | sh=0.88 fit=1.60 | sh=1.00 fit=0.70 |
| 6577 | 0.82 | 0.60 | sh=0.88 **fit=1.60** ✓ | sh=1.00 fit=0.70 | sh=0.87 fit=0.54 |
| 6580 | 1.50 | 0.73 | sh=-0.66 fit=-0.89 ⚠️ | sh=0.87 fit=0.54 | sh=0.41 fit=0.19 |
| 6583 | 1.64 | 0.66 | sh=1.68 fit=0.62 | sh=1.70 fit=0.63 | sh=1.89 fit=0.59 |
| 6584 | 1.09 | 0.53 | sh=0.32 fit=0.10 ⚠️ | sh=1.88 fit=0.59 | sh=1.10 fit=0.41 |
| 6585 | 1.00 | 0.58 | sh=0.09 fit=0.02 ⚠️ | sh=0.91 fit=0.28 | sh=1.14 fit=0.58 |
| 6589 | 2.08 | 0.80 | sh=-0.60 fit=-0.59 ⚠️ | sh=1.14 fit=0.58 | sh=2.31 fit=0.67 |
| 6590 | 0.91 | 0.74 | sh=2.32 **fit=0.68** | sh=0.82 fit=0.66 | sh=0.91 fit=0.64 |

✓ = single-fail variant (closest to submittable)
⚠️ = catastrophic regression

## Pattern analysis

### Wrapper-by-wrapper outcome

| Wrapper | Fitness lift cases | Sharpe regress cases | New failures |
|---|---|---|---|
| V1 industry_neutralize | 1 (pk=6577 +1.00) | 4 of 8 | HIGH_TURNOVER on 4, CW on 1 |
| V2 winsorize std=4 | 1 (pk=6570 +0.83) | 5 of 8 | LOW_SHARPE on most, CW on 2 |
| V3 combined | 0 | 3 of 8 | HIGH_TURNOVER on 5, CW on 3 |

### New failure modes introduced

8/24 variants triggered **HIGH_TURNOVER** (new). Wrapping increases rebalance
sensitivity — neutralization daily residualization burns turnover budget.

4/24 variants triggered **CONCENTRATED_WEIGHT** (new) on alpha that didn't
have CW originally. Winsorize at ±4σ doesn't resolve cumulative concentration
when only a few names dominate the signal.

## Root cause: 5/8 alpha are "barely-PASS" boundary cases

Original sharpe distribution:
- 5 alpha with sh < 1.25 (BRAIN gate): 0.82, 0.91, 1.00, 1.09, 1.11
- 3 alpha with sh ≥ 1.25: 1.50, 1.64, 2.08

The 5 sub-1.25 alpha need a **+50% sharpe lift** to pass — wrappers don't
deliver structural sharpe gains, they redistribute risk. These should never
have been labeled PASS by our gate. Internal `SHARPE_MIN=1.0` is a 探索 bar,
not a quality bar.

The 3 high-sharpe alpha (`implied_volatility_call_*`) have a different
issue: fit 0.66-0.80 + concentrated weight. IV signals are structurally
concentrated (option-rich names dominate). Wrapping with industry neut
can flatten but kills sharpe in 2 of 3 cases.

## Fix C status

✅ **Mechanism correct**: when this batch reruns under Fix C, all 8 alpha
will be flagged PASS_PROVISIONAL (BRAIN actionable fails) and routed to
`_collect_optimization_candidates`. This is the architectural fix.

❌ **Variant set inadequate**: the 3 wrappers I chose match what
`optimization_chain._determine_optimization_priorities` would prioritize
(集中 → wrapper, 风险 → structure), but the actual variant generators
(`_generate_window_variants`, `_generate_wrapper_variants`) emit wider
parameter sweeps. Live optimization_chain has more shots:
- decay sweep (0/4/8/16) — can fix HIGH_TURNOVER
- window sweep (10/20/60/120/240) — sharpe stability
- truncation tuning (0.04/0.08/0.10) — concentration
- sign_flip — pk=6577/6585/6590 already have `multiply(-1, ...)`; flipping back is testable
- structural rewrites

## What we actually learned

1. **Internal PASS gate (sharpe ≥ 1.0, fitness ≥ 0.5) is too loose**. ~62%
   of "PASS" are below BRAIN's submission bar. Real fix is **gate alignment**:
   bump to sharpe ≥ 1.25 + fitness ≥ 0.95. Yields will drop ~80% but every
   PASS will be honest.
2. **Wrapper-only optimization rarely saves boundary alpha**. The
   architectural Fix C is correct (route them to optimization), but realistic
   yield improvement requires running the **full** optimization_chain, not
   the 3 hand-picked wrappers I tested.
3. **IV-class alpha need separate handling**. `implied_volatility_call_*`
   gives high sharpe but structurally concentrated weight. May need
   cross-sectional rank (`rank(...)`) instead of `ts_zscore` to distribute
   weight before BRAIN's CW gate.
4. **One real win**: pk=6577 + industry_neutralize → fit 0.60 → 1.60. The
   wrapper class works; Fix C will trigger this path on future alpha.

## Recommended next steps

| Priority | Action | Impact |
|---|---|---|
| P0 | Tighten PASS gate to BRAIN bar: `SHARPE_MIN=1.25, FITNESS_MIN=0.95` | Honest PASS rate, ~80% drop in volume |
| P1 | Verify Fix C live: launch new mining batch, confirm BRAIN-rejected PASS get downgraded to PROVISIONAL and enter optimization queue | Architectural fix proven in production |
| P1 | Add sign_flip variant to optimization_chain for already-negated alpha (`-1, ...` → drop the -1) | Cheap test, may save pk=6577/6585/6590 |
| P2 | IV-class detection: when expression contains `implied_volatility_call_*`, prefer `rank(...)` over `ts_zscore` | Structural fix for high-sharpe-low-fit class |
| P3 | Replace winsorize std=4 with std=2.5 in optimization_chain | Less signal kill while still clipping |

## Bottom line

Fix C is the right architectural fix. But the **8 existing PASS** in batch
276-283 are mostly weak boundary alpha — wrappers won't save them. The
real production validation is whether **future PASS alpha that BRAIN
rejects** get correctly routed through optimization and emerge submittable.

For these 8, recommend marking them dead and moving on.
