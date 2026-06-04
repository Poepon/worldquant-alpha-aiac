# Datafields Snapshot v1 (R7-1)

Generated: 2026-05-03T06:15:00.940480Z

## Region × universe summary

| Region | N datasets | N fields | MATRIX | VECTOR |
|---|---|---|---|---|
| USA | 17 | 5937 | 5013 | 772 |

## Datasets by region/universe

| Region | Universe | Dataset | Category | Subcategory | Fields | MATRIX | VECTOR |
|---|---|---|---|---|---|---|---|
| USA | TOP3000 | `analyst4` | analyst | analyst-analyst-estimates | 653 | 469 | 184 |
| USA | TOP3000 | `fundamental2` | fundamental | fundamental-footnotes | 318 | 318 | 0 |
| USA | TOP3000 | `fundamental6` | fundamental | fundamental-fundamental-data | 886 | 574 | 312 |
| USA | TOP3000 | `model16` | model | model-valuation-models | 24 | 24 | 0 |
| USA | TOP3000 | `model51` | model | model-risk-models | 16 | 16 | 0 |
| USA | TOP3000 | `model77` | model | model-technical-models | 3241 | 3241 | 0 |
| USA | TOP3000 | `news12` | news | news-news | 322 | 75 | 247 |
| USA | TOP3000 | `news18` | news | news-news-sentiment | 75 | 61 | 14 |
| USA | TOP3000 | `option8` | option | option-option-volatility | 64 | 64 | 0 |
| USA | TOP3000 | `option9` | option | option-option-analytics | 74 | 74 | 0 |
| USA | TOP3000 | `pv1` | pv | pv-price-volume | 24 | 13 | 0 |
| USA | TOP3000 | `pv13` | pv | pv-relationship | 165 | 30 | 0 |
| USA | TOP3000 | `pv96` | pv | pv-price-volume | 32 | 23 | 9 |
| USA | TOP3000 | `sentiment1` | sentiment | sentiment-sentiment | 17 | 17 | 0 |
| USA | TOP3000 | `socialmedia12` | socialmedia | socialmedia-social-media | 18 | 12 | 6 |
| USA | TOP3000 | `socialmedia8` | socialmedia | socialmedia-social-media | 2 | 2 | 0 |
| USA | TOP3000 | `univ1` | pv | pv-price-volume | 6 | 0 | 0 |

## Plan v5+ §R7-1 USA cross-check (Quasi-T1 fields + universal PV)

- Plan-mentioned aliases: 20
- Present in USA/TOP3000: 11
- Missing (need synthesis or different real name): 9

### ❌ Missing aliases (plan uses these names but USA/TOP3000 doesn't have them)

- `amount`
- `book_value_per_share`
- `cfo`
- `ev`
- `net_income`
- `open_interest`
- `total_assets`
- `total_debt`
- `total_equity`

These need either (a) BRAIN real-name mapping in field_adapter (e.g. `eps` → `fnd6_newa2v1300_eps_per_share`) or (b) synthesis via available fields (e.g. `eps` → `divide(fnd6_..._ni, shares)`).