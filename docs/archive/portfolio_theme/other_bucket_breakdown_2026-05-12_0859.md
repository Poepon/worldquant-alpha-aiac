# 'other' bucket breakdown — 2026-05-12_0859 UTC

Layer 1 classifier (`scripts/portfolio_theme_audit.py`) left **267**
of 702 all_pass alphas in `other`. This report decomposes them
by top-level op + field-family signatures to surface hidden sub-themes.

## Top-level operator distribution (others)

| top_op | count | % of others | mean sharpe |
|---|---|---|---|
| `multiply` | 130 | 48.7% | 1.20 |
| `ts_arg_max` | 26 | 9.7% | 1.07 |
| `group_neutralize` | 26 | 9.7% | 1.32 |
| `group_zscore` | 19 | 7.1% | 1.37 |
| `subtract` | 13 | 4.9% | 1.24 |
| `group_rank` | 12 | 4.5% | 0.84 |
| `ts_decay_linear` | 9 | 3.4% | 1.35 |
| `ts_arg_min` | 7 | 2.6% | 0.81 |
| `ts_delta` | 4 | 1.5% | 4.15 |
| `ts_mean` | 3 | 1.1% | 1.01 |
| `rank` | 3 | 1.1% | 1.57 |
| `zscore` | 3 | 1.1% | 1.56 |
| `quantile` | 3 | 1.1% | 1.58 |
| `normalize` | 3 | 1.1% | 1.54 |
| `ts_zscore` | 3 | 1.1% | 3.84 |
| `group_scale` | 1 | 0.4% | 1.56 |
| `ts_regression` | 1 | 0.4% | 0.98 |
| `ts_scale` | 1 | 0.4% | 0.69 |

## Field-family signature distribution (others)

Family codes: PV (price-volume), FND (fundamental), ANL (analyst),
SNT (sentiment/news), MDL (factor/model composite), OPT (option/IV),
RISK (correlation/volatility), GRP (group built-in).

| family | count | mean sharpe |
|---|---|---|
| `PV` | 164 | 1.31 |
| `ANL` | 3 | 5.57 |
| `RISK` | 2 | 0.89 |
| `FND` | 1 | 0.66 |
| `MDL` | 1 | 0.75 |

## Sample expressions (per top_op, first 5 with highest sharpe)

### `multiply` (130 alphas)

- pk=6405  sh=1.96  to=0.83  `multiply(-1, ts_rank(ts_delta(close, 1), 20))`
- pk=6409  sh=1.96  to=0.83  `multiply(-1, ts_rank(ts_delta(close, 1) / close, 60))`
- pk=7177  sh=1.95  to=0.51  `multiply(-1, ts_decay_linear(ts_rank(returns, 240), 4))`
- pk=7554  sh=1.94  to=0.51  `multiply(-1, ts_decay_linear(ts_rank(returns, 120), 4))`
- pk=6410  sh=1.92  to=0.83  `multiply(-1, ts_zscore(ts_delta(close, 1), 20))`

### `ts_arg_max` (26 alphas)

- pk=5466  sh=1.58  to=0.55  `ts_arg_max(cap, 5)`
- pk=5230  sh=1.45  to=0.58  `ts_arg_max(forward_price_30, 5)`
- pk=5224  sh=1.28  to=0.53  `ts_arg_max(call_breakeven_90, 5)`
- pk=5371  sh=1.28  to=0.03  `ts_arg_max(current_ratio, 240)`
- pk=7576  sh=1.25  to=0.16  `ts_arg_max(news_eps_actual, 60)`

### `group_neutralize` (26 alphas)

- pk=7560  sh=1.73  to=0.55  `group_neutralize(multiply(-1, ts_zscore(cap, 5)), industry)`
- pk=7561  sh=1.59  to=0.54  `group_neutralize(multiply(-1, ts_zscore(cap, 5)), sector)`
- pk=4827  sh=1.59  to=0.56  `group_neutralize(multiply(-1, ts_zscore(high, 5)), subindustry)`
- pk=4841  sh=1.56  to=0.55  `group_neutralize(multiply(-1, ts_rank(low, 5)), subindustry)`
- pk=5210  sh=1.56  to=0.55  `group_neutralize(multiply(-1, ts_arg_min(cap, 5)), subindustry)`

### `group_zscore` (19 alphas)

- pk=7565  sh=1.80  to=0.56  `group_zscore(multiply(-1, ts_zscore(cap, 5)), industry)`
- pk=4832  sh=1.64  to=0.56  `group_zscore(multiply(-1, ts_zscore(high, 5)), subindustry)`
- pk=5211  sh=1.63  to=0.58  `group_zscore(multiply(-1, ts_arg_min(cap, 5)), subindustry)`
- pk=5213  sh=1.62  to=0.60  `group_zscore(multiply(-1, ts_rank(low, 5)), subindustry)`
- pk=4875  sh=1.59  to=0.58  `group_zscore(ts_arg_max(close, 5), subindustry)`

### `subtract` (13 alphas)

- pk=7568  sh=1.80  to=0.56  `subtract(multiply(-1, ts_zscore(cap, 5)), group_mean(multiply(-1, ts_zscore(cap, 5)), cap, subindustry))`
- pk=7567  sh=1.80  to=0.56  `subtract(multiply(-1, ts_zscore(cap, 5)), group_mean(multiply(-1, ts_zscore(cap, 5)), cap, industry))`
- pk=4835  sh=1.55  to=0.55  `subtract(multiply(-1, ts_arg_min(cap, 5)), group_mean(multiply(-1, ts_arg_min(cap, 5)), cap, market))`
- pk=4824  sh=1.38  to=0.11  `subtract(ts_zscore(growth_potential_rank_derivative, 20), group_mean(ts_zscore(growth_potential_rank_derivative, 20), cap, subindustry))`
- pk=4708  sh=1.08  to=0.10  `subtract(ts_arg_max(earnings_certainty_rank_derivative, 20), group_mean(ts_arg_max(earnings_certainty_rank_derivative, 20), cap, subindustry))`

