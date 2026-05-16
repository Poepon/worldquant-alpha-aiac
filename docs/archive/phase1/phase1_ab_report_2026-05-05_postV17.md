# Phase 1 A/B Report

Generated: 2026-05-04T17:45:13.433524Z

## Variant comparison

| Metric | Legacy (v=0) | Phase 1 (v=1) | Δ |
|---|---|---|---|
| Tasks launched | 4 | 4 |  |
| PASS alphas | 22 | 10 |  |
| FAIL alphas | 82 | 130 |  |
| PASS rate | 21.15% | 7.14% |  |
| OS overfit (sharpe≥5, test=0) | 0 / 22 | 0 / 10 |  |
| Cross-dataset alphas | 0 / 22 | 1 / 10 |  |
| Cross-dataset rate | 0.00% | 10.00% |  |
| Distinct anchor datasets | 4 | 3 |  |
| Train sharpe avg (PASS) | 0.97 | 1.09 |  |
| Test sharpe avg (PASS) | 1.00 | 0.89 |  |
| OS retention (test/train) | 1.03 | 0.82 |  |

## Interpretation guide

- **Cross-dataset rate**: Phase 1 should produce noticeably more cross-dataset alphas (LLM picks fundamental+pv combinations).
- **Distinct anchor datasets**: V-13 RANDOM secondary sort already spreads anchor selection; Phase 1 should preserve or improve.
- **OS retention**: V-12 + V-12.1 should keep test/train ratio ≥ 0.4 in both variants. If Phase 1 ratio drops, cross-dataset introduces overfit risk that needs deeper investigation.
- **PASS rate**: marginal change expected on small N — focus on cross-dataset rate for Phase 1 verdict.