# 四池世界前端重设计方案 (2026-06-07)

> 取证基准:2026-06-07 实时队列(hyp_intent PENDING=764/DONE=605;candidate_queue PENDING_SIM=3533/PENDING_EVAL=24/DONE=1913)+ live `feature_flag_overrides` + 池接线真相图(22 机制)。
> 方法:workflow `wf_151d29d9-1c2`(36 agent / 2.58M tok)——2 grounding(端点清单 + 池接线图)→ 32 逐页取证 → 1 综合。
> 已核实前端真实结构:`frontend/src/components/AppSidebar.jsx`(顶级 5 项 + 「运维监控」SubMenu 27 子项 = 33 个可点条目)、`frontend/src/pages/ops/OpsLayout.jsx`(27 条 /ops 路由)、`frontend/src/App.jsx`(6 条顶级路由)。

---

## 1. 执行摘要

**核心问题**:侧边栏的「运维监控」是 FLAT/ONESHOT 时代逐机制堆出来的 27 个监控页,四池切换后其中近半数读的是**已删机制 / OFF flag 写不出的输出字段** → 恒空死页与运营高频真需求(PENDING_SIM=3533 积压、lease/心跳、supervisor 健康、submit-yield)之间严重错配;首页仪表盘也有 3 个死区(active-tasks 卡 / mock PnL 图 / 硬编码健康灯)。

**死活统计(33 个可点条目)**:

| 类别 | 数量 | 条目 |
|---|---|---|
| **RETIRE(彻底死、无复用价值)** | 4 | LLM 评判(R5)、方向 Bandit、交叉变异(G5)、认知层(R8-v3) |
| **REDESIGN(结构性失真/语义作废,须改写)** | 6 | 仪表盘、总览、Hypothesis 触发器、归因与重试(CoSTEER)、假设森林(G8)、优化闭环(Stage A) |
| **归档候选(Phase 2 可复用,降级非删)** | 2 | 逻辑资产库(G10)、语法校验(G3-v2) |
| **KEEP / KEEP_MINOR(live,仅文案/小修)** | 21 | Alpha 列表、Alpha 详情、危机压力测试、数据管理、配置中心、挖掘池(HG/S/E)、提交积压、自动提交、Feature Flag、LLM 路由、LLM 成本、Alpha 健康度、五支柱平衡、失败模式沉淀、宏观叙事、LLM 算子监控、AST 原创性(G3)、模拟缓存(R9)、容量估算(R11)、因子透镜(R13)、BRAIN 模式 |

> 一句话取舍:**4 个真删 + 6 个真改写 + 2 个降级归档 + 21 个保留小修**,再新建 6 个池原生页填补「队列健康 / 积压告警 / lease-心跳 / supervisor / submit-yield」缺口。

---

## 2. 全量验收表(按严重度排序)

