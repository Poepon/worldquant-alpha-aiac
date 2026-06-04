# Phase 1.5 — Task Schema 收敛到 agents/core/(实施计划)

> **文档版本**:v2(2026-05-17 实测反馈整合 + flat search 完整设计扩展)
> **v1 日期**:2026-05-16
> **关联调研**:[`rd_agent_alpha_gpt_research_2026-05-16.md`](rd_agent_alpha_gpt_research_2026-05-16.md)
> **v2 增量来源**:
> - 2026-05-16 跑量实测(task 652 cascade + task 1330 TIER1 + Bug B fix 链)
> - 学界竞品对比(RD-Agent / Alpha-GPT / AlphaAgent / Hubble v2 / QuantaAlpha)
> - 对抗性思考结论:cascade T1/T2/T3 在学术 / 工业实践里**零先例**,AIAC 是孤例
>
> **决策锚点**(v2 修订):
> - R1a/R1b 路径(放弃 R1c 退出选项 — 原决策不变)
> - **v2 新增**:R1a 提前到 Phase 0(原 Phase 1)— GO 闸门论证见 §11.3
> - **v2 新增**:flat search 作为 R1b 的 reference 技术路径(§13)
> - Phase 1.5 在 R1a 实测后启动(用实测数据反证范围 — 原决策不变)
>
> **目标**:
> - **v1 目标**(不变):让 `MiningTask` schema 收敛到 `backend/agents/core/` 已有数据结构,为 R2 / R6 / R1b 铺路
> - **v2 扩展**:把 R1a 启用 + Bug B fix 实测证据 + flat search 切换路径整合进 master plan,作为 Phase 0 → Phase 1.5 → Phase 3 三段闭环的实施依据

---

## ⚠️ v2 状态更新 — GO 闸门实测评估(2026-05-17)

**phase15 §0 GO/NO-GO 闸门 4 条**实测核查:

| 闸门 | v1 plan 假设 | 2026-05-17 实测 | 状态 |
|---|---|---|---|
| Phase 1 (R1a + R3 + R4') ship 且 production ≥ 2 周 | 假设已 ship | R1a **未启用**(`grep -rn "from backend.agents.core" backend/agents/graph/ backend/agents/mining_agent.py backend/tasks/ backend/services/ backend/routers/ backend/celery_app.py` = **0 matches**;R3 未实施;R4' 未实施)| **❌ 未达** |
| `enhance_existing_node_evaluate` 至少 100 次触发,AttributionType 分布有数据 | 假设已采集 | hook 从未在生产 wire 进 mining 主路径 → 0 次触发 | **❌ 未达** |
| R2 候选 arm 集已离线收敛 | 假设已收敛 | R2 未启动 | **❌ 未达** |
| Phase 3 时间表未提前 | Q3 2026 不变 | 不变 | ✅ |

**结论**:phase15 v1 GO 闸门 **1/4 满足**,不能直接启动 Revision A-D。但 v1 §0 的 NO-GO 触发条件("Phase 1 R1a 实测收益 < +5% PASS 率提升")**也没数据反驳** — 因为 R1a 根本没 ship 过任何一行 production code。

**v2 修订**:Phase 0 必须先做 R1a 启用 + 2 周观察期,把 GO 闸门 4/4 拉满后再启动 Phase 1.5 Revision A。详见 §14。

---

## 0. 前置条件(GO/NO-GO 闸门)

启动前必须满足:

