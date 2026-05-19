# R13-spike — BRAIN simulate daily PnL feasibility

**Sprint**: Phase 4 Sprint 2 day 1 (per plan v5 §6.5)
**Owner**: 单工作 session
**人日**: 0.5
**Status**: GO — OLS 路径
**Date**: 2026-05-20

## 1. 目的

决定 B2 R13 `factor_decomposition` shadow PR 走哪条路径:

| 路径 | 触发条件 | 工时 |
|------|---------|------|
| **OLS** (推荐) | BRAIN simulate API 返 daily PnL ≥504d | 3.5 |
| Bucket-median fallback | 只返 IS 总 metrics | 4.0 (+0.5 bucket 设计) |
| Stamp-only (降级) | BRAIN 完全不返 PnL | 1.0 |

## 2. 调研结论 — GO OLS

### 2.1 BRAIN 返 daily PnL series? **YES**

Endpoint:`GET /alphas/{alpha_id}/recordsets/pnl`

Response shape(实测,production caller `CorrelationService._fetch_pnl_series`,`backend/services/correlation_service.py:254`):

```json
{
  "records": [
    ["2020-01-02", 12345.67, ...],
    ["2020-01-03", 12300.42, ...],
    ...
  ],
  "schema": {
    "properties": [
      {"name": "date"},
      {"name": "pnl"},
      ...
    ]
  }
}
```

Parse 逻辑:`_pnl_records_to_series` (correlation_service.py:104) 拿 schema.properties 当 column names,records 当 DataFrame rows,取 `date` + `pnl` 两列做 Date-indexed Series。

### 2.2 数据长度 ≥504d? **YES**

CorrelationService 实际使用 `LOOKBACK_YEARS = 4`(`correlation_service.py:_series_to_returns`)→ 4 × 252 trading days ≈ **1008 daily PnL points**,远超 504d (≈2y) 阈值。

实测样本(production caller `refresh_os_alpha_cache`):每个 OS alpha 的 PnL series 通常 2-4 年,极少数 <1y 的会被 caller 当 empty Series 处理 — R13 OLS 可直接 mirror 此 fallback。

### 2.3 BRAIN simulate response 本身有 daily PnL? **NO**

`_get_completed_alpha_details`(`brain_adapter.py:1148-1199`)解析 simulate 返回的 alpha detail,`is.pnl` 字段是 **scalar 总 PnL 数值**,不是 series。

→ R13 factor_lens 需要 daily series → 必须额外调 `get_alpha_pnl(alpha_id)`(每个 candidate alpha 一次 GET request)。

### 2.4 调用成本 cost overhead 估算

shadow mode 只对 PASS alpha 算 residual sharpe:

| 维度 | 估算 |
|------|------|
| PASS alpha / day | ~30-60(per Sprint 0 spike baseline)|
| 额外 GET /recordsets/pnl per alpha | 1 |
| 单次 call 延迟 | ~0.5-1s(production CorrelationService 实测)|
| 日 cost overhead | 30-60s 额外 BRAIN API time / day |

**可承受** — 相当于 1 个 alpha simulate 的 wait time,即使 PASS 数量翻 5×(R12 GO 路径)仍 <5min/day。

## 3. OLS 路径架构(B2 PR 实施)

```
node_simulate → PASS alpha → node_evaluate
                                  ↓
                            R13 shadow:
                              1. get_alpha_pnl(alpha_id) → pnl_series
                              2. _series_to_returns(pnl_series) → daily returns
                              3. lstsq(X=factor_returns_snapshot, y=returns) → β + residual
                              4. residual_sharpe = mean(residual) / std(residual) × sqrt(252)
                              5. stamp metrics["_r13_residual_sharpe"] = float
                              6. (shadow) 不改 quality_status,只 stamp
```

X 矩阵 cache:同 region/universe/lookback 复用(plan v2 §4.6 注),numpy lstsq 50 alpha/day ≤10min compute。

## 4. 风险

| 风险 | 应对 |
|------|------|
| `_fetch_pnl_series` empty(rate-limit soft-fail / 真 empty alpha) | mirror CorrelationService:3× retry + last warning,empty 时 residual 设 None(shadow mode 可观察 "covered fraction") |
| factor_returns_snapshot.parquet 季度过期 | `/ops/r13/snapshot-stale-check` endpoint stale >90d 告警(per plan v2 §4.6) |
| X 矩阵 cache miss 触发突发 OLS | regionwise pre-warm via Sprint 2 spike output(可选)|

## 5. 决策

✅ **GO OLS 路径** — B2 R13 走 plan v5 §6.9 / v2 §4.6 完整实施,3.5 人日不变,无 fallback 设计开销。

**B2 PR 解锁**:可在 Sprint 2 内排进(任务 #53)。

## 6. 关联

- `backend/services/correlation_service.py:_fetch_pnl_series` — daily PnL fetcher 复用模板
- `backend/adapters/brain_adapter.py:get_alpha_pnl` — `/alphas/{id}/recordsets/pnl` wrapper
- Plan v5 §6.5 / v3 §4.6 / v2 §4.4
