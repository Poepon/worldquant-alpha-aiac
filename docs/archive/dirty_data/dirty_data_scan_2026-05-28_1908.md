# Dirty Data Scan Report — 2026-05-28_1908

Read-only enumeration. No DELETE/UPDATE performed.

## A. wrong dataset_id / failed-empty alpha

| Probe | Count |
|---|---:|
| `A1.alpha_null_dataset_id_with_expr` | 4,998 |
| `A1b.alpha_null_dataset_id_with_task` | 0 |
| `A1c.alpha_null_dataset_id_no_task_brain_imported` | 4,998 |
| `A2.alpha_pre_brain_skip_recorded` | 0 |
| `A3.alpha_status_FAIL` | 0 |
| `A4.alpha_empty_metrics` | 0 |
| `A5.alpha_anchor_vs_fields_mismatch` | 0 |
| `A6.alpha_failure_no_error_type` | 0 |

### Samples — `A1.alpha_null_dataset_id_with_expr`

```
{"id": 15719, "alpha_id": "kqQVv5pl", "task_id": null, "expr_head": "multiply(-1, ts_zscore(ts_std_dev(vec_avg(nws18_bee), 5), 20))", "status": "UNSUBMITTED", "created_at": "2026-05-28 10:50:07.286711"}
{"id": 15718, "alpha_id": "78JZ3Ok5", "task_id": null, "expr_head": "ts_zscore(add(rank(ts_zscore(historical_volatility_180, 60)), rank(ts_zscore(div", "status": "UNSUBMITTED", "created_at": "2026-05-28 10:50:07.286711"}
{"id": 15717, "alpha_id": "A1k0eq9w", "task_id": null, "expr_head": "multiply(-1, rank(ts_zscore(divide(subtract(ts_mean(vec_avg(nws18_ber), 20), vec", "status": "UNSUBMITTED", "created_at": "2026-05-28 10:50:07.286711"}
{"id": 15716, "alpha_id": "O0orRxGb", "task_id": null, "expr_head": "ts_zscore(ts_decay_linear(add(vec_avg(nws18_acb), vec_avg(nws18_bam)), 20), 40)", "status": "UNSUBMITTED", "created_at": "2026-05-28 10:50:07.286711"}
{"id": 15715, "alpha_id": "zqOYg81X", "task_id": null, "expr_head": "multiply(-1, rank(ts_zscore(vec_avg(nws18_bee), 3)))", "status": "UNSUBMITTED", "created_at": "2026-05-28 10:50:07.286711"}
```

### Samples — `A1c.alpha_null_dataset_id_no_task_brain_imported`

```
{"id": 15719, "alpha_id": "kqQVv5pl", "status": "UNSUBMITTED", "date_created": "2026-05-28 13:14:19", "created_at": "2026-05-28 10:50:07.286711"}
{"id": 15718, "alpha_id": "78JZ3Ok5", "status": "UNSUBMITTED", "date_created": "2026-05-28 13:14:20", "created_at": "2026-05-28 10:50:07.286711"}
{"id": 15717, "alpha_id": "A1k0eq9w", "status": "UNSUBMITTED", "date_created": "2026-05-28 13:14:33", "created_at": "2026-05-28 10:50:07.286711"}
{"id": 15716, "alpha_id": "O0orRxGb", "status": "UNSUBMITTED", "date_created": "2026-05-28 13:16:58", "created_at": "2026-05-28 10:50:07.286711"}
{"id": 15715, "alpha_id": "zqOYg81X", "status": "UNSUBMITTED", "date_created": "2026-05-28 13:17:41", "created_at": "2026-05-28 10:50:07.286711"}
```

## B. stale IQC audit + JSONB null

