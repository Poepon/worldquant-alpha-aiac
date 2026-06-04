# Phase 1 A/B Report

Generated: 2026-05-05T04:19:53.762907Z

## Variant comparison

| Metric | Legacy (v=0) | Phase 1 (v=1) | Δ |
|---|---|---|---|
| Tasks launched | 4 | 4 |  |
| PASS alphas | 15 | 6 |  |
| FAIL alphas | 88 | 38 |  |
| PASS rate | 14.56% | 13.64% |  |
| OS overfit (sharpe≥5, test=0) | 0 / 15 | 0 / 6 |  |
| Cross-dataset alphas | 5 / 15 | 2 / 6 |  |
| Cross-dataset rate | 33.33% | 33.33% |  |
| Distinct anchor datasets | 2 | 2 |  |
| Train sharpe avg (PASS) | 1.06 | 0.73 |  |
| Test sharpe avg (PASS) | 1.13 | 0.80 |  |
| OS retention (test/train) | 1.07 | 1.10 |  |

## Interpretation guide

- **Cross-dataset rate**: Phase 1 should produce noticeably more cross-dataset alphas (LLM picks fundamental+pv combinations).
- **Distinct anchor datasets**: V-13 RANDOM secondary sort already spreads anchor selection; Phase 1 should preserve or improve.
- **OS retention**: V-12 + V-12.1 should keep test/train ratio ≥ 0.4 in both variants. If Phase 1 ratio drops, cross-dataset introduces overfit risk that needs deeper investigation.
- **PASS rate**: marginal change expected on small N — focus on cross-dataset rate for Phase 1 verdict.