| # | 菜单项 | 路由 | liveness | verdict | 理由 |
|---|---|---|---|---|---|
| 1 | LLM 评判 (R5) | /ops/r5-judge | dead | **RETIRE** | r5_judge 模块 1c-delete 整删 + ENABLE_LLM_JUDGE OFF,r1a_attribution_log R5 列恒 0 |
| 2 | 方向 Bandit | /ops/direction-bandit-monitor | dead | **RETIRE** | ContextualDirectionBandit 已物理删,direction_bandit_log 0 写入者;方向多样性已由 pillar/orthogonality/dataset bandit 承担 |
| 3 | 交叉变异 (G5) | /ops/g5-monitor | dead | **RETIRE** | llm_crossover_alpha 已删 + flag OFF,g5_crossover_log 无 INSERT 源 |
| 4 | 认知层 (R8-v3) | /ops/r8v3-monitor | dead | **RETIRE** | ENABLE_COGNITIVE_LAYER_PROMPT 默认 OFF,_cognitive_layer_used 永不写 |
| 5 | Hypothesis 触发器 | /ops/hypothesis-health | dead | **REDESIGN** | 池内 Hypothesis 恒 PROPOSED(晋升 beat 未部署)+ HypothesisRoundStats 不写 → 全图表结构性恒空;改造为「候选队列漏斗」 |
| 6 | 归因与重试 (CoSTEER) | /ops/costeer | dead | **REDESIGN** | 6 端点中 5 个读死表(r1a/r1b),仅 r8/query-stats 活 → 重构为「知识库与 RAG 健康」 |
| 7 | 假设森林 (G8) | /ops/g8-monitor | dead | **REDESIGN** | eligible_count 恒 0 + _g8_forest_referenced_ids 不写(persister 未传参) → 修后端两处或前端改读 boolean stamp |
| 8 | 优化闭环 (Stage A) | /ops/optimization-cycles | stale | **REDESIGN** | 6h beat 06-04 停(flag OFF);Start 按钮会起与四池抢 sim 槽的孤立任务 → 改写为「优化 sweep 审计 + 手动蓝本」 |
| 9 | 仪表盘 | /dashboard | partial | **REDESIGN** | 5 活源 + 3 死区(active-tasks 卡 / mockPnLData 假图 / 硬编码健康灯);active-tasks 查 RUNNING 而池 task=ACTIVE 永空 |
| 10 | 总览 | /ops/overview | partial | **REDESIGN** | 7 slot 中 regime DEAD(卡 2026-05-19)+ hypothesis-health PARTIAL;删 regime 面板 + 池队列卡替换 |
| 11 | 逻辑资产库 (G10) | /ops/g10-logic | dead | **归档候选** | distill+inject 双 flag OFF,表自 06-03 空;表结构完整,Phase 2 PASS 积累后可重接 → 降级灰显非删码 |
| 12 | 语法校验 (G3-v2) | /ops/g3v2-monitor | dead | **归档候选** | ENABLE_GRAMMAR_VALIDATOR 恒 OFF,_g3v2_* 不写;机制未删,将来激活应并入 G3 原创性页 |
| 13 | Alpha 列表 | /alphas | live | KEEP_MINOR | 5 端点全指活表;隐藏 task_id 过滤 + 加池状态徽标 |
| 14 | Alpha 详情 | /alphas/:id | partial | KEEP_MINOR | 核心全活;修 _crisis_correlations clobber bug + 状态变迁 Tab 池 persister 补写 |
| 15 | 危机压力测试 | /correlation | live | KEEP_MINOR | crisis_corr 快照独立于池;tariff_2025 空窗 + 多区域 pickle 缺失加说明 |
| 16 | 数据管理 | /data | live | KEEP_MINOR | 三 tab 活表;修 getAsyncStatus 缺失 bug(P0) + 去「强制挖掘 FLAT」文案 |
| 17 | 配置中心 | /config | partial | KEEP_MINOR | 5 tab 活 + 2 死 stub(质量阈值/算子偏好硬编码无 onClick);接活端点或改只读 |
| 18 | 挖掘池 (HG/S/E) | /ops/pool-pipeline | live | **KEEP** | 池原生页(2026-06-06 cutover),8 数据源全 live;加积压告警 + expected_workers 后端返回 |
| 19 | 提交积压 | /ops/submit-backlog | live | KEEP_MINOR | 4 端点全活;去 FLAT 文案 + 与池队列深度联动 |
| 20 | 自动提交 (影子) | /ops/auto-submit | live | KEEP_MINOR | ENABLE_AUTO_SUBMIT ON beat 活;菜单名「影子」改动态 mode + 显示 recon_verdict |
| 21 | Feature Flag | /ops/feature-flags | n/a | KEEP_MINOR | 纯控制表;给已删机制 flag 加「池世界已停用」description |
| 22 | LLM 路由 | /ops/llm-routing | live | KEEP_MINOR | resolve_model_for 池每轮调;清 SUGGESTED_NODE_KEYS 幽灵条目(r5/g5/r1b) |
| 23 | LLM 成本 | /ops/cost-monitor | live | KEEP_MINOR | llm_call_log + ENABLE_COST_TELEMETRY ON 池每调用写;去 FLAT 文案 + 与 sim-slot 联动 |
| 24 | Alpha 健康度 | /ops/alpha-health | partial | KEEP_MINOR | beat 今日已跑活;修 BAND_ORDER CRITICAL/UNKNOWN + api.js 反斜杠 bug |
| 25 | 五支柱平衡 | /ops/pillar-balance | live | KEEP_MINOR | hypotheses.pillar 无条件写;legacy_inferred 降级 + source 文案修 |
| 26 | 失败模式沉淀 | /ops/negative-knowledge | partial | KEEP_MINOR | 2/3 源 live(HypothesisRoundStats 源冻结);加 hyp-trigger「暂停」文案 |
| 27 | 宏观叙事 | /ops/macro-narratives | partial | KEEP_MINOR | seed KB 每日活;token-budget 面板恒 0(EXTRACT OFF)加 flag 提示 |
| 28 | LLM 算子监控 | /ops/llm-op-monitor | partial | KEEP_MINOR | beat 每日活;FAILURE_PITFALL 写侧未接通加说明 |
| 29 | AST 原创性 (G3) | /ops/g3-monitor | live | KEEP_MINOR | 两 flag ON 池 HG/E 双路写;去 FLAT 文案 + task_id 跨任务提示 |
| 30 | 模拟缓存 (R9) | /ops/r9-cache | live | KEEP_MINOR | 池 S 持续写 simulation_cache;flag 状态卡改读 DB 权威 + 加队列水位 |
| 31 | 容量估算 (R11) | /ops/r11-capacity | live | KEEP_MINOR | ENABLE_CAPACITY_SCORE ON 池 E 写;加积压感知说明 + 时间窗调整 |
| 32 | 因子透镜 (R13) | /ops/r13-factor-lens | partial | KEEP_MINOR | USA snapshot 有数据,其余 4 区缺 parquet;快照状态提升 + 按区下钻 |
| 33 | BRAIN 模式 | /ops/brain-role | live | KEEP_MINOR | feature_flag 控制表活;running_tasks_count 语义改 pending intent count |

