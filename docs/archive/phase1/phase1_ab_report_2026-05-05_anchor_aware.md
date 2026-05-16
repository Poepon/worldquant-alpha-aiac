# Phase 1 A/B Report

Generated: 2026-05-05T01:13:10.100890Z

## Variant comparison

| Metric | Legacy (v=0) | Phase 1 (v=1) | Δ |
|---|---|---|---|
| Tasks launched | 4 | 4 |  |
| PASS alphas | 3 | 9 |  |
| FAIL alphas | 67 | 78 |  |
| PASS rate | 4.29% | 10.34% |  |
| OS overfit (sharpe≥5, test=0) | 0 / 3 | 0 / 9 |  |
| Cross-dataset alphas | 2 / 3 | 9 / 9 |  |
| Cross-dataset rate | 66.67% | 100.00% |  |
| Distinct anchor datasets | 3 | 2 |  |
| Train sharpe avg (PASS) | 1.09 | 1.42 |  |
| Test sharpe avg (PASS) | 0.77 | 1.50 |  |
| OS retention (test/train) | 0.71 | 1.06 |  |

## Interpretation guide

- **Cross-dataset rate**: Phase 1 should produce noticeably more cross-dataset alphas (LLM picks fundamental+pv combinations).
- **Distinct anchor datasets**: V-13 RANDOM secondary sort already spreads anchor selection; Phase 1 should preserve or improve.
- **OS retention**: V-12 + V-12.1 should keep test/train ratio ≥ 0.4 in both variants. If Phase 1 ratio drops, cross-dataset introduces overfit risk that needs deeper investigation.
- **PASS rate**: marginal change expected on small N — focus on cross-dataset rate for Phase 1 verdict.