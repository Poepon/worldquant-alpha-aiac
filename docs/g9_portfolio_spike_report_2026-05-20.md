# G9-spike — Portfolio Simulator Feasibility

**Sprint**: Phase 4 Sprint 2 (per plan v5 §6.6 / v3 §4.5)
**人日**: 1.5
**Decision**: **PARTIAL GO** — Phase 5 自建 simulator GO,Phase 4 binary 排除 G9 PR
**Date**: 2026-05-20

## 1. 调研目标

| 问题 | 结论 |
|------|------|
| BRAIN `/portfolios/*` endpoint 存在? | **NO** |
| BRAIN `/multi-alpha-combine` endpoint 存在? | **NO**(`run_selection` 是 super-selection,不是 portfolio combiner)|
| BRAIN 有 marginal contribution endpoint? | **YES** — `before-and-after-performance`,但只能查单 alpha 加入既存 portfolio 的边际贡献,不能任意组合 |
| Consultant tier 解锁 portfolio API? | 无证据(brain_alpha_structure.json + api_structure.json 均无)|
| 自建 simulator 工时 ≤4 人日? | **YES** — 拉 N alpha PnL + 加权 combine + sharpe/drawdown 计算 |
| Regime conditioning ≤3 人日? | **YES** — 复用 P2-C `ENABLE_REGIME` 现有 regime stage |

## 2. BRAIN API 调研

### 2.1 Endpoints 实际可用清单(`ace_lib.py` + `brain_adapter.py`)

| Endpoint | 功能 | 与 G9 关联 |
|----------|------|-----------|
| `/simulations` POST | 单/多 alpha simulate(独立)| ❌ 不组合 |
| `/simulations/super-selection` GET | super alpha selection expression | ❌ 是单 alpha 过滤,不是 portfolio |
| `/alphas/{id}/recordsets/pnl` GET | daily PnL series 单 alpha | ✅ 自建 simulator 输入 |
| `/users/self/alphas/{id}/before-and-after-performance` GET | 单 alpha vs personal portfolio marginal | ⚠️ 仅 query,不能新建 portfolio |
| `/competitions/{c}/alphas/{id}/before-and-after-performance` | 同上 vs 比赛 portfolio | ⚠️ 同上 |
| `/teams/{t}/alphas/{id}/before-and-after-performance` | 同上 vs team portfolio | ⚠️ 同上 |
| `/alphas/{id}/correlations/PROD` | alpha vs prod portfolio 相关性 | ⚠️ 单维度 |
| `/users/self/alphas` 各 stage(IS/OS/CHECKING)| alpha 列表 | ✅ pool 输入 |

**Gap**:BRAIN 仅暴露 *single-alpha-vs-portfolio* 视角(加入或不加入既存 portfolio),**不允许**程序化构建任意 alpha 子集 + 任意 weight 的 portfolio backtest。

### 2.2 BRAIN marginal contribution endpoint 的局限

`get_before_and_after_performance` 返回:
- `stats.before` / `stats.after`(sharpe / fitness / pnl / turnover ...)
- `pnl.records`(per-day [date, beforePnL, afterPnL])
- `score.before` / `score.after`

但 **portfolio 是固定的**(personal / competition / team submitted alpha set),只能 "this candidate joined or didn't"。要做 G9 需要的 task — **从 N candidate 中选 k 个 + weighting 看组合 sharpe / sortino / drawdown** — 此 endpoint 帮不上。

## 3. 自建 simulator 可行性

### 3.1 数据可用性 ✅

- AIAC 已有 PASS / OS / IS alpha pool(规模数百+)
- `CorrelationService._fetch_pnl_series` 已经 production-tested,3× retry + empty-fallback,~0.5-1s / alpha
- LOOKBACK_YEARS=4 → 每 alpha ~1008 daily PnL points
- 同 region 同 universe 同 lookback 的 PnL series 可缓存到 `os_pnls_{region}.pkl`(已 LIVE)

### 3.2 引擎架构(Phase 5 实施)

