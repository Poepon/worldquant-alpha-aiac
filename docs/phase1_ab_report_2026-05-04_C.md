# Phase 1 A/B Report

Generated: 2026-05-04T04:51:45.586282Z

## Variant comparison

| Metric | Legacy (v=0) | Phase 1 (v=1) | Δ |
|---|---|---|---|
| Tasks launched | 4 | 4 |  |
| PASS alphas | 12 | 6 |  |
| FAIL alphas | 85 | 44 |  |
| PASS rate | 12.37% | 12.00% |  |
| OS overfit (sharpe≥5, test=0) | 0 / 12 | 0 / 6 |  |
| Cross-dataset alphas | 0 / 0 | 0 / 0 |  |
| Cross-dataset rate | — | — |  |
| Distinct anchor datasets | 4 | 1 |  |
| Train sharpe avg (PASS) | 1.09 | 1.45 |  |
| Test sharpe avg (PASS) | 1.19 | 0.98 |  |
| OS retention (test/train) | 1.09 | 0.68 |  |

## Interpretation guide

- **Cross-dataset rate**: Phase 1 should produce noticeably more cross-dataset alphas (LLM picks fundamental+pv combinations).
- **Distinct anchor datasets**: V-13 RANDOM secondary sort already spreads anchor selection; Phase 1 should preserve or improve.
- **OS retention**: V-12 + V-12.1 should keep test/train ratio ≥ 0.4 in both variants. If Phase 1 ratio drops, cross-dataset introduces overfit risk that needs deeper investigation.
- **PASS rate**: marginal change expected on small N — focus on cross-dataset rate for Phase 1 verdict.