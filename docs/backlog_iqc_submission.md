# IQC2026S1 Submission Strategy Backlog

> ✅ **DONE 2026-05-11** — backend + frontend 整栈实施完毕。原 backlog 估时 2.25 day,实施实际 ~2h(单 alpha-keyed endpoint,无需新建 service / model)。文档保留作下游使用 + KB feedback 增强(未做)的入口。

## Item: BRAIN before-and-after-performance API 集成

### 来源
用户问询 (2026-05-11) — 是否实现 `https://api.worldquantbrain.com/competitions/IQC2026S1/alphas/{alpha_id}/before-and-after-performance`。

确认现状:
- `ace_lib.py:1335-1367` 有 reference SDK 实现(同步 requests)
- ✅ `backend/adapters/brain_adapter.py:1158` 已实现(async + Retry-After 轮询 30× / 401 re-auth via `_safe_api_call`)
- ✅ `backend/services/alpha_service.py:get_marginal_contribution()` — 服务层包装 + 自动计算 delta(sharpe/fitness/turnover/returns/pnl/drawdown/score)
- ✅ `GET /api/v1/alphas/{alpha_id}/marginal-contribution?competition=IQC2026S1` — 路由暴露
- ✅ `frontend/src/pages/AlphaDetail.jsx` 边际贡献 tab(lazy fetch,自动渲染 score 变化卡片 + 6 项 metric delta tags)
- ✅ Unit tests 5/5 pass(live PG fixture pattern)

### 未做(可选 KB feedback 增强)
KB 反向 feedback — 边际 score < 0 的 alpha 写入 FAILURE_PITFALL anti-pattern 让 RAG 检索看见。当前 V-22 已把 BRAIN check verdict 喂回 LLM,但 marginal score 这个更精细的信号还没接到 KB。属"6 月观察后实测决定"的 backlog 项。

### 接口语义(对照 `ace_lib.py:1334` "merged performance check")
**单 alpha 的 standalone vs merged 表现对比**,不是池 before/after:
- `before`: 该 alpha **standalone**(独立 backtest)的 IS 表现 — 即 BRAIN simulate 常规 metric
- `after`: 该 alpha **merged 进 competition/team pool** 后,经组合相关性调整 / re-weighting / IRR 后**它自己**贡献的 PnL/sharpe
- 测的是 alpha 在组合中**被稀释或增强的程度**

样本(`e7dYX3Ez` @ IQC2026S1, team `BP40504`)关键洞察:
- pnl `5.39M → 6.07M` (+12.6%) — merged 后实际贡献 PnL 更高(与池中其他 alpha 互补 hedge)
- sharpe `3.19 → 3.14`、fitness `2.67 → 2.57` — merged 后边际稀释
- **competition score `7227 → 6780` (-6.2%)** — leaderboard 用 merged score,**不是 standalone**

**对当前 spike 数据的隐含影响**:spike 里看到的 PASS alpha sharpe 全部是 **standalone 数**,真实 IQC 参赛 score 可能差 5-15%。当前 can_submit gate 完全不看 merged 表现。

### 实施价值
✅ IQC 提交决策 — 选择"提交后 score 提升最大"的 alpha 子集
✅ 反向 feedback — 边际贡献负的 alpha 写 KB 作 "PASS but score-diluting" anti-pattern
✅ 池子健康度监控 — 已提交 alpha 数 + 池子 sharpe/fitness 趋势
❌ Mining 阶段无用(API 仅对已提交 alpha 返回数据)
❌ 大批量调用受 BRAIN `Retry-After` 节流

### 实施方案(IQC 提交冲刺期前做)

| 任务 | 文件 | 工时 |
|---|---|---|
| 1. `BrainAdapter.get_before_and_after_performance(alpha_id, competition=None, team_id=None)` async + 401 re-auth + Retry-After loop | `backend/adapters/brain_adapter.py` | 0.5 day |
| 2. `protocols/brain_protocol.py` 加 method 签名 + mock fixture | `backend/protocols/` + `tests/fixtures/mock_brain.py` | 0.25 day |
| 3. Service 层 `submission_strategy_service.py` — 批量分析 can_submit alpha,排序 marginal score 增量 | `backend/services/submission_strategy_service.py`(新)| 0.5 day |
| 4. 路由 `GET /api/v1/alphas/{alpha_id}/before-and-after?competition=...` | `backend/routers/alphas.py` | 0.25 day |
| 5. Frontend AlphaDetail 加 "Marginal Contribution" tab(score 变化 + yearlyStats before/after 对比图)| `frontend/src/pages/AlphaDetail.jsx` | 0.5 day |
| 6. 单测 + 集成测 | `tests/` | 0.25 day |
| **合计** | | **~2.25 day** |

### 触发时机
- IQC2026S1 stage 1 截止日确定后(查 BRAIN 网站 / `/competitions/IQC2026S1`)往前推 1 周启动
- 优先级:V-19 上线后 + can_submit 池累计 ≥ 30 个有效 alpha 时
- 不阻塞 R7 / Phase 1 / Phase 2 主路径

### 风险
- BRAIN API rate limit 不明 — 实施时先 1 alpha 测试 Retry-After 行为
- competition param 是 hardcoded "IQC2026S1" 还是动态 — 看 alpha 是否绑 competition
- score 计算逻辑 BRAIN 端可能调整,after 数值非确定性