---

## 3. 建议的新信息架构 (IA)

**设计原则**:(a) 删 4 个 dead 页 + 降级 2 个归档候选;(b) 把零散 FLAT 时代 telemetry 按「池生命周期阶段」(生成 HG → 模拟 S → 评估 E → 反馈 → 提交)重组;(c) 顶级聚焦操作者高频需求。

```
仪表盘  /dashboard                         [REDESIGN — 池感知首页]
Alpha 列表  /alphas                        [KEEP]
提交中心  (新顶级二级组)
  ├─ 提交积压  /ops/submit-backlog          [KEEP_MINOR]
  ├─ 自动提交  /ops/auto-submit             [KEEP_MINOR]
  └─ 优化 sweep 审计  /ops/optimization-cycles [REDESIGN]
数据管理  /data                            [KEEP_MINOR]
危机压力测试  /correlation                  [KEEP_MINOR]

运维监控  /ops  (SubMenu,按池阶段重组)
  ── 池总览 ──
  ├─ 总览  /ops/overview                   [REDESIGN — 删 regime + 池队列卡]
  ├─ 挖掘池 (HG/S/E)  /ops/pool-pipeline    [KEEP — 池原生主监控]
  ├─ 队列健康  /ops/pool-queue             [新建 §4.1]
  ├─ 工作器与心跳  /ops/pool-workers        [新建 §4.2]
  ── 生成阶段 (HG) ──
  ├─ 五支柱平衡  /ops/pillar-balance        [KEEP_MINOR]
  ├─ AST 原创性 (G3)  /ops/g3-monitor       [KEEP_MINOR]
  ├─ 假设森林 (G8)  /ops/g8-monitor         [REDESIGN]
  ├─ 宏观叙事  /ops/macro-narratives        [KEEP_MINOR]
  ├─ 失败模式沉淀  /ops/negative-knowledge   [KEEP_MINOR]
  ── 模拟阶段 (S) ──
  ├─ 模拟缓存 (R9)  /ops/r9-cache           [KEEP_MINOR]
  ── 评估阶段 (E) ──
  ├─ Alpha 健康度  /ops/alpha-health        [KEEP_MINOR]
  ├─ 容量估算 (R11)  /ops/r11-capacity      [KEEP_MINOR]
  ├─ 因子透镜 (R13)  /ops/r13-factor-lens   [KEEP_MINOR]
  ── 知识库 & RAG ──
  ├─ 知识库与 RAG  /ops/knowledge          [REDESIGN — 原 /ops/costeer]
  ├─ LLM 算子监控  /ops/llm-op-monitor      [KEEP_MINOR]
  ├─ Hypothesis 池漏斗  /ops/hypothesis-health [REDESIGN]
  ── 系统 & 配置 ──
  ├─ LLM 路由  /ops/llm-routing            [KEEP_MINOR]
  ├─ LLM 成本  /ops/cost-monitor           [KEEP_MINOR]
  ├─ Feature Flag  /ops/feature-flags      [KEEP_MINOR]
  └─ BRAIN 模式  /ops/brain-role           [KEEP_MINOR]
  ── 废弃 / 待 Phase 2 复用 (折叠灰显) ──
  ├─ [归档] 逻辑资产库 (G10)  /ops/g10-logic [stale]
  └─ [归档] 语法校验 (G3-v2)  /ops/g3v2-monitor [stale]

配置中心  /config                          [KEEP_MINOR]

【删除(路由 + 页面 + 菜单条目)】
  ✗ LLM 评判 (R5)  /ops/r5-judge
  ✗ 方向 Bandit  /ops/direction-bandit-monitor
  ✗ 交叉变异 (G5)  /ops/g5-monitor
  ✗ 认知层 (R8-v3)  /ops/r8v3-monitor
```