| Probe | Count |
|---|---:|
| `B1.iqc_audit_old_scope_S1` | 0 |
| `B2.iqc_audit_missing_recommendation` | 1 |
| `B3.iqc_audit_stale_false_but_old_scope` | 0 |
| `B4.iqc_audit_too_old_stale_false` | 0 |
| `B-jsonb.alphas.metrics_scalar_null` | 0 |
| `B-jsonb.alphas.fields_used_scalar_null` | 0 |
| `B-jsonb.alphas.operators_used_scalar_null` | 0 |
| `B-jsonb.alphas.checks_scalar_null` | 0 |
| `B-jsonb.alphas.is_metrics_scalar_null` | 0 |
| `B-jsonb.alphas.os_metrics_scalar_null` | 0 |
| `B-jsonb.alphas.settings_scalar_null` | 0 |
| `B-jsonb.alphas.decay_curve_scalar_null` | 0 |
| `B-jsonb.hypotheses.dataset_pool_scalar_null` | 0 |
| `B-jsonb.hypotheses.trigger_detail_scalar_null` | 0 |
| `B-jsonb.hypotheses.baseline_metrics_scalar_null` | 0 |
| `B-jsonb.hypotheses.thesis_score_history_scalar_null` | 0 |
| `B-jsonb.hypotheses.key_fields_scalar_null` | 0 |
| `B-jsonb.hypotheses.suggested_operators_scalar_null` | 0 |
| `B-jsonb.mining_tasks.config_scalar_null` | 0 |
| `B-jsonb.mining_tasks.target_datasets_scalar_null` | 0 |
| `B-jsonb.mining_tasks.generation_strategy_scalar_null` | 0 |
| `B-jsonb.experiment_runs.config_snapshot_scalar_null` | 0 |
| `B-jsonb.experiment_runs.strategy_snapshot_scalar_null` | 0 |
| `B-jsonb.experiment_runs.runtime_state_scalar_null` | 0 |
| `B-jsonb.trace_steps.input_data_scalar_null` | 0 |
| `B-jsonb.trace_steps.output_data_scalar_null` | 0 |

### Samples — `B2.iqc_audit_missing_recommendation`

```
{"id": 8003, "alpha_id": "zq5XGamo", "scope": null, "at": "2026-05-13T19:11:15.604705+00:00"}
```

## C. orphan task/run + zombie

| Probe | Count |
|---|---:|
| `C1.mining_task_running_stale_30m` | 0 |
| `C2.mining_task_running_no_persist_2h` | 0 |
| `C3.experiment_run_running_stale_1h` | 0 |
| `C4.mining_job_running_stale_1h` | 0 |
| `C5.trace_step_running_stale_1h` | 0 |
| `C6.trace_step_orphan_task` | 0 |
| `C7.alpha_failure_orphan_task` | 0 |

## D. RAG / hypothesis forest

| Probe | Count |
|---|---:|
| `D1.hypothesis_chronic_failure` | 285 |
| `D2.hypothesis_zombie_parent` | 0 |
| `D3.hypothesis_active_but_abandoned` | 0 |
| `D4.alpha_failure_unanalyzed_old` | 16 |
| `D5.alpha_pnl_null_value` | 1,858 |
| `D6.alpha_pnl_orphan_alpha` | 0 |
| `D7.knowledge_entries_duplicate_pattern_hash` | 0 |
| `D8.knowledge_entries_inactive` | 5,810 |

### Samples — `D1.hypothesis_chronic_failure`