- ✅ Phase 1 (R1a + R3 + R4') 全部 ship,在 production 跑 ≥ 2 周
- ✅ `enhance_existing_node_evaluate` 至少触发 100 次,`AttributionType` 分布有数据
- ✅ R2 候选 arm 集已通过离线实验收敛(决定 `generation_strategy` 是 2/3/4 个值)
- ✅ Phase 3 时间表未提前(若 Q3 提前到 Q2,本计划压缩;若推到 Q4,本计划照跑不变)

**NO-GO 触发条件**:Phase 1 R1a 实测收益 < +5% PASS 率提升 → 切换到 R1c 路径,本计划退化到方向 A(只做 §3.1 + §3.2,跳过 §3.3 / §3.4)。

---

## 1. 范围(四项重构)

| # | 项 | 改什么 | 为谁铺路 |
|---|---|---|---|
| **3.1** | `task.config` JSONB → Pydantic `TaskConfig` | 新 Pydantic 包装,DB 仍是 JSONB | R1b 时 `Scenario.config` 复用同一个 schema |
| **3.2** | `mining_mode` + `agent_mode` + `cascade_phase` 三字段合并简化 | 新加 `schedule` + `starting_tier` 二维正交列(替代前三者),删 INTERACTIVE 死枚举;`generation_strategy` 列独立(与 tier 正交) | R2 (Direction Bandit) + R1b |
| **3.3** | 运行时状态下沉到 `ExperimentRun.runtime_state` | 4 列从 `MiningTask` 搬到 `ExperimentRun`,`cascade_phase` 由 `runtime_state["current_tier"]` 取代 | R6 (Trace MCTS) + R1b |
| **3.4** | tier 推进线性 → DAG | `ExperimentRun.runtime_state["dag"]` 持节点链,每节点带 `current_tier` | R6 主菜 |

---

## 2. 列迁移表

### 2.1 字段替换(三字段合一 + 死枚举清零)

| 旧列 | 新位置 | 迁移规则 |
|---|---|---|
| `mining_mode` (`DISCRETE` / `CONTINUOUS_CASCADE`) | `mining_tasks.schedule` (`ONESHOT` / `CASCADE`) | 直接重命名映射 |
| `agent_mode` (`AUTONOMOUS` / `INTERACTIVE` / `AUTONOMOUS_TIER1..3`) | `mining_tasks.starting_tier` (Integer 1/2/3) | `AUTONOMOUS`→1, `TIER1`→1, `TIER2`→2, `TIER3`→3, `INTERACTIVE` 行**审查后删除**(grep 已证零调用) |
| `cascade_phase` (`T1`/`T2`/`T3`/`IDLE`/`NULL`) | **删除** — 由 `runtime_state["current_tier"]` 接管 | CASCADE 任务:`current_tier = {T1:1, T2:2, T3:3}[cascade_phase]`;ONESHOT 任务:不存在(读取走 `starting_tier`) |

### 2.2 运行时状态下沉(4 列)

| 列名 | 当前位置 | 终态位置 | 类型 | 迁移策略 |
|---|---|---|---|---|
| `cascade_round_idx` | `mining_tasks` | `experiment_runs.runtime_state["round_idx"]` | int | 双写 → 切读 → 删 |
| `progress_current` | `mining_tasks` | `experiment_runs.runtime_state["progress"]` | int | 双写 → 切读 → 删 |
| `current_iteration` | `mining_tasks` | `experiment_runs.runtime_state["iteration"]` | int | 双写 → 切读 → 删 |
| `last_alpha_persisted_at` | `mining_tasks` | `experiment_runs.runtime_state["last_persisted_at"]` | datetime | 双写 → 切读 → 删 |

**留在 `mining_tasks` 的运行时字段**:`status`(任务级生命周期,影响 PAUSE/RESUME 判断;Run 级 status 已存在,职责不同 —— task.status = 用户视角,run.status = 一次执行视角)。

### 2.3 新加列(在 `mining_tasks` 上,任务定义)

- `schedule`: `String(20)`, NOT NULL, default `"ONESHOT"`(取值 `ONESHOT` / `CASCADE`)
- `starting_tier`: `Integer`, NOT NULL, default `1`(取值 `1` / `2` / `3`;CASCADE 永远 `1`,ONESHOT 启动时定)
- `generation_strategy`: `JSONB`, default `["llm"]`(R2 bandit arm 候选集,**与 tier 正交**,不要合并)

### 2.4 运行时不变式

- ONESHOT: `runtime_state["current_tier"] == starting_tier`(永远成立)
- CASCADE: `starting_tier == 1` AND `runtime_state["current_tier"] ∈ {1, 2, 3}`(round 推进时改变)
- 任何 `current_tier < starting_tier` = bug(回归断言)

---

## 3. Alembic 4 步走(每步独立 revision,可独立回滚)

### Revision A:加列(零风险,可立即上)

```
add column mining_tasks.schedule              String(20)  NOT NULL DEFAULT 'ONESHOT'
add column mining_tasks.starting_tier         Integer     NOT NULL DEFAULT 1
add column mining_tasks.generation_strategy   JSONB                DEFAULT '["llm"]'
add column experiment_runs.runtime_state      JSONB                DEFAULT '{}'
```

- 影响:无(新列无人读)
- 回滚:`alembic downgrade -1` drop 4 列

### Revision B:回填 + 双写代码部署

回填 SQL:

- `schedule = 'CASCADE' if mining_mode='CONTINUOUS_CASCADE' else 'ONESHOT'`
- `starting_tier = 1 if schedule='CASCADE' else AGENT_MODE_TO_TIER.get(agent_mode, 1)`
- `generation_strategy = '["llm"]'`(所有历史 task 默认值)
- `runtime_state["current_tier"] = {T1:1, T2:2, T3:3}.get(cascade_phase, starting_tier)`
- `runtime_state` 其余字段(round_idx/progress/iteration/last_persisted_at)从最近一次 `ExperimentRun` 反推 + 从 `MiningTask` copy
- INTERACTIVE 任务审查:`SELECT id FROM mining_tasks WHERE agent_mode='INTERACTIVE'`,**预期 0 行**;若非 0 → 暂停,人工裁决

代码:`TaskService.create_task` 同时写新旧列;`mining_tasks.py` 同时读旧列(权威)+ 旁写新列。

- 影响:写放大 ~3%,读路径不变
- 回滚:`alembic downgrade -1` 清回填(数据保留旧列,无丢失)

### Revision C:切读(高风险窗口,做灰度)

代码:所有读取改读新位置,旧列变只读 fallback:

- `task.schedule or ('CASCADE' if task.mining_mode=='CONTINUOUS_CASCADE' else 'ONESHOT')`
- `task.starting_tier or AGENT_MODE_TO_TIER.get(task.agent_mode, 1)`
- `run.runtime_state.get("current_tier") or {T1:1,T2:2,T3:3}.get(task.cascade_phase, task.starting_tier)`

灰度策略:用 `ENABLE_TASK_SCHEMA_V2` flag override,先 staging → 单 task → region 全量。

- 影响:cascade worker 重启路径(`mining_tasks.py:1251-1264`)、watchdog (`session_watchdog.py`)、router 响应、ops dashboard
- 回滚:flag flip OFF 立即切回旧列(代码保留 fallback)

### Revision D:删旧列(Phase 2 完成 + 4 周稳定期后)

- 删 `mining_mode` / `cascade_phase` / `cascade_round_idx` / `progress_current` / `current_iteration` / `last_alpha_persisted_at`(**6 列**)
- `agent_mode` **保留**(legacy view 兼容,标 deprecated;Phase 3 R1b 时再删)
- 回滚:Alembic 反向加列 + 从 `runtime_state` + `schedule` + `starting_tier` 反向 copy(脚本预备好)

---

## 4. 影响面清单(grep 已验)

**必改文件**(读/写迁移列 + 三字段合并影响):

- `backend/tasks/mining_tasks.py` — cascade 主循环 + dispatch(`if task.mining_mode=='CONTINUOUS_CASCADE'` 改为 `if task.schedule=='CASCADE'`)
- `backend/tasks/session_watchdog.py` — liveness 检测
- `backend/services/task_service.py` — start/pause/resume + `AGENT_MODE_TO_TIER` 改写为 `starting_tier` 直读
- `backend/routers/mining_session.py` — `MiningSessionResponse` 移除 `cascade_phase`,加 `current_tier`
- `backend/routers/tasks.py` — `TaskResponse` / `TaskCreateRequest`(`agent_mode` → `starting_tier`)
- `backend/routers/dashboard.py` — 进度展示
- `backend/agents/mining_agent.py` — 读 progress/iteration
- `backend/agents/graph/workflow.py` — 读 `config["brain_role_snapshot"]`
- `backend/agents/graph/nodes/generation.py` — 读 config
- `backend/models/base.py` — `AgentMode` enum 标 deprecated,新增 `Schedule` enum(`ONESHOT`/`CASCADE`)

**测试改动**(预估 ~20 个文件):

- `backend/tests/conftest.py` — fixture 加新字段
- `backend/tests/integration/test_capability_isolation.py`
- `backend/tests/integration/test_v27_1_cascade_lock_takeover.py`
- `backend/tests/test_v19_mining_session.py`
- `backend/tests/integration/test_task_config_snapshot_propagation.py`
- 其余 `test_phase2_*.py` 多个文件用 `agent_mode="AUTONOMOUS_TIER1"`,需同步 `paradigm`/`factor_tier`

---

## 5. 回滚预案(三层)

| 触发 | 动作 | 时长 |
|---|---|---|
| Revision C 灰度发现读 bug | `ENABLE_TASK_SCHEMA_V2=False` flip | < 1 分钟 |
| Revision B 双写发现数据漂移 | `alembic downgrade -2` + 代码 revert | < 30 分钟 |
| Revision D 删列后才发现遗漏读路径 | Alembic 反向加列 + 从 `runtime_state` 回填脚本 | < 2 小时 |

**关键不可回滚点**:Revision D。所以 D 必须在 R6/R7/R10 全部 ship + 稳定 4 周后执行。

---

## 6. 测试策略

- **新增** `backend/tests/migration/test_phase15_dual_write.py` — 验证 Revision B 双写一致性
- **新增** `backend/tests/migration/test_phase15_runtime_state_schema.py` — 验证 Pydantic schema 拒绝未知键
- **复用** `tests/baseline.json` 回归基线 — Phase 1.5 不应改变任何 alpha 指标,baseline 若变化 = bug
- **手测清单**:
  - cascade PAUSE/RESUME 跨 Revision B↔C 切换
  - watchdog 在新位置正确触发
  - dashboard 进度数字一致
  - ops console flag flip 实测 ENABLE_TASK_SCHEMA_V2 切换无中断

---

## 7. 时间估算

| 阶段 | 工程量 | 日历周 |
|---|---|---|
| Revision A 加列 + 测试 fixture 修 | 2 人日 | 0.5 周 |
| Revision B 双写 + 回填脚本 + 灰度验证 | 3 人日 | 1 周 |
| Revision C 切读 + 灰度推全 | 3 人日 | 1 周(含观察) |
| Pydantic TaskConfig (§3.1) | 2 人日 | 与 A/B 并行 |
| 三字段合并简化 (§3.2) `schedule`+`starting_tier`+`generation_strategy` | 2 人日 | 与 A/B 并行 |
| **合计 Phase 1.5 上线** | **10-12 人日** | **2.5 周** |
| Revision D 删旧列(独立窗口) | 1 人日 | Phase 2 后 4 周稳定期 |

---

## 8. 与 Phase 2 / Phase 3 的衔接

- **R2 (Direction Bandit)** 在 Phase 1.5 Revision C ship 后立即可做:bandit 选择写入 `runtime_state["arm_history"]`
- **R6 (Trace MCTS)** 直接在 `runtime_state` 上加 `dag` 字段,不动 schema
- **R1b (Q3 2026)** 时 `MiningTask` ↔ `Scenario`、`ExperimentRun` ↔ `Experiment` 是一对一映射,迁移 = 改 `__tablename__` + dataclass 适配,**0 schema 变更**

---

## 9. 决策记录

| 决策 | 选项 | 选定 | 原因 |
|---|---|---|---|
| 走 R1a/R1b 还是 R1c | a + b / c | **a + b** | 沉没成本(3223 行 core/)变资产;Phase 3 hypothesis-as-driver 是产品方向 |
| Phase 1.5 时机 | Phase 1 之前 / 之后 | **之后** | 用 R1a 实测数据反证 `generation_strategy` arm 集 / `runtime_state` 字段集 |
| `mining_mode`/`agent_mode`/`cascade_phase` 处置 | 各自保留 / 三字段合并 | **三字段合并为 `schedule` + `starting_tier`** | 信息论冗余:CASCADE ignore agent_mode、AUTONOMOUS≡TIER1、INTERACTIVE 死枚举(grep=0)、cascade_phase 50%+ NULL 全是症状 |
| `generation_strategy` 与 tier 关系 | 合并为单字段 / 独立列 | **独立列** | tier 是算子组合层级,strategy 是 R2 arm 选择,两个正交概念 |
| `agent_mode` 终态 | 删除 / 保留 deprecated | **Revision D 保留,Phase 3 删** | 降低单步风险,与 R1b 合并删除 |
| `cascade_phase` 终态 | 重命名 / 删除 | **Revision D 删除** | 已被 `runtime_state["current_tier"]` 完全取代,无需保留 |
| `status` 字段去向 | 下沉到 Run / 留在 Task | **留在 Task** | task.status = 用户视角,run.status = 执行视角,职责不同 |
| **v2 新增**:R1a 启动时机 | Phase 1 / Phase 0(提前) | **Phase 0**(提前) | v1 假设 R1a 已 ship,实测 0 触发(§11.3);R1a 不启动则 phase15 GO 闸门永远 0/4 |
| **v2 新增**:cascade 软停机制是否保留 | 保留 / 改 watchdog auto-resume / 改 run 内无限循环 | **Phase 3 时随 flat 切换一起改**(无限循环) | cascade 软停是 ops 反模式(§11.4),但单独修不值 — 与 flat 切换合并 |
| **v2 新增**:flat search 是否纳入 R1b 路径 | 是 / 否 / 平行第三路径 | **是,作为 R1b 的 reference 技术路径** | 学术 SOTA 全部 flat(§12);AIAC `core/` 既有 RD-Agent 风骨架支持 flat;不需要重新发明 |
| **v2 新增**:Bug B fix 是否前置 R1a | 是 / 否 | **是,2026-05-16 已 commit a425937** | flip-retry alpha 跳过 `_evaluate_single_alpha` 会让 R1a hook 漏数据;fix 后 main + flip 两条路径都走 hook,AttributionType 采集完整无偏 |

---

## 10. 未决问题(待 Phase 1 数据回填)

- [ ] `generation_strategy` 默认 arm 集是否包含 `genetic`/`rag_template`/`knowledge_pattern`(取决于 R2 离线实验)
- [ ] `runtime_state` 是否需要存 `arm_history` 完整序列,还是只存最近 N 条(取决于 R6 MCTS 选树深度)
- [ ] Pydantic `TaskConfig` 是否拒绝未知键(strict)或宽松(extra="allow")—— 建议 strict,但需评估对在跑 cascade task 的兼容性
- [ ] Revision B 前置审查 `SELECT id FROM mining_tasks WHERE agent_mode='INTERACTIVE'` 是否真为 0 行(grep 显示生产路径零调用,但历史数据需 SQL 二次确认)
- [ ] 历史 CASCADE 任务的 `starting_tier` 是否全部应该回填为 1(永远从 T1 起,需确认无例外)

---

*v1 本计划由 [`rd_agent_alpha_gpt_research_2026-05-16.md`](rd_agent_alpha_gpt_research_2026-05-16.md) 调研结论驱动。Phase 1.5 仅是 task schema 重构,不替代 R 系列功能项;每个 Revision 独立 PR,可独立回滚。*

---

## 11. 实测增量证据(v2 新增,2026-05-17)

本节是 v1 plan 写成时缺失的实测反证 — 用 5/15-16 跑量数据 + 学界对照验证 v1 假设。

### 11.1 Bug B 发现 + 修复 — R1a 隐性前置条件

**场景**:2026-05-16 跑 5 个 cascade smoke task(1313/1325/1326/1327/1328/1329/1330)+ resume task 652,共 50 个 PASS_PROVISIONAL alpha 落库。验收 P2-C `_regime_at_eval` 数据采集时发现 alpha.metrics **0/50 命中 `_regime_at_eval` stamp**。

**Root cause**:`backend/agents/graph/nodes/evaluation.py:1656-1733` T1 sign-flip retry path 自己实现一套简化 gate,**跳过 `_evaluate_single_alpha`**,导致 flip alpha 不走 line 646 spread + line 737 P2-C stamp + dual_run / graded / robustness 全部新功能。alpha 10025-10029(task 1313/1325 落库的所有 PASS_PROVISIONAL)**100% 是 flip-retry 产物**(main loop sharpe 负 → flip 后翻正 PROV),metrics 只剩 BRAIN raw + `_sim_settings`。

**Fix**:commit `a425937`(2026-05-16),把 flip-retry path 改成 `await _evaluate_single_alpha(new_alpha, _ctx)` 走完整管线 + 镜像 fear-score fallback。

**Fix 验证**:
- 111 evaluate 相关单测 + 集成测试 PASS(env 显式 override = false 隔离)
- In-memory test:flip alpha metrics 33 字段含 `_regime_at_eval='normal'` + `_score=1.685`(pre-fix ~15 字段无 stamp)
- 生产 task 1330 + task 652 resume 13 个 alpha **100% 命中 stamp**

**为什么这是 R1a 隐性前置条件**:
- R1a hook `enhance_existing_node_evaluate(alpha, ctx, regime, attribution, ...)` 设计为 evaluate node 末尾 shim,捕获 alpha + ctx + AttributionType
- pre-Bug B fix 时 flip alpha 跳过 `_evaluate_single_alpha`,**R1a hook 永远捕不到 flip alpha**
- AIAC 历史 alpha 中 flip-retry alpha 占 **27/37(73%)**(task 652 derived alpha 数据),意味着 R1a 采集会**严重偏向 main-loop alpha**,AttributionType 分布失真
- post-Bug B fix 后 flip alpha 走完整 `_evaluate_single_alpha`,R1a hook 可稳定捕获所有 alpha — 这是先做 Bug B fix 再启动 R1a 的工程依赖

### 11.2 task 652 cascade resume 实测(2026-05-16 12:20-14:12 UTC)

cascade task 652(`mining-session-USA`,V-19 CONTINUOUS_CASCADE)resume 1h52m,13 个 PASS_PROVISIONAL alpha 落库:

| 来源 | 数量 | 备注 |
|---|---|---|
| **derived**(parent_alpha_id 非空,T2 wrapper sweep)| **7/13** | 5 个同 parent 7820,2 个同 parent 5476 |
| main(T1 新生成) | 6/13 | |
| can_submit=True | **0/13** | 全部因 `LOW_FITNESS / HIGH_TURNOVER / CONCENTRATED_WEIGHT / LOW_SUB_UNIVERSE_SHARPE` 卡 BRAIN 评估门 |

**Parent 7820 衍生的 5 个 alpha 同病**:`ts_arg_min(fnd6_acodo, 120)` base signal,5 种 group_* wrapper(`group_scale/sector`、`group_scale/industry`、`group_scale/subindustry`、`group_neutralize/sector`、`group_rank/subindustry`)**全部 LOW_SUB_UNIVERSE_SHARPE FAIL**(TOP3000 整体 sharpe 1.5+ 但 sub-universe 0.18-0.49,低于阈值 0.64-0.66)。

**T2 wrapper sweep 是"伪进化"实证**:5 个 BRAIN sim 浪费在用 group_* wrapper 救一个结构性死掉的 base signal — 这不是进化,是"对同一个 base signal 试 5 种皮"。完美演示 v1 plan §1 列的"tier 推进线性 → DAG"(§3.4)为什么不只是工程优化,而是修正一个**算法概念错误**:**alpha 的 alpha 经常就在 wrapper**(`group_neutralize / winsorize / ts_decay`),把 wrapper 当二等公民在 T2 才加是错的。

### 11.3 R1a 启用紧迫性 — grep 反证 plan 假设

phase15 v1 §0 闸门写 "✅ Phase 1 (R1a + R3 + R4') 全部 ship,在 production 跑 ≥ 2 周",但实测:

```bash
$ grep -rn "from backend.agents.core" backend/agents/graph/ backend/agents/mining_agent.py \
    backend/tasks/ backend/services/ backend/routers/ backend/celery_app.py
# 0 matches
```

`agents/core/` 的 3223 行代码 **production 路径零调用**。`enhance_existing_node_evaluate` 至今未在 mining 主路径 wire,触发次数 = 0。

**意味着**:
- phase15 v1 §0 NO-GO 触发条件"Phase 1 R1a 实测收益 < +5% PASS 率提升 → 切 R1c"**永远无法触发**(没数据)
- v1 §10 未决问题 5 项(`generation_strategy` arm 集 / `runtime_state` 字段集等)**永远无法回答**(plan 自己说"用 R1a 实测数据反证范围")
- phase15 是 dead-locked 在等 R1a,R1a 又因为没人启动而 dead-locked

**v2 解 lock 方案**:Phase 0 必须先做 R1a 启用(§14)— 2 人日工程量,与 Bug B fix(已 done)+ ENABLE_NEGATIVE_KNOWLEDGE_NUDGE flip(已 done)凑成"Phase 0 三件套",2 周观察期后启动 phase15 Revision A。

### 11.4 cascade 软停机制 — 设计反 ops

实测 task 652 resume 行为:
- 12:20:22 UTC start,14:12:17 UTC PAUSED — 跑 1h52m,产 13 alpha
- 我手动 resume 第二次:16:20:58 start,16:29:51 PAUSED — 跑 9m,产 0 alpha
- 第三次:16:24:44 start,16:36 PAUSED — 跑 12m,产 1 alpha

**`backend/tasks/mining_tasks.py:_run_cascade_phase` 设计**:每个 phase 跑 `CASCADE_T1_ROUNDS=10 / CASCADE_T2_ROUNDS=10 / CASCADE_T3_ROUNDS=5` round 后,内部 set `task.status=PAUSED`(line 1378-1388 的 finalize 路径根据 task 状态镜像到 run),**没有 cron / supervisor / 自动 resume**。需要人手 `POST /mining-session/start` 才能继续。

**反 ops 程度**:
- 生产环境意味着"每小时按一次按钮"
- 实测 task 652 历史 5/13 12:32 start → 5/14 17:14 last alpha,中间隐含约 8-10 次手动 resume
- 学界 / 工业竞品(RD-Agent / Alpha-GPT / QuantaAlpha)无一采用此设计(§12)

**修复方向**:与 flat search 切换合并(§13)— flat path 用 `while task.status not in ('PAUSED','STOPPED')` 无限循环,只在用户主动 PAUSE 时退出,从根本消除软停。

### 11.5 Bug 3 误判教训 — 别反射性"修 bug"

2026-05-16 调试 P2-C 时一度怀疑 worker `_flag_override_cache` 同步 hook 失效(命名 Bug 3),写了 9 个 ENABLE flag 到 .env 作为 workaround。后来发现:
- task 1325 LLM hypothesis 输出含 "balanced regime"、"momentum pillar nudge" 字眼 — **证明 mining_agent P2-C 注入实际跑了**
- 真问题是 mining_agent 用 loguru logger,celery worker file log 没 sink loguru → log 看不到 ≠ 功能没生效

**.env workaround 副作用**:9 个 default-OFF 单测因 env 全 ON 而失败(`robustness_attempted should be 0 when flag OFF`)— v2 已经撤销 workaround,memory 记 [[no-reflex-flag-cleanup]] 防复发。

**对 plan 的意义**:phase15 §6 测试策略要加一条 — 任何 default-OFF 单测都要显式 monkeypatch settings,不要假设 .env 没全局 override。

---

## 12. 竞品对比矩阵(v2 新增)

本节回答:"AIAC cascade T1/T2/T3 在学术界 / 工业界处于什么位置?"

### 12.1 主流系统架构对比

| 系统 | 年份 | 生成机制 | 调度/分层 | 反馈机制 | 抗 decay | 状态 |
|---|---|---|---|---|---|---|
| **AIAC**(当前)| 2026 | LLM + typed hypothesis | **cascade T1/T2/T3 phase 切换** | self-correct + KB | negative_knowledge + pillar | 生产中 |
| **RD-Agent-Quant**(MSRA, NeurIPS 2025)⭐ | 2025 | LLM 生成假设森林 → Co-STEER DAG 代码演化 | **flat + multi-armed bandit 调度方向**(无 tier) | bandit reward(实际通过率) | 70% 因子精简 | 学术 SOTA,2.5× ARR vs Alpha158 |
| **Alpha-GPT** v1/v2(HKUST, 2023-2025)| 2025 | LLM seed + **GP 邻域演化** | **3 阶段 flat**:Ideation → Impl → Review;4 层 Hierarchical RAG | natural language analyst | 多轮 human-in-loop | EMNLP 2025 Demos / IQC 2024 top-10 |
| **AlphaAgent**(KDD 2025)| 2025 | LLM flat 生成 | **flat + 三正则化**(AST 相似 / 假设对齐 / 复杂度)| 单流回测 | **AST subtree isomorphism 原创度门** | KDD 2025 接收,IR=1.488 |
| **Hubble v2**(arxiv 2604.09601, 2026-04)| 2026 | LLM + DSL flat | **flat + Family-cap top-k=2** | dual-channel RAG | **negative-channel "avoid like" 模板** | 学术,与 AIAC 80% 重合但更严 |
| **QuantaAlpha**(arxiv 2602.07085, 2026-02)| 2026 | LLM + **trajectory-level mutation/crossover** | flat **进化** + 轨迹拓扑 | trajectory replay | semantic consistency + crowding 控制 | 学术新作 |
| **Chain-of-Alpha**(arxiv 2508.06312)⚠️ | 2025 | **dual-chain**:Generation + Optimization | 两链迭代 generate→evaluate→refine | backtest + prior knowledge | — | **已撤稿**(审稿争议) |
| **AlphaEvolve** | — | **GP + 参数学习 + 矩阵运算** | flat | GP fitness | 内置 | 非 LLM 流派 |
| **AlphaGen**(2023)| 2023 | **DRL** formulaic mining | 单 agent flat | combination model reward | RL exploration | 早期 baseline |
| **Navigate Alpha Jungle**(arxiv 2505.11122)| 2025 | LLM + **MCTS** | tree search | UCB rollout | tree pruning | parallelizability 弱 |
| **AlphaSAGE** | 2025 | **GFlowNet** | flow-based sampling | flow consistency | inherent diversity | 实验性 |
| **Citadel/Renaissance/Two Sigma**(工业)| — | **未公开使用 LLM 做 alpha generation** | — | — | — | CTO 立场:LLM 仅做 research assistant |

### 12.2 AIAC cascade 在矩阵里的位置 — 异类

| 维度 | AIAC cascade | 主流竞品 |
|---|---|---|
| 分层切换 | **T1→T2→T3 phase 机械切换**(基于 round budget + MIN_TIER_SEED_COUNT 门)| 全部 flat;调度靠 bandit / GP / MCTS / trajectory mutation |
| wrapper 处理 | T2 phase 才加 wrapper("后补"二等公民)| LLM 一次生成完整 alpha(含 wrapper),不分阶段 |
| hypothesis 与 tier 关系 | **正交两套系统**(typed hypothesis 不知道自己被哪个 tier 处理)| **统一**:假设森林 + bandit 选下一个挖什么(RD-Agent)|
| 软停机制 | run 跑一段就 mark PAUSED 等人手 resume | 持续后台跑(RD-Agent 单实验 < $10 全自动)|
| 学界先例 | **0**(没有任何学术论文采用 T1/T2/T3 cascade)| 假设森林 / dual-chain / trajectory mutation 都有论文支撑 |

### 12.3 反 cascade 硬证据

- **Chain-of-Alpha 因 dual-chain 设计争议被撤稿** — 学界对"两阶段流水线"持保留。AIAC T1/T2/T3 三阶段比 dual-chain 更激进。
- **RD-Agent 用 22-26 因子达到 14.21% ARR**(Alpha158 158 因子 5.70% ARR)— 验证 **flat + 假设驱动 + 精简** 优于 **分层 + 暴力穷举**。
- **AIAC task 652 实测**:7/7 derived alpha 来自 2 个 parent 同源失败 — cascade T2 wrapper sweep 是盲目穷举的工程实证。

### 12.4 顶级竞品共性(reference architecture)

RD-Agent + AlphaAgent + Hubble v2 共同特征:

1. **假设作为一等公民驱动调度**:RD-Agent 用 hypothesis forest + bandit 决定下一个挖什么。AIAC typed hypothesis 已有但被 cascade tier 架空。
2. **flat 生成 + 演化优化**:LLM 一次生成完整 alpha(含 wrapper),优化用 GP / trajectory mutation / AST 子树,不分 tier phase。
3. **反 decay 用正则化 / 拥挤防御**:三正则化 / family-cap / crowding 控制。AIAC 有 pillar 分类但无 hard cap。
4. **持续运行无需人手 resume**:RD-Agent / QuantaAlpha 跑 trajectory 自动闭环。
5. **单次实验成本 < $10**(RD-Agent 公开数据)。

### 12.5 工业实践 reality check

- **Citadel CTO 明确反对 PM 把判断外包给 LLM** — LLM 仅做 research assistant
- **JPMorgan LLM Suite** 覆盖 20 万员工,**未公开用于 alpha generation**
- **Renaissance / Two Sigma** 无 LLM 内部使用公开披露
- **学术与工业 gap 明显** — 学术全跑 flat + LLM,工业 conservative

**对 AIAC 的意义**:走学术 SOTA 路径(flat + hypothesis-driven),但保留 human review gate(P3 ops console 已有),既能 keep up with research frontier 又有工业一致的安全网。

---

## 13. flat search 完整设计(v2 新增,R1b reference 技术路径)

> **范围声明**:本节是 R1b(Plan v5 Q3 2026 主循环改造)的 reference 技术路径设计。**当前不实施**,等 Phase 0 R1a 启用 + 2 周观察期 + AttributionType 分布数据收集到位后,在 R1b kickoff 时启动。本节给出 file:line 级实施细节作为提前 lock 设计 + risk 评估,**不承诺时间表**。

### 13.1 设计哲学反转

| 维度 | 当前(cascade tier)| 提议(flat search)|
|---|---|---|
| **挖矿主循环** | 外层 cycle `T1→T2→T3`,每个 phase 跑 N round 切换 | 单一无限循环,靠 hypothesis state 驱动 |
| **alpha 生成层级** | T1 生成"骨架",T2 加 wrapper,T3 组合 | LLM 一次生成完整 alpha(含 wrapper),无层级 |
| **下一步挖什么** | cascade_phase 决定(机械切换)| `hypothesis.thesis_score + ACTIVE 状态 + bandit dataset` 多信号融合 |
| **失败应对** | T2 wrapper 穷举尝试 | LLM 看 failed_tests + pitfall KB 决定改什么 |
| **概念基础** | 工程师后验给信号分层 | LLM 是单一生成器,tier 概念外置 |

### 13.2 保留 vs 删除

#### ✅ 保留(不动)
- `factor_tier` column(alpha 评分多档:1/2/3 仍有意义,作为"alpha 复杂度标签")
- `tier_thresholds`(每档 sharpe_min/fitness_min — 评分阈值不变)
- `alpha_routing.py`(pass/optimize/fail/provisional 多档路由 — 评分内部分类)
- typed hypothesis pipeline(B3/B4/pillar/triggered — 完全不动)
- evaluate node 全部 stamp(含 Bug B fix)
- ops dashboard / pillar service / regime / negative_knowledge
- BRAIN integration、bandit selector、composite_fields、pre_simulate_filter

#### ❌ 删除
- `_run_cascade_phase` (`backend/tasks/mining_tasks.py:921-1175`) — phase 内部循环
- cascade 主循环 (`mining_tasks.py:1180-1399`) — T1→T2→T3 切换 + cascade_round_idx 累加
- `cascade_phase` / `cascade_round_idx` 字段使用(标记 deprecated,migration 不删 — 与 phase15 §3 Revision D 合并)
- `CASCADE_T1_ROUNDS / CASCADE_T2_ROUNDS / CASCADE_T3_ROUNDS` settings
- `MIN_TIER_SEED_COUNT`(不再需要"5 个 PASS 才进 T2"门)
- `agent_mode in (AUTONOMOUS_TIER1, AUTONOMOUS_TIER2, AUTONOMOUS_TIER3)` 的 cascade 入口(保留 mode 字符串作为 LLM prompt 偏好提示,但内部走相同 flat path)

### 13.3 新增 `_run_flat_iteration`(替换 cascade loop)

```python
# backend/tasks/mining_tasks.py(新增)
async def _run_flat_iteration(
    db, task, run, brain, mining_agent, operators,
    *, lock_key, lock_token,
) -> dict:
    """Flat mining: 单一无限循环,hypothesis-driven。
    Drop tier phase switching. Each iteration:
      1. score active hypotheses by (thesis_score, recency, pass_rate)
      2. pick top-N hyps OR generate new hyps if pool exhausted
      3. for each hyp: bandit-pick dataset, call LLM (含 wrapper),
         evaluate, write back to hyp.alpha_count/pass_count
      4. abandon hyps with alpha_count>=5 and pass_count=0
      5. exit on task.status PAUSE/STOP signal
    """
    iteration = 0
    total_alphas = 0
    while True:
        await db.refresh(task)
        if task.status in ("PAUSED", "STOPPED"):
            break
        if not _verify_cascade_ownership(lock_key, lock_token, where="flat iter"):
            break

        # 1. Score + pick hypothesis (typed pipeline)
        hyp = await _pick_next_hypothesis(db, task)
        if hyp is None:
            # generate new hyp via LLM
            hyp = await mining_agent.generate_new_hypothesis(task)
            if hyp is None:
                logger.info(f"[flat] no more hyps to explore, exiting")
                break

        # 2. bandit-pick dataset (existing logic)
        dataset_id = await _bandit_pick_dataset(db, task, hyp)

        # 3. run single round (existing run_one_round_inline, 但不传 tier)
        result = await _run_one_round_inline(
            db, task, run, brain, mining_agent, operators,
            dataset_id=dataset_id,
            hypothesis_id=hyp.id,
            # 不传 tier — 让 _evaluate_single_alpha 用 alpha.factor_tier 兜底
        )
        total_alphas += len(result.get("all_alphas", []))

        # 4. abandon under-performing hyps
        await _maybe_abandon_hypothesis(db, hyp)

        # 5. heartbeat
        await _stamp_heartbeat(result)
        iteration += 1
        logger.info(
            f"[flat] iter={iteration} hyp={hyp.id} pillar={hyp.pillar} "
            f"alphas+={len(result.get('all_alphas', []))} total={total_alphas}"
        )

    return {
        "success": True,
        "mode": "FLAT_CONTINUOUS",
        "alphas_mined": total_alphas,
        "iterations": iteration,
    }
```

辅助函数(新增):

```python
async def _pick_next_hypothesis(db, task) -> Optional[Hypothesis]:
    """Score active hyps, return top-1.
    Scoring: thesis_score DESC, then (alpha_count<5 OR pass_count>=1) DESC,
    then created_at DESC. Returns None if no ACTIVE hyp.
    """
    stmt = (
        select(Hypothesis)
        .where(Hypothesis.region == task.region)
        .where(Hypothesis.status == "ACTIVE")
        .where(Hypothesis.is_active == True)
        .order_by(
            desc(coalesce(Hypothesis.thesis_score, 0)),
            desc(Hypothesis.created_at),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _maybe_abandon_hypothesis(db, hyp):
    """Abandon if alpha_count>=5 and pass_count=0 (5 chance no PASS = dead)."""
    if hyp.alpha_count >= 5 and hyp.pass_count == 0:
        hyp.status = "ABANDONED"
        hyp.abandon_reason = f"5 attempts, 0 PASS"
        await db.commit()
        logger.info(f"[flat] abandoned hyp {hyp.id} (alpha_count={hyp.alpha_count})")
```

### 13.4 入口替换

```python
# backend/tasks/mining_tasks.py:run_mining_task(改造)
if task.mining_mode == "FLAT_CONTINUOUS":
    return await _run_flat_iteration(...)
elif task.mining_mode == "CONTINUOUS_CASCADE":
    # 旧 cascade 路径保留(已有 task 兼容)
    return await _run_cascade_legacy(...)
else:
    # AUTONOMOUS_TIER1/2/3 也走 flat(mode 字符串仅作 LLM prompt 提示)
    return await _run_flat_iteration(...)
```

### 13.5 配置层

```python
# backend/config.py(改造)
# ❌ 删
# CASCADE_T1_ROUNDS / CASCADE_T2_ROUNDS / CASCADE_T3_ROUNDS / MIN_TIER_SEED_COUNT

# ✅ 加
FLAT_ABANDON_AFTER_FAILS: int = 5  # hyp.alpha_count >= N 且 pass=0 → abandon
FLAT_TOP_K_HYPS_PER_ITER: int = 1  # 每 iter pick 几个 hyp (1=serial, >1=parallel)
FLAT_NEW_HYP_WHEN_EMPTY: bool = True  # ACTIVE 池空时让 LLM 生成新 hyp
```

### 13.6 数据迁移(与 phase15 §3 Revision 合并)

- `cascade_phase / cascade_round_idx` 字段在 phase15 Revision D 删(本设计不另加 migration)
- 新 task 默认 `mining_mode="FLAT_CONTINUOUS"`(由 phase15 Revision B 回填脚本扩展处理)
- task_service.start_session 创建新 task 时,`mining_mode` 字段在 phase15 Revision C 切读时一并改 default
- 老 task(`mining_mode="CONTINUOUS_CASCADE"`)仍走旧路径,新 task 走 flat — 双轨期与 phase15 灰度窗口一致

### 13.7 T2 wrapper sweep 替换为 LLM mutation

```python
# backend/agents/mining_agent.py(新增,替代 T2 暴力穷举)
async def llm_mutate_alpha(
    self, alpha: AlphaCandidate, *, parent_hypothesis: Hypothesis
) -> List[AlphaCandidate]:
    """Replace T2 sweep. Given a PROVISIONAL alpha + its failed_tests,
    let LLM propose 2-3 targeted wrapper additions (not all 5 group_*).
    """
    failed_tests = (alpha.metrics or {}).get("_failed_tests", [])
    brain_failed = (alpha.metrics or {}).get("_brain_failed_checks", [])

    prompt = build_mutation_prompt(
        original=alpha,
        failed_tests=failed_tests,
        brain_failed=brain_failed,
        pillar=parent_hypothesis.pillar,
        pitfalls=await self._fetch_pitfalls(top_k=3),  # P2-D nudge
    )
    response = await self.llm.chat(prompt, max_tokens=2000)
    mutations = parse_mutations(response)  # 2-3 wrappers, each with reason
    return mutations
```

### 13.8 cascade 与 flat 行为对比(基于 task 652 数据外推)

| 指标 | cascade 现状(5/16) | flat 预期 |
|---|---|---|
| BRAIN sim/天 | 161(其中 ~50% 浪费在 T2 wrapper sweep)| ~100(没 sweep 浪费)|
| 新增 alpha/小时 | ~7(cascade 软停后人手 resume)| ~10-15(无停机)|
| 7 derived alpha 同源率 | 5/7 来自同 1 parent(T2 穷举)| LLM 引导散开到不同 parent |
| PASS rate(90 天观察期)| 0/37 = 0% | 待测,目标 >5% |
| 工程 ops 负担 | 每小时手动 resume | 0(持续无限跑)|

### 13.9 风险矩阵

| 风险 | 缓解 |
|---|---|
| flat 模式下 LLM "贪心"挖同一 hyp 不切换 | `_pick_next_hypothesis` 加 round-robin 因子或 epsilon-greedy 5% 选低 score hyp |
| typed hypothesis 池子可能瞬间被掏空 | `FLAT_NEW_HYP_WHEN_EMPTY=True` 让 LLM 生成新 hyp 时降低 throttle |
| 已有 cascade task 接口兼容性 | `mining_mode="CONTINUOUS_CASCADE"` 路径保留,新建走 FLAT |
| 老的 30+ 集成测试覆盖 cascade | 重写一份 flat 测试套,cascade 测试标 `@pytest.mark.legacy_cascade` |
| AUTONOMOUS_TIER2/3 agent_mode 现有 task 行为变化 | 内部走 flat 但 LLM prompt 加 "你正在挖 tier=2 alpha(base PASS seed 基础上加 wrapper)" 文字提示,保持心智模型 |
| flat 跑量没产生 PASS 比 cascade 更糟 | 灰度 1 region 跑 2 周对照,若 PASS rate < cascade → rollback `mining_mode` default 回 CASCADE |

### 13.10 实施分 4 phase(可单独 ship + rollback)

| Phase | 工作量 | 可独立部署?|
|---|---|---|
| F1: 新建 `_run_flat_iteration` + `mining_mode="FLAT_CONTINUOUS"` 路径(双轨)| 2-3 人日 | ✓ 老 task 继续走 cascade |
| F2: `start_session` 默认创建 FLAT mode + 新 alembic comment | 0.5 人日 | ✓ 老 cascade task 继续 |
| F3: 把 T2 wrapper sweep 替换为 `llm_mutate_alpha` | 1-2 人日 | ✓ 仅影响新 task 的 mutation 行为 |
| F4: 删 cascade legacy 代码 + CASCADE_T*_ROUNDS settings | 1 人日 | ⚠️ 老 task 不能 resume,需要先 stop |

**flat search 合计**:4.5-6.5 人日 + 灰度窗口(2-4 周)

### 13.11 验证 criteria(gate before ship F4)

- flat task 跑 24h 内产出 >= 50 个 alpha(cascade 当前 ~30/天)
- flat task 至少 1 个 alpha can_submit=True(cascade 当前 0/37)
- hypothesis.pass_count > 0 的 hyp 占 ACTIVE 总数 >= 10%(cascade 当前 1/9)
- 没有 hyp 卡在 alpha_count >= 10 且 pass=0(abandon 机制 work)

### 13.12 flat search 与 phase15 时间表合并

```
Phase 0 (1-2 周):
  - Bug B fix ship (✅ 已 commit a425937 2026-05-16)
  - R4 flip ENABLE_NEGATIVE_KNOWLEDGE_NUDGE (✅ 已 ON)
  - R1a 接入 enhance_existing_node_evaluate hook (新增 2 人日)
  - 2 周观察 AttributionType 分布
  ↓
Phase 1 (2-3 周, 6-9 人日):
  - R2 + R3 + R4' 落地(phase15 v1 §0 剩余 GO 闸门)
  ↓
Phase 1.5 (2.5 周, 10-12 人日):
  - phase15 Revision A → B → C(schema 收敛)
  - Revision D 推到 Phase 3 末
  ↓
Phase 2 (2 周, 7-9 人日):
  - R5 + R6 + R7 + R10
  ↓
Phase 3 (Q3 2026, 4-6 周):
  - flat search F1 → F2 → F3 → F4(本节 §13)
  - R1b 全 Pipeline 激活(借助 flat 切换)
  - R8 / R9
  - phase15 Revision D 删旧列(与 F4 合并)
```

---

## 14. R1a 启用细化(v2 新增,Phase 0 主菜)

### 14.1 目标

- 把 `backend/agents/core/integration.py:342-407` 的 `enhance_existing_node_evaluate()` hook 接入 `backend/agents/graph/nodes/evaluation.py:node_evaluate` 末尾
- 让每个跑完 evaluate 的 alpha 都被 hook 处理,生成 `HypothesisFeedback` 对象 + `AttributionType` 分类,写入 `agents/core/EvolvingKnowledge`
- 2 周观察期收集 100+ 次触发数据,反证 phase15 §10 未决问题

### 14.2 真实 signature(2026-05-17 verify)

读 `backend/agents/core/integration.py:342-407` 实测 signature(**与 v2 第一稿假设不符**,修订):

```python
def enhance_existing_node_evaluate(    # ⚠️ 同步函数,不是 async
    alpha,                              # Existing Alpha model (有 .expression / .quality_status / .validation_error)
    sim_result: Dict[str, Any],         # {"sharpe": float, "fitness": float, ...}
    hypothesis_dict: Dict[str, Any],    # {"statement": "..."}
    trace: Optional[ExperimentTrace] = None
) -> HypothesisFeedback:                # ⚠️ 返回 feedback 对象,自身不持久化
```

**v2 第一稿错误清单**(已修):
1. ❌ 假设 `async` → 实测同步,**不要 `await`**
2. ❌ 参数 `ctx=_ctx, regime=..., hypothesis_id=...` → 实测 `sim_result + hypothesis_dict + trace`
3. ❌ 假设 hook 自己写 `EvolvingKnowledge` / `KnowledgeRule` → 实测**只 return `HypothesisFeedback`,caller 自己持久化**
4. ❌ §14.4 KPI 写 "EvolvingKnowledge 容量 >= 50 rule" → 实测 hook 不写 EvolvingKnowledge,改写 `alpha.metrics["_r1a_attribution"]` 或 `trace_step.output_data`

### 14.3 修订后接入代码(`backend/agents/graph/nodes/evaluation.py:2554` 前)

```python
# R1a hook (P3 Phase 0): 采集 AttributionType 分布到 alpha.metrics + trace_update。
# Bug B fix (commit a425937) 保证 flip-retry alpha 也走 _evaluate_single_alpha,
# 这里循环 updated_alphas 时 main + flip 两条路径都被捕获,采集无偏。
# Doc: phase15_task_schema_refactor_plan §14.
from backend.agents.core.integration import enhance_existing_node_evaluate

r1a_attribution_counts = {"hypothesis": 0, "implementation": 0, "both": 0, "unknown": 0}
r1a_hook_failures = 0

# Build hypothesis_dict from MiningState. state.current_hypothesis_statement
# is the typed-path Hypothesis.statement (Phase 2 B3, LEVEL>=2). For legacy
# variant=0 path, fall back to alpha.hypothesis (per-alpha narrative).
_hyp_statement = getattr(state, "current_hypothesis_statement", "") or ""

for alpha in updated_alphas:
    # Skip PENDING / never-simulated — hook needs a concrete result to attribute
    if alpha.quality_status not in ("PASS", "PASS_PROVISIONAL", "OPTIMIZE", "FAIL"):
        continue
    try:
        _sim_result = {
            "sharpe": (alpha.metrics or {}).get("sharpe"),
            "fitness": (alpha.metrics or {}).get("fitness"),
        }
        _hyp_dict = {
            "statement": _hyp_statement or (alpha.hypothesis or ""),
        }
        feedback = enhance_existing_node_evaluate(  # 同步 — 不要 await
            alpha=alpha,
            sim_result=_sim_result,
            hypothesis_dict=_hyp_dict,
            trace=None,  # Phase 0 暂不接 trace,R6 (Trace MCTS) 时再补
        )
        # Persist to alpha.metrics — Bug B fix verified persistence path
        # (re-bind dict before mutating, V-26.79 defence)
        if isinstance(alpha.metrics, dict):
            alpha.metrics = dict(alpha.metrics)
        else:
            alpha.metrics = {}
        alpha.metrics["_r1a_attribution"] = feedback.attribution.value
        alpha.metrics["_r1a_hyp_supported"] = bool(feedback.hypothesis_supported)
        r1a_attribution_counts[feedback.attribution.value] += 1
    except Exception as _r1a_ex:
        r1a_hook_failures += 1
        logger.warning(
            f"[node_evaluate] R1a hook failed for {alpha.alpha_id or '?'} "
            f"(non-fatal): {_r1a_ex}"
        )

# Expose counters to trace_steps.output_data for ops dashboard / KPI tracking
trace_update["r1a_attribution_counts"] = r1a_attribution_counts
trace_update["r1a_hook_failures"] = r1a_hook_failures

return {
    "pending_alphas": updated_alphas,
    **trace_update
}
```

**注意点**:
- hook 同步 — 加 `await` 会 raise `TypeError`
- hook 输出 4 个字符串值:`hypothesis / implementation / both / unknown`(全小写,见 `feedback.py:17-22`)
- `alpha.metrics` 持久化路径已被 Bug B fix 验证(commit `a425937` 让 flip-retry alpha 也走 line 646 spread,SQLAlchemy 能 detect)— 这里照同样套路 re-bind dict
- `state.current_hypothesis_statement` 是 typed path B3 持久化的字段,**legacy variant=0 path 可能为空** → fallback 到 `alpha.hypothesis`(Alpha 模型 text 字段)

### 14.4 AttributionType 数据采集策略

`agents/core/feedback.py:17-22` `AttributionType` enum:
- `"hypothesis"` — 失败归因于假设方向错误(LLM 提的 thesis 不对)
- `"implementation"` — 失败归因于代码实现 / wrapper 选择(thesis 对但 expression 错)
- `"both"` — 两者都有
- `"unknown"` — 无法判定

`enhance_existing_node_evaluate` 内部:
1. 调 `quick_alignment_check(hypothesis_dict, alpha.expression, [])` 看 thesis 与 expression 是否对齐
2. 调 `determine_attribution_heuristic(result_dict, alignment_issues, validation_error)` 推断 attribution
3. 构造 `HypothesisFeedback(attribution=AttributionType(...), ...)` 返回

**持久化由 caller 完成**:本节代码把 `feedback.attribution.value` 写到 `alpha.metrics["_r1a_attribution"]`,同时聚合到 trace_step output 的 `r1a_attribution_counts` dict。

下游消费(Phase 0 观察期):
- SQL `SELECT (metrics->>'_r1a_attribution') AS attr, COUNT(*) FROM alphas WHERE created_at > now() - interval '14 day' GROUP BY 1` 反证 attribution 分布
- Ops dashboard `/api/v1/ops/r1a-attribution`(Phase 0 末追加 endpoint,可选)
- trace_step EVALUATE 节点 output_data 已含 `r1a_attribution_counts`,免单建 endpoint

### 14.5 2 周观察期 KPI(修订)

| 指标 | 目标 | 来源 |
|---|---|---|
| hook 触发次数 | >= 100 | `SELECT COUNT(*) FROM alphas WHERE metrics ? '_r1a_attribution'` |
| `alpha.metrics._r1a_attribution` 非 NULL 比例 | >= 95% | 同上 / 总 alpha 数 |
| AttributionType 分布(non-`unknown` 比例)| >= 70% | enum value counts |
| `hypothesis` vs `implementation` 比例 | 任意,但要有数据 | 反证 R2 arm 集设计 |
| hook failure 次数 | < 10 | trace_steps 聚合 `r1a_hook_failures` |
| 无 production 路径 crash | OK | log warning grep + alpha 持久化数不掉 |

(原 v2 第一稿 KPI 中"EvolvingKnowledge 容量 >= 50 rule" **删除** — hook 不写 EvolvingKnowledge,改 `alpha.metrics._r1a_attribution` 比例覆盖。)

### 14.6 实施步骤(2 人日,与 v2 第一稿一致)

| 步骤 | 工作量 |
|---|---|
| 1. Edit `evaluation.py:2554` 加 hook 调用 + try/except 守护(§14.3 代码块直接 copy)| 0.5 人日 |
| 2. 加 unit test `test_node_evaluate_r1a_hook.py`:mock alpha + 验证 hook 被调 + `alpha.metrics["_r1a_attribution"]` 写入正确 enum value | 0.5 人日 |
| 3. 加 integration test:跑一个 mock evaluate 流程,断言 trace_step output 含 `r1a_attribution_counts` 4 个 key 累加正确 | 0.5 人日 |
| 4. 跑量 1 个 mining task(TIER1 smoke,max_iter=2)验证 hook 在生产路径 fire(不爆 warning,alpha.metrics 字段持久化)| 0.5 人日 |
| **合计** | **2 人日** |

### 14.7 回滚预案

- hook 加 `try/except` 守护,任何异常 → log warning + counter +1 + 继续 evaluate(不 propagate)
- 加 feature flag `ENABLE_R1A_HOOK`(默认 True 上线后,有问题 flip 到 False;flag default OFF 时跳过整个 for-loop)
- `alpha.metrics["_r1a_attribution"]` 是新字段,删除不影响主路径
- trace_update 的 `r1a_attribution_counts` 是新 key,下游 ops dashboard 读不到只是 NULL,不 break

### 14.8 与 phase15 闸门关系

- R1a ship + 100 次触发 → phase15 §0 闸门 2/4 → 4/4 满足
- AttributionType 分布数据 → phase15 §10 未决问题 1-2(generation_strategy arm 集 / runtime_state 字段集)有数据反证
- 此后 phase15 Revision A → B → C → D 可顺次启动,**有数据驱动决策**

### 14.9 v2 修订总结(2026-05-17)

读 `integration.py:342-407` 实测 signature 后修订 §14.2-14.5:
- 删除"async / await"假设(同步函数)
- 删除"hook 自动写 EvolvingKnowledge"假设(只返回 feedback)
- 增加"caller 自持久化到 `alpha.metrics["_r1a_attribution"]`"具体路径
- KPI 改成 `alpha.metrics` 字段查询,不依赖 EvolvingKnowledge row count
- §14.3 接入代码块从"概念示意"升级为"可直接 Edit 上线"

---

## 15. v2 版本变更日志

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-05-16 | v1 | 初版 — phase15 task schema 重构 4 步 Alembic plan |
| 2026-05-17 | v2 | 整合 2026-05-16 实测反馈:Bug B fix / task 652 实测 / R1a 启用紧迫性 / cascade 软停反思;新增竞品对比矩阵(§12);新增 flat search 完整设计(§13);新增 R1a 启用细化(§14);更新 §9 决策记录 4 条新决策;§0 加 GO 闸门实测核查 warning |
| 2026-05-17 | v2.1 | 读 `integration.py:342-407` verify `enhance_existing_node_evaluate` signature,修订 §14.2-14.5:删 async 假设、删 EvolvingKnowledge 自动持久化假设、增加 caller 自持久化到 `alpha.metrics["_r1a_attribution"]` 的具体代码、KPI 改成 alpha.metrics 字段查询。§14.3 接入代码块从概念示意升级为可直接 Edit 上线 |

---

*本计划由 [`rd_agent_alpha_gpt_research_2026-05-16.md`](rd_agent_alpha_gpt_research_2026-05-16.md) 调研结论 + 2026-05-16 cascade tier 跑量实测 + 学界竞品对比共同驱动。Phase 1.5 仍是 task schema 重构,Phase 0 R1a 启用是其前置;§13 flat search 设计是 R1b 长期方向的 reference 技术路径,**当前不实施**,等 Phase 0/1/1.5/2 全部 ship 后在 Q3 2026 R1b kickoff 时启动。每个 Revision / Phase / F 步骤独立 PR,可独立回滚。*