> 顶级从「仪表盘 / Alpha列表 / 危机 / 数据 / 运维监控 / 配置」改为加入 **提交中心** 二级组——因为这是 execution-limited 系统(67 backlog vs 13 submitted)的真瓶颈,操作者最高频。运维监控内部从「按机制代号(R5/G8/R11)平铺 27 项」改为「按池阶段分段」。

---

## 4. 新页面清单(池世界缺失的)

### 4.1 队列健康与积压告警 `/ops/pool-queue`(P1,最高优先)
- **目的**:把 PENDING_SIM=3533 这种「HG 灌爆 S」的产能错配在一屏可视化告警。
- **关键指标**:candidate_queue 各 stage 计数+趋势;积压比值 `PENDING_SIM / SIMULATING_slots`(>500 红色 Alert);hyp_intent PENDING/DONE/FAILED;throughput_90min(alpha/h);dataset 维度积压热力。
- **数据源**:`GET /ops/pools/status`(已 live)。无需新后端,纯前端聚合。

### 4.2 工作器与心跳健康 `/ops/pool-workers`(P1)
- **目的**:监控 supervisor 常驻 worker 存活、lease 过期(stuck claim)、crash-loop。
- **关键指标**:`pool:workers:alive` vs expected_workers;`stuck_past_lease` 明细;每日 budget 进度条;worker 重启间隔。
- **数据源**:`GET /ops/pools/status`(全 live);expected_workers 建议后端补返回(现前端硬编码 4)。

### 4.3 candidate_queue 钻取 `/ops/pool-queue/:id`(P2)
- **目的**:单条候选全生命周期溯源(expression/verdict/sharpe/lease 历史/关联 hyp_intent_id)。
- **数据源**:candidate_queue + alpha_failures(candidate_queue_id) + alphas。需新增 `GET /ops/pools/candidate/{id}`。