### `group_rank` (12 alphas)

- pk=4519  sh=0.88  to=0.03  `group_rank(ts_arg_max(growth_potential_rank_derivative, 60), industry)`
- pk=4505  sh=0.87  to=0.03  `group_rank(multiply(-1, ts_rank(earnings_certainty_rank_derivative, 20)), industry)`
- pk=4506  sh=0.87  to=0.03  `group_rank(multiply(-1, ts_rank(earnings_certainty_rank_derivative, 20)), subindustry)`
- pk=4512  sh=0.87  to=0.03  `group_rank(ts_arg_max(earnings_certainty_rank_derivative, 20), industry)`
- pk=4513  sh=0.87  to=0.03  `group_rank(ts_arg_max(earnings_certainty_rank_derivative, 20), subindustry)`

### `ts_decay_linear` (9 alphas)

- pk=1352  sh=1.93  to=0.29  `ts_decay_linear(-ts_rank(returns, 5), 10)`
- pk=1372  sh=1.90  to=0.44  `ts_decay_linear(-ts_rank(returns, 5), 10)`
- pk=1344  sh=1.82  to=0.32  `ts_decay_linear(-ts_zscore(returns, 5), 10)`
- pk=7569  sh=1.43  to=0.41  `ts_decay_linear(multiply(-1, ts_zscore(cap, 5)), 5)`
- pk=4833  sh=1.22  to=0.42  `ts_decay_linear(multiply(-1, ts_zscore(high, 5)), 5)`

### `ts_arg_min` (7 alphas)

- pk=6419  sh=1.31  to=0.16  `ts_arg_min(news_eps_actual, 60)`
- pk=5489  sh=0.86  to=0.05  `ts_arg_min(ebitda, 120)`
- pk=5422  sh=0.79  to=0.18  `ts_arg_min(actual_eps_value_quarterly, 20)`
- pk=5366  sh=0.77  to=0.03  `ts_arg_min(ebitda, 240)`
- pk=5243  sh=0.73  to=0.03  `ts_arg_min(ebitda / assets, 240)`

### `ts_delta` (4 alphas)

- pk=7797  sh=14.14  to=0.67  `ts_delta(news_eps_actual, 20)`
- pk=5336  sh=0.97  to=0.41  `ts_delta(industry_relative_return_5d, 60)`
- pk=6571  sh=0.77  to=0.40  `ts_delta(unsystematic_risk_last_60_days, 20)`
- pk=5369  sh=0.73  to=0.06  `ts_delta(depre_amort, 120)`

### `ts_mean` (3 alphas)

- pk=7570  sh=1.30  to=0.35  `ts_mean(multiply(-1, ts_zscore(cap, 5)), 5)`
- pk=4523  sh=0.93  to=0.04  `ts_mean(ts_arg_max(growth_potential_rank_derivative, 60), 10)`
- pk=5425  sh=0.79  to=0.02  `ts_mean(actual_eps_value_quarterly, 60)`

## Top field tokens used (others)

| field | usage count | family |
|---|---|---|
| `cap` | 51 | PV |
| `high` | 30 | PV |
| `low` | 28 | PV |
| `earnings_certainty_rank_derivative` | 24 | OTHER |
| `returns` | 23 | PV |
| `close` | 21 | PV |
| `growth_potential_rank_derivative` | 18 | OTHER |
| `open` | 12 | PV |
| `analyst_revision_rank_derivative` | 7 | OTHER |
| `industry_rel_ttm_sales_to_ev` | 5 | OTHER |
| `ebitda` | 5 | OTHER |
| `forward_price_30` | 4 | OTHER |
| `actual_eps_value_quarterly` | 4 | OTHER |
| `forward_price_20` | 3 | OTHER |
| `news_eps_actual` | 3 | ANL |
| `relative_valuation_rank_derivative` | 2 | OTHER |
| `vwap` | 2 | PV |
| `scl12_buzz` | 2 | OTHER |
| `bookvalue_ps` | 2 | OTHER |
| `call_breakeven_90` | 2 | OTHER |
| `call_breakeven_60` | 2 | OTHER |
| `employee` | 2 | OTHER |
| `inverse_peg_earnings_growth` | 2 | OTHER |
| `call_breakeven_20` | 2 | OTHER |
| `current_ratio` | 2 | OTHER |
| `pv13_custretsig_retsig` | 1 | OTHER |
| `correlation_last_90_days_spy` | 1 | RISK |
| `scl12_sentvec` | 1 | OTHER |
| `forward_price_180` | 1 | OTHER |
| `beta_last_90_days_spy` | 1 | RISK |

## Suggested classifier additions

Looking at top_op + field family signatures, propose new themes for:
- `multiply` with families PV(89) + OTHER(40)  → consider new theme
- `ts_arg_max` with families OTHER(17) + PV(6)  → consider new theme
- `group_neutralize` with families PV(14) + OTHER(12)  → consider new theme
- `group_zscore` with families PV(13) + OTHER(6)  → consider new theme
- `subtract` with families PV(13) + OTHER(10)  → consider new theme
- `group_rank` with families PV(7) + OTHER(5)  → consider new theme
- `ts_decay_linear` with families PV(6) + OTHER(3)  → consider new theme
- `ts_arg_min` with families OTHER(4) + FND(1)  → consider new theme
