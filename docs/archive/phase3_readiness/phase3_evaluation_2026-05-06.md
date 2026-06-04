# Phase 3 提前评估 — 2026-05-06

> Plan v5+ §C-Phase 3 (主循环翻转 hypothesis-centric) 在 plan v5 Final
> §三轮精简时推到 Q3 (2026-07~09)。Phase 2 工程层完成后做一次提前评估，
> 决定是按计划 Q3 启动还是更早。

## TL;DR

**推荐：维持 Q3 时间表，不提前启动**。理由：
- Phase 2 价值未经实测验证（B11 N=3 不足以下结论）
- Phase 3 主循环翻转 = 高 blast radius；在 Phase 2 metrics 不明时叠加风险大
- 还有 ~2 个月（5 月→7 月）可累积真实 LEVEL=2 生产数据，到 Q3 决策有数据支撑
- 当前 Phase 1+2 已是 plan v5 Final 核心交付，工程层完整

立即可做的 Phase 3 准备工作（不动主循环）已列在文末 §"提前可做项"。

---

## Phase 3 设计回顾

Plan v5+ §C-Phase 3 关键改动：

```diff
现状 (Phase 2 完成态)：
   for dataset_id in datasets:                        ← 外层 dataset
       fields = _get_dataset_fields(...)
       for round in max_iterations:                    ← 内层 round
           hypothesis_propose → ... → save_results

Phase 3 翻转：
+  for hypothesis_round in range(hypothesis_count):   ← 外层 hypothesis
       HYPOTHESIS_PROPOSE
         → Hypothesis + selected_datasets
       fields = union of selected_datasets' fields
       for alpha_round in alphas_per_hypothesis:      ← 内层 alpha
           CODE_GEN → SIMULATE → EVALUATE → SAVE
       HYPOTHESIS_FEEDBACK
```

### 核心改动点（plan §C-Phase 3 §"关键改动"清单）

| 文件 | 改动 | 工时估 |
|---|---|---|
| `mining_tasks.py` | 主循环改 `for hypothesis_round in max_hypothesis_rounds`；daily_goal 跨 hypothesis 累计 | ~80 行 |
| `mining_agent.py` | run_evolution_loop 接 `hypothesis: Hypothesis` 而非 `dataset_id` | ~60 行 |
| `workflow.py` | conditional entry: T1+L3 → run_enhanced_mining 路径；T2/T3 → 不变 | ~50 行 |
| `integration.py:run_enhanced_mining` | 从 dormant 改成 production entry | ~40 行 |
| `task_service.py` | TaskCreate 接受 `hypothesis_count`（默认 5）| ~20 行 |
| frontend `TaskManagement.jsx` + `TaskDetail.jsx` | UI 暴露 hypothesis_count；TaskDetail hypothesis-grouped | ~50 行 |
| 集成测 | T1 task 跑通端到端 + T2/T3 不受影响 | ~50 行 |
| **合计** | **~300 行 / plan 估算 3-4 day** | |

实际加上 LangGraph state propagation 教训（Phase 2 发现 6 个 V-19.x bug），保守估 **5-7 day**。

---

## 提前启动 Phase 3 的支持证据

| ✅ 支持 | 说明 |
|---|---|
| Phase 2 工程层完整 | typed Hypothesis + lifecycle + KB hypothesis-keyed + B5 v2 LLM 全就位 |
| `run_enhanced_mining` 已 dormant | plan §C-Phase 3 设计的入口已存在，激活而非新建 |
| LangGraph 教训累积 | V-19.x 6 个 bug 都修了，团队对 state propagation / SAVEPOINT / variant isolation 有经验 |
| 测试基础设施成熟 | 345 tests + smoke + B11 production verification 路径，Phase 3 单测 + 集成测可复用 |

---

## 反对提前启动的证据（更重）

| ❌ 反对 | 严重 | 说明 |
|---|---|---|
| **Phase 2 价值未实测** | 🔴 高 | B11 N=3 effective per variant，PASS rate -25%（在 30% 容忍内但方向是反的）。在 Phase 2 是否有效都没数据时叠加 Phase 3 = 复合不确定性 |
| **主循环翻转 = 高 blast radius** | 🔴 高 | mining_tasks.py 是核心入口，T2/T3 路径要保留意味着大量 conditional 分支。Phase 2 已经有 V-19.5/6/7 三个 lifecycle 微调，Phase 3 还会暴露更多 |
| **没有"hypothesis 主导优于 dataset 主导"的实证** | 🟠 中 | Plan v5+ §A-architecture 是设计假设，目前没有数据证明它在 BRAIN 平台真的更好 |
| **Frontend 改动需要设计** | 🟠 中 | TaskDetail hypothesis-grouped 视图不只是后端改造，需要 UI/UX 决策 |
| **BRAIN 配额仍紧张** | 🟠 中 | B11 跑 8 task 用了 4 小时；要做 Phase 3 实测 A/B 需要 N≥20，跑量更大 |
| **plan v5 Final 三轮精简刚定** | 🟡 低 | 决策刚做 1 周就推翻有 process 成本，除非有强信号 |