### 4.4 hyp_intent 检视 `/ops/hypothesis-health`(P1,复用旧路由 REDESIGN)
- **目的**:替换结构性恒空的「Hypothesis 触发器」,展示池真实假设漏斗。
- **关键指标**:hyp_intent status 漏斗(PENDING 764 → CLAIMED → DONE 605 → FAILED 1);Hypothesis.pillar 分布;status 分布(当前全 PROPOSED,cognitive reconcile beat 上线后出现 ACTIVE/PROMOTED)。
- **数据源**:`GET /ops/pools/status` + hypotheses.pillar。

### 4.5 submit-yield 仪表 `/ops/submit-yield`(P2,待 Phase 2)
- **目的**:监控「submit-yield 塌方」——产出→can_submit→实际提交的转化漏斗与每日产率、IS-sharpe 分布(侦测字段卫生退化)。
- **数据源**:alphas 按 date 聚合 + bandit `_submit_yield_label`(commit 10dbdc8 已实现,未部署)。

### 4.6 池认知对账监控 `/ops/cognitive-reconcile`(P2,待 Phase 2)
- **目的**:`ENABLE_POOL_COGNITIVE_RECONCILE` beat 上线后,监控 /15min watermark 回灌生命周期+归因。
- **数据源**:cognitive_reconcile_tasks + hypotheses lifecycle 列 + knowledge_entries 增量。一旦上线,G8/Hypothesis 漏斗/失败模式 hyp-trigger 维度全部自动复活。

---

## 5. 分阶段落地计划

### P0 — 删死页 / 止血(低风险,~1 天)
| 改动 | 文件 |
|---|---|
| 删 4 个 RETIRE 菜单条目(r5-judge/direction-bandit/g5-monitor/r8v3-monitor) | `AppSidebar.jsx`(L73,75,77,80) |
| 删对应 4 条路由 + import | `OpsLayout.jsx` |
| 删 4 个页面文件 | `LLMJudgeMonitor.jsx`/`DirectionBanditMonitor.jsx`/`G5CrossoverMonitor.jsx`/`R8v3Monitor.jsx` |
| 删对应 api 方法 | `api.js` |
| 后端 handler 保留(防书签 404)加 DEPRECATED docstring | `backend/routers/ops.py` |
| 修 P0 bug:getAsyncStatus 缺失 | `api.js` + `DataManagement.jsx`(L365) |
| 修 _crisis_correlations clobber(写局部 metrics) | `backend/agents/graph/nodes/evaluation.py`(L717-726) |
| 修 BAND_ORDER UNKNOWN→CRITICAL + api.js 反斜杠 | `AlphaHealthMonitor.jsx`(L42) + `api.js`(L401) |
| 去 FLAT 文案 | 数据管理/提交积压/AST/成本/自动提交各页 |
| Feature Flag 给已删机制 flag 加「池世界已停用」description | `backend/services/feature_flag_service.py` |
| LLM 路由清幽灵 node_keys | `LLMRoutingConsole.jsx`(L56-67) |

### P1 — 重组 IA + 关键池监控(中风险,~3-5 天)
| 改动 | 文件 |
|---|---|
| 菜单树按 §3 重组(分段 + 提交中心顶级组 + 归档折叠组) | `AppSidebar.jsx`(重写 menuItems) |
| 新建队列健康页(§4.1) | 新 `PoolQueueMonitor.jsx` + 复用 `GET /ops/pools/status` |
| 新建工作器心跳页(§4.2);后端补 expected_workers | 新 `PoolWorkersMonitor.jsx` + `backend/routers/ops.py` |
| 仪表盘改写:删 active-tasks 卡/mock 图/硬编码健康灯 → 池队列卡 + circuit-breaker 健康 + 真实 PnL | `Dashboard.jsx`(L39-46) |
| 总览改写:删 regime 面板 + 池队列卡;beat 网格补池 beat | `OpsOverview.jsx`(L226-242) + `ops_service.py` + `routers/ops.py` |
| Hypothesis 触发器 → hyp_intent 漏斗(§4.4) | 重写 `HypothesisHealthMonitor.jsx` |
| CoSTEER → 知识库与 RAG(保留 r8/query-stats+kb-shape,删 r1a/r1b),菜单 RELABEL | 重写 → `KnowledgeRagMonitor.jsx` |
| 优化闭环 → 优化 sweep 审计(冻结 banner + Start disabled 警告 + 手动蓝本历史) | `OptimizationCyclesMonitor.jsx` |
| G8 修后端两处或前端改读 boolean stamp + Health banner | `backend/agents/pipeline/persister.py` + `G8ForestMonitor.jsx` |
| 2 个归档候选(G10/G3-v2)移入「废弃/待 Phase 2」折叠灰显组 | `AppSidebar.jsx` |