```
PortfolioSimulator(region, lookback_years=4):
  ├─ load_pnl_matrix([alpha_id1, alpha_id2, ...])  → (N_days × N_alphas) DataFrame
  │   ├─ batch fetch via existing CorrelationService.refresh_os_alpha_cache
  │   └─ pickle cache reuse
  ├─ combine(weights: np.ndarray)                  → portfolio_pnl: pd.Series
  │   └─ pnl_matrix @ weights / sum(weights)
  ├─ metrics(portfolio_pnl)                        → {sharpe, sortino, max_dd, calmar, turnover}
  ├─ regime_conditioned_metrics(portfolio_pnl, regime_series)
  │   └─ groupby regime → per-regime sharpe/dd
  └─ optimize_weights(method)
      ├─ equal_weight (baseline)
      ├─ inverse_vol
      ├─ risk_parity (closed-form approx)
      └─ regime-aware (per-regime equal-vol + regime probabilities)
```

工时:
- portfolio engine(load/combine/metrics)+ test:2.5 人日
- weight optimizer 3 methods:1 人日
- regime conditioning(复用 P2-C `ENABLE_REGIME`):2 人日
- ops endpoint `/ops/g9/backtest` + frontend G9Monitor:1.5 人日
- Screener Agent(LLM 推荐 N→k 子集):1.5 人日 (条件性)

**total Phase 5 G9**:~8.5 人日(per plan v3 §4.5 "Phase 5 完整 G9 进 plan(4-6 portfolio engine + 3 regime weighting + 2 Screener Agent)" 估算 9-11 人日,本 spike refine 到 8.5)

### 3.3 Regime conditioning ≤3 人日 ✅

P2-C `ENABLE_REGIME` 已 LIVE,提供:
- `regime_inference_service.py` — daily regime label
- `regime_thresholds_service.py` — per-regime threshold band
- `regime_style_service.py` — per-regime style preference

G9 只需用 regime 时间序列做 portfolio_pnl groupby,~2 人日;额外 conditioning logic(regime probability × weight blend)~1 人日。

## 4. 决策 — PARTIAL GO

| 维度 | 决策 |
|------|------|
| **Phase 4 (现在)** | **G9 binary 排除**(plan v3.0 已定),不进 14 PR clip |
| **Phase 5 G9 GO 路径** | **GO with conditions** — 自建 simulator(BRAIN portfolio API 真不存在,排除 BRAIN-tier 升级救援可能)|
| **Phase 5 估算更新** | 8.5 人日(was 9-11,refine - 0.5)|
| **GO 触发**| Phase 4 ship 后(2026-07-19+)Sprint 5 cleanup 完 → Phase 5 开始,G9 是 Phase 5 第一个 PR |

**Rationale**:
- BRAIN 不提供 portfolio API → 自建 unavoidable
- 自建 cost 可控(<10 人日)且复用 70% 现有基础设施(CorrelationService + ENABLE_REGIME + os_pnls cache + ops 框架)
- Phase 4 工时已紧(48 人日),G9 强行塞 Phase 4 会挤掉 R8-v3 cognitive layer 或 G10 logic-as-asset 等更关键 PR

**Phase 4 不做** G9 的原因(plan v2/v3 已定,本 spike 复验):
- BRAIN portfolio API 不存在 → 没有 "等 Consultant tier 解锁" 的暗渠
- 自建 simulator 是 Phase 5 工作 — 与 Phase 4 critical path (R12 评估 + R14 风控 + R8/R11/R13 评估扩展)无依赖

## 5. 关联

- `backend/services/correlation_service.py` — PnL fetcher + os_pnls cache 模板
- `backend/services/regime_inference_service.py` — regime time series
- `ace_lib.py:run_selection` — 不是 portfolio combiner,只是 super-selection(确认)
- Plan v5 §6.6 / v3 §4.5 / Phase 5 G9 路线

## 6. 后续

- Phase 5 G9 PR 触发条件:Phase 4 ship + R12 decision 落地 + Sprint 5 cleanup 完成
- 本 spike 报告作为 Phase 5 plan 起点,无需 Phase 4 内再调研
