# Phase 2 实施计划 — Typed Hypothesis + KB 引用

> Plan v5+ §Phase 2 (9-12 day) 启动计划。Phase 1 已验收,Phase 2 是
> hypothesis 升级到一等公民:typed class + DB 持久化 + lifecycle +
> KB 学习单元升级。

## 目标(plan v5+ §C-Phase 2)

```
Phase 1 (已完成):
  hypothesis dict → 1 round 用一次 → 丢
  KB 学习单元 = (alpha, dataset, error_type)

Phase 2 (待实施):
  hypothesis = 一等公民 (typed Hypothesis class + DB 行)
  跨多 round 累积 alpha 到同一 hypothesis_id
  HypothesisFeedback (HYPOTHESIS / IMPLEMENTATION / BOTH 归因)
  abandon 阈值:N round 0 PASS + attribution=HYPOTHESIS → 退出
  KB 学习单元 = (alpha, hypothesis_id, attribution, dataset_pool)
```

## 已存在 / 已 active / dormant 一览

| 组件 | 状态 | 文件 |
|---|---|---|
| `Hypothesis` typed class | active(已用于 mining) | `backend/agents/core/experiment.py:31-91` |
| `HypothesisFeedback` + `AttributionType` | dormant | `backend/agents/core/feedback.py:25-150` |
| `LLMHypothesisGen` typed wrapper | active | `backend/agents/core/pipeline.py:76-197` |
| `Experiment2Feedback` typed wrapper | dormant | `backend/agents/core/pipeline.py` |

## Phase 2 子步骤拆分(我建议的实施顺序)

### B1. Schema 设计(0.5 day)
- `backend/models/hypothesis.py`(新)— Hypothesis ORM 模型
  字段:id / statement / rationale / region / dataset_pool jsonb /
        status enum / alpha_count / pass_count / sharpe_avg / abandon_reason / created_at
- `backend/models/alpha.py` — 加 `hypothesis_id INT NULLABLE FK hypotheses(id)` + 部分索引
- `backend/models/base.py` — TraceStepType 加 `HYPOTHESIS_PROPOSE` + `HYPOTHESIS_FEEDBACK`

### B2. Alembic migration(0.5 day)
- 新建 `hypotheses` 表
- alphas 表加 hypothesis_id 列(NULLABLE,无现有数据 break)
- 部分索引:`alphas(hypothesis_id) WHERE hypothesis_id IS NOT NULL`

### B3. node_hypothesis_propose 节点(1 day)
- 替换现有 `node_hypothesis`
- 用 `LLMHypothesisGen.gen(trace, queried_knowledge) → Hypothesis`(typed)
- INSERT INTO hypotheses 表拿 hypothesis_id
- 写入 state.current_hypothesis_id
- 生 trace step `HYPOTHESIS_PROPOSE`

### B4. alpha 持久化加 hypothesis_id(0.5 day)
- `workflow.run_with_persistence` + `_incremental_save_alphas`
- 写 Alpha 行时 `hypothesis_id=state.current_hypothesis_id`

### B5. node_hypothesis_feedback 节点(1 day)
- round 末调 `Experiment2Feedback.gen(...) → HypothesisFeedback`
- 按 attribution 决定 hypothesis 状态:
  - IMPLEMENTATION fail → 保留 hypothesis 再试
  - HYPOTHESIS fail → mark for abandon
- 更新 hypothesis.status / abandon_reason

### B6. should_abandon_hypothesis 早停(0.5 day)
- `backend/agents/graph/early_stop.py` 加 `should_abandon_hypothesis(hypothesis_history)`
- 阈值:N=3 round 0 PASS 且 attribution=HYPOTHESIS → abandon
- abandon → break round loop,下个 round 重新 propose

### B7. hypothesis_service.py(1 day)
- CRUD + lifecycle 状态转移 + 统计聚合
- promote / abandon / refine 状态机

### B8. KB 学习单元升级(1.5 day)
- `feedback_agent.learn_from_round` 加 hypothesis_id 引用
- SUCCESS_PATTERN 关联到产生它的 hypothesis
- KB query 路径 `KnowledgeEntry.meta_data.hypothesis_id`

### B9. RAG variant 隔离(0.5 day)
- 灰度期间 KB 检索过滤 `experiment_variant`
- 防止 Phase 2 数据污染 legacy variant

### B10. 单测 + 集成测(2 day)
- hypothesis lifecycle 状态机
- abandon 触发(IMPLEMENTATION × N → 不 abandon / HYPOTHESIS × N → abandon)
- KB hypothesis_id 关联

### B11. A/B vs Phase 1 baseline(2 day)
- HYPOTHESIS_CENTRIC_LEVEL=1 (Phase 1 baseline) vs LEVEL=2 (Phase 2)
- 跑 8-10 task,对比 PASS rate / hypothesis abandon rate / KB hypothesis-keyed entries

**总计: 10-11 day**(plan §"C-Phase 2 ~400 行 / 4-5 dev-days" 估算偏低,因为加了 schema migration + lifecycle 状态机)

## Phase 2 启动前置条件

- [x] Phase 1 验收通过(本报告)
- [x] V-12 / V-15 / V-17 修复全部上线
- [x] R7 audit / R1 Golden Set v0.1 就位
- [ ] **HYPOTHESIS_CENTRIC_LEVEL=1 设默认**(需重启 worker 让 .env 生效)
- [ ] 确认有足够 BRAIN 配额(Phase 2 A/B 8-10 task)
- [ ] 用户决定 Phase 2 启动时机(可立即 / 等几天看 Phase 1 稳定性 / 暂停)

## 下一步选择

| 路径 | 含义 |
|---|---|
| **P1. 立即启动 Phase 2 B1-B2**(schema + migration,1 day)| 最小风险开局,后续 sub-step 灰度推进 |
| **P2. 先全量上线 Phase 1**(`.env` LEVEL=1 + 重启)| 让 Phase 1 跑几天累积 baseline,然后再 Phase 2 |
| **P3. 暂停,留给下个会话**(当前已 push 完整 stack)| 18 commits 已 push 到 gitea/master,留底完成 |