### P2 — Phase 2 反馈环上线后(gated on `ENABLE_POOL_COGNITIVE_RECONCILE` 部署 + commit 10dbdc8 push)
| 改动 | 文件 |
|---|---|
| 新建 submit-yield 仪表(§4.5) | 新 `SubmitYieldMonitor.jsx` + bandit `_submit_yield_label` 聚合端点 |
| 新建池认知对账监控(§4.6) | 新 `CognitiveReconcileMonitor.jsx` + reconcile beat 状态端点 |
| 新建 candidate_queue 钻取页(§4.3) | 新 `PoolCandidateDetail.jsx` + `GET /ops/pools/candidate/{id}` |
| G8/Hypothesis 漏斗/失败模式去掉「暂停」文案(生命周期晋升后复活) | 对应 REDESIGN 页 |
| 失败模式 Source 3 从 HypothesisRoundStats 迁到 hypotheses 失败归因列 | `negative_knowledge_service.py` |
| 评估 G10/G3-v2 是否随 PASS 积累翻 ON → 从归档组提回正式分段 | `AppSidebar.jsx` + 对应页 |

---

## 6. 风险与开放问题(需用户拍板)

1. **陈旧 telemetry 页:彻底删码 vs 降级归档?** 4 个已明确删的(R5/方向Bandit/G5/R8-v3)机制已物理删除,建议删前端+保留后端 handler 防 404。但 G10/G3-v2 机制源码未删仅 flag OFF → 建议降级灰显而非删。
2. **G8 假设森林:修后端补全 vs 前端降级?** A:后端 persister 补传 + 候选含 PROPOSED(真修,触及池写路径需回归);B:前端改读已写的 boolean + banner(零后端风险,语义弱化)。
3. **Hypothesis 生命周期晋升依赖未部署的 cognitive reconcile beat**——多页复活 gated 在 `ENABLE_POOL_COGNITIVE_RECONCILE`(commit 10dbdc8 未 push)。是否本轮一并推进 Phase 2 部署?若不,这些页停在「带缺口 banner 的诚实空态」,P2 阻塞。
4. **优化闭环 Start 按钮**:四池下启动会跑抢 sim 槽的孤立任务。disabled+警告(保留紧急手动)还是直接移除?建议前者。
5. **提交中心提为顶级二级组**——改变操作者肌肉记忆。基于 execution-limited 判断的 IA 重心转移,请确认。
6. **后端 ops handler 删 vs 保留**:删 4 死页后端点保留(防 404)还是同步删?建议保留 + DEPRECATED docstring。
7. **新页所需少量后端补丁**(expected_workers/candidate 钻取/submit-yield 聚合/reconcile 状态)——本轮动后端还是 P1 先用纯前端聚合 `GET /ops/pools/status`、补丁推迟 P2?

---

### Load-bearing 文件
- 侧边栏菜单源:`frontend/src/components/AppSidebar.jsx`(menuItems L33-93)
- Ops 路由表:`frontend/src/pages/ops/OpsLayout.jsx`(27 条 Route L66-116)
- 顶级路由:`frontend/src/App.jsx`(6 条 L23-40)
- 池原生监控页(新页模板,复用 `GET /ops/pools/status` 消费模式):`frontend/src/pages/ops/PoolPipelineMonitor.jsx`