```
{"id": 3048, "statement": "Stocks experiencing a sharp decline in unsystematic (idiosyncratic) risk while simultaneously seeing positive analyst sentiment revisions will outperform over the next 5-20 days.", "alpha_count": 45, "pass_count": 0, "sharpe_max": null, "created_at": "2026-05-17 09:21:02.877883+00:00"}
{"id": 3046, "statement": "Stocks experiencing a sharp decline in unsystematic (idiosyncratic) risk while sentiment is improving will outperform over the next 5-10 days.", "alpha_count": 45, "pass_count": 0, "sharpe_max": null, "created_at": "2026-05-17 09:09:02.103124+00:00"}
{"id": 3047, "statement": "Stocks whose unsystematic (idiosyncratic) risk has declined recently relative to its own history outperform high-idiosyncratic-risk peers over the next 5-20 days.", "alpha_count": 45, "pass_count": 0, "sharpe_max": null, "created_at": "2026-05-17 09:15:01.991875+00:00"}
{"id": 3049, "statement": "Stocks experiencing a sharp decline in implied volatility relative to their recent history will outperform over the next 5-10 days as risk premia compress.", "alpha_count": 41, "pass_count": 0, "sharpe_max": null, "created_at": "2026-05-17 09:26:46.944846+00:00"}
{"id": 3042, "statement": "The change in the ratio (close - vwap) / (high - low) over a 5-day window predicts short-term cross-sectional reversal returns.", "alpha_count": 28, "pass_count": 0, "sharpe_max": null, "created_at": "2026-05-17 06:48:23.249772+00:00"}
```

### Samples — `D4.alpha_failure_unanalyzed_old`

```
{"id": 37818, "task_id": 3095, "error_type": "OTHER", "created_at": "2026-05-19 15:25:19.888342+00:00"}
{"id": 37817, "task_id": 3095, "error_type": "QUALITY_CHECK_FAILED", "created_at": "2026-05-19 15:25:19.888342+00:00"}
{"id": 37816, "task_id": 3095, "error_type": "OTHER", "created_at": "2026-05-19 15:25:19.888342+00:00"}
{"id": 37815, "task_id": 3095, "error_type": "OTHER", "created_at": "2026-05-19 15:25:19.888342+00:00"}
{"id": 37814, "task_id": 3095, "error_type": "QUALITY_CHECK_FAILED", "created_at": "2026-05-19 15:25:19.888342+00:00"}
```

### Samples — `D5.alpha_pnl_null_value`

```
{"alpha_id": 15711, "trade_date": "2019-01-02 00:00:00", "pnl": null, "cumulative_pnl": 0.0}
{"alpha_id": 15712, "trade_date": "2019-01-02 00:00:00", "pnl": null, "cumulative_pnl": 0.0}
{"alpha_id": 15715, "trade_date": "2019-01-02 00:00:00", "pnl": null, "cumulative_pnl": 0.0}
{"alpha_id": 15713, "trade_date": "2019-01-02 00:00:00", "pnl": null, "cumulative_pnl": 0.0}
{"alpha_id": 15714, "trade_date": "2019-01-02 00:00:00", "pnl": null, "cumulative_pnl": 0.0}
```

### Samples — `D8.knowledge_entries_inactive`

```
{"id": 20433, "entry_type": "FAILURE_PITFALL", "pat_head": "R1B_FAILURE_TREE: The interaction of intraday volatility (hi", "usage_count": 0, "created_at": "2026-05-27 20:04:46.546633+00:00"}
{"id": 20424, "entry_type": "FAILURE_PITFALL", "pat_head": "R1B_FAILURE_TREE: Companies with high asset-to-cashflow rati", "usage_count": 0, "created_at": "2026-05-27 18:51:33.535875+00:00"}
{"id": 20423, "entry_type": "FAILURE_PITFALL", "pat_head": "R1B_FAILURE_TREE: Rapid expansion in the volatility-of-volat", "usage_count": 0, "created_at": "2026-05-27 18:47:28.319850+00:00"}
{"id": 20389, "entry_type": "FAILURE_PITFALL", "pat_head": "R1B_FAILURE_TREE: High estimate dispersion (high minus low) ", "usage_count": 0, "created_at": "2026-05-27 04:28:29.303959+00:00"}
{"id": 20293, "entry_type": "FAILURE_PITFALL", "pat_head": "R1B_FAILURE_TREE: Risk-adjusted momentum (returns regressed ", "usage_count": 0, "created_at": "2026-05-26 17:47:13.399843+00:00"}
```

## Grand total flagged rows: 17,966