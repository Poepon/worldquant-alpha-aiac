# Phase 1 A/B Report

Generated: 2026-05-04T12:01:10.968390Z

## Variant comparison

| Metric | Legacy (v=0) | Phase 1 (v=1) | Δ |
|---|---|---|---|
| Tasks launched | 4 | 4 |  |
| PASS alphas | 17 | 9 |  |
| FAIL alphas | 81 | 40 |  |
| PASS rate | 17.35% | 18.37% |  |
| OS overfit (sharpe≥5, test=0) | 0 / 17 | 0 / 9 |  |
| Cross-dataset alphas | 0 / 0 | 0 / 0 |  |
| Cross-dataset rate | — | — |  |
| Distinct anchor datasets | 3 | 2 |  |
| Train sharpe avg (PASS) | 1.12 | 1.22 |  |
| Test sharpe avg (PASS) | 1.00 | 1.02 |  |
| OS retention (test/train) | 0.89 | 0.84 |  |

## Interpretation guide

- **Cross-dataset rate**: Phase 1 should produce noticeably more cross-dataset alphas (LLM picks fundamental+pv combinations).
- **Distinct anchor datasets**: V-13 RANDOM secondary sort already spreads anchor selection; Phase 1 should preserve or improve.
- **OS retention**: V-12 + V-12.1 should keep test/train ratio ≥ 0.4 in both variants. If Phase 1 ratio drops, cross-dataset introduces overfit risk that needs deeper investigation.
- **PASS rate**: marginal change expected on small N — focus on cross-dataset rate for Phase 1 verdict.