---

## 关键决策因子：Phase 2 真值

Phase 3 的 ROI 高度依赖 Phase 2 的实测价值。当前数据：

| 维度 | 状态 |
|---|---|
| 工程层 (alpha.hypothesis_id, lifecycle, KB) | ✅ 100% 验证 |
| 性能层 (PASS rate / cross-dataset / OS retention) | **⏳ 不足**（n=3）|

**Phase 3 的核心论断**："hypothesis 是更好的 mining 单元" — 需要 Phase 2 数据回答两个子问题：

1. **同一 hypothesis 跨 round 累积 alpha 是否带来更高 PASS rate？**
   现状无法回答 — Phase 2 任务 max_iterations=2 太少，没有真正的"跨 round 累积"
2. **PROMOTED 的 hypothesis 在后续 round 是否更可能产 alpha？**
   现状无法回答 — 同一 hypothesis 没机会跑多 round

要回答这两个，需要：
- N ≥ 10 task at LEVEL=2，max_iterations ≥ 5（让 hypothesis 跑足够长）
- 跟踪 hypothesis 跨 round 的 PASS rate trajectory
- 比较"PROMOTED hypothesis 后续 round PASS rate" vs "ABANDONED hypothesis 早期 round PASS rate"

预估 BRAIN 配额：10 × 5 round × ~3 alpha = 150 simulate；预计 ~5-7 day 持续 mining 收集数据。**这是 Q3 启动 Phase 3 之前的必做"打底"工作**。

---

## 推荐路径

### 立即（5 月-6 月）— 不动主循环

1. **生产观察期**（默认配置 LEVEL=2 灰度）
   - 设 `task.config.hypothesis_centric_variant=2` 为部分新任务的默认
   - 累积 N ≥ 10 LEVEL=2 task 真实数据
   - 监控指标：alpha.hypothesis_id 分布 / PROMOTED/ABANDONED 比例 / KB hypothesis-keyed retrieval 命中率

2. **小工程优化**（不阻塞 Phase 3）
   - 把 `max_iterations` default 从 2 升到 5（让 hypothesis 有足够 round 累积数据）
   - Frontend 加一个 hypothesis-list 只读 view（不是 hypothesis-grouped 主视图，只是看数据）
   - 监控告警：B5 v2 LLM 调用频率 / 失败率

3. **Phase 2 性能回归监控**
   - 每周自动跑 phase2_ab_compare 累积报告
   - 如果 Phase 2 PASS rate 持续 < Phase 1 -20% → 触发 RCA（plan §V-1 灰度回滚机制）

### Q3 (7-9 月)— Phase 3 启动条件

启动门槛（必须满足才动主循环）：

| 门槛 | 阈值 | 当前 |
|---|---|---|
| Phase 2 N ≥ 20 task 实测 | n_completed ≥ 20 | ❌ 0（B11 8 task 还在跑）|
| Phase 2 PASS rate ≥ Phase 1 - 20% | within margin | 待数据 |
| Hypothesis cross-round PASS rate trajectory 数据 | n_hypothesis with ≥3 rounds | ❌ 0 |
| BRAIN 配额季度总额 | ≥ 500 simulate budget | 待 Q3 评估 |
| LLM API 价格稳定 | DeepSeek pricing not surged | 待 Q3 评估 |

### 提前可做项（不阻塞 Phase 3，工程层 < 0.5 day each）

- ✅ **B11 跑量延长**：`max_iterations` 从 2 提升到 5（task config 改一行）— 让 Phase 2 的 cross-round lifecycle 真正发生
- ✅ **Hypothesis age tracking**：HypothesisService 加 `rounds_active` 字段或 SQL view，方便分析"老 hypothesis 是否更好"
- ✅ **`run_enhanced_mining` 注释升级**：把 dormant 标记改成 "Phase 3 entry, blocked on B11 实测"，让未来 contributor 看清依赖关系
- ✅ **写 Phase 3 启动 readiness check 脚本**：自动检查上述 5 个门槛，给"GO / NO-GO"信号

---

## 决策

**维持 Q3 (2026-07-09) 时间表**。当前 Phase 2 工程层完整、生产环境刚启用，是观察期不是叠加期。

不实施 Phase 3 不影响 Phase 2 价值兑现 — Phase 2 的 hypothesis-keyed KB / lifecycle / B5 v2 attribution 都是独立增量，跨 round 跨 task 持续累积。

到 Q3 评估时，根据上述 5 个门槛 + 累积数据决定是 GO 还是再推。

---

## 历史记录

| 时间 | 决策 | 文件 |
|---|---|---|
| 2026-05-02 | Plan v5 Final §三轮精简 把 Phase 3 推 Q3 | (plan 内) |
| 2026-05-06 | Phase 2 工程层完成 + B5 v2 升级 | `phase2_completion_2026-05-06.md` |
| 2026-05-06 | **Phase 3 提前评估 — 维持 Q3 时间表** | 本文 |
| Q3 (TBD) | Phase 3 启动 / 再推决策 | 待写 |
