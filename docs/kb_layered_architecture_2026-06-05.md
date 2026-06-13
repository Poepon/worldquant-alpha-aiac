# 知识库（Knowledge Base）分层架构 — 5 层结构

> 生成日期：2026-06-05
> 方法：8-agent workflow 深读 ~9,900 行 KB 相关代码（6 子系统调查 + 2 对抗性验证），关键冲突由人工 grep + 读码裁定。
> 行号锚点截至 2026-06-05；承重锚点（live 闭环 + 死写者）已逐一核验，其余取自调查 agent 的读码结果。
> 状态图例：🟢 生产 live ｜ 🟡 部分 live / flag-gated（"写 live 读休眠"或反之）｜ 🔴 休眠 / 死代码

---

## 0. 核心规律（TL;DR）

**系统是"执行层重、认知层轻"（execution-heavy, cognition-light）。** 知识库的健康度呈**倒金字塔**：越往"智能"的上层（战略、认知）越休眠，越往"机械"的下层（执行）越 live。所谓"经验自进化知识库"目前**有骨架、无大脑**——真正每轮跑的只是执行层的机械模式读写 + 一条延迟回写闭环。

| 层级 | live 占比（估） | 一句话 |
|------|:---:|------|
| **1 战略层** | 🔴 ~10% | "挖什么 / 往哪挖"几乎不被知识驱动；只有数据集 bandit（flag-gated）和宏观叙事**写侧**在动 |
| **2 战术层** | 🟡 ~45% | RAG 检索 + SUCCESS/FAILURE 模式 + 算子偏好在跑，但**退化**（只 L1/legacy）；多样性/外部导入全休眠 |
| **3 执行层** | 🟢 ~70% | **唯一真正运转的层**——读 RAG、落库、延迟回写、骨架化工具 |
| **4 认知层** | 🔴 ~5% | RD-Agent 自进化 + R8-v3 研究透镜 + G10 逻辑库 + 归因——**整层是脚手架**，flag 全 OFF |
| **5 其他** | 🟡 | API 端点 live，但读的是休眠数据；遥测 / RL 表是死的 |

**单一落库点** = `knowledge_entries` 表（`KnowledgeEntry`，多态 by `entry_type`）。
`compute_pattern_hash = sha256(pattern|region|dataset_id)[:32]` **已冻结**（改即废 `ix_kb_pattern_hash` UNIQUE，须回填全部历史行）。

---

## 1️⃣ 战略层（Strategic）— 挖什么 / 往哪挖 / 经济叙事

> 决定**方向与广度**的长周期知识。这层基本休眠 = 当前挖掘方向几乎不被知识驱动。

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `MacroNarrativeService` + `MACRO_NARRATIVE` 条目 | 经济机理叙事注入 hypothesis | 🟡 **写 live**（10:00 beat 无条件 UPSERT ~11 种子）／**读休眠** | `services/macro_narrative_service.py`；`tasks/macro_narrative_extract.py`；读侧 `agents/graph/nodes/generation.py` node_hypothesis（`if _macro_enabled:`） |
| `bandit_state`（数据集导流 bandit） | discounted Beta-Bernoulli 选高价值数据集 | 🟡 flag-gated（默认 OFF，据记忆 Tier A v1 已部署） | `dataset_selector.py` `_load/_save_bandit_state`；`tasks` `run_dataset_weight_refresh` |
| `region_config` 知识 | 各 region 中性化 / universe 配置经验 | 🟡 种子在库，检索弱 | `agents/knowledge_seed.py` `REGION_OPTIMIZATIONS` |
| `run_enhanced_mining` / `hypothesis_centric_level=3`（假设为驱动） | RD-Agent "假设主导"战略反转 | 🔴 **未接线**（docstring 自承 DORMANT，deferred to Q3） | `agents/core/integration.py` |
| `ENABLE_HYPOTHESIS_FOREST_REUSE`（跨任务假设复用） | 复用历史有效假设树 | 🔴 flag OFF | `config.py`；node_hypothesis |
| `ENABLE_PILLAR_AWARE_SELECTION` / pillar balance | 按 pillar 平衡探索方向 | 🔴 flag OFF | `config.py`；`diversity_tracker.py` `get_pillar_balance` |

---

## 2️⃣ 战术层（Tactical）— 给定方向，用哪些模式 / 套路 / 算子

> 决定**怎么打一手牌**的中层"剧本"。KB 内容主体在此，但检索已退化。

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `RAGService.query` + `hierarchical_rag` | 检索引擎（取 SUCCESS/FAILURE 拼进 prompt） | 🟡 **live 但退化**：只 L1（`dataset_categories_used` 类目重叠）或 legacy | `agents/services/rag_service.py:419`；`agents/hierarchical_rag.py`（L0:411 / L3:487 / L2:947 无 `current_expression` 恒空；L1 打分 :620/:643；pillar 仅 @> 粗过滤 :725） |
| `SUCCESS_PATTERN` / `FAILURE_PITFALL` 知识 | 成功套路 + 失败陷阱"剧本" | 🟡 **读 live**；FAILURE 回写 live，**SUCCESS 每轮从不新增**（见 §6 不对称） | `knowledge_entries` |
| `operator_prefs`（算子偏好） | >80% 失败率算子自动 BAN | 🟢 live（6h beat 重算，`routers/config.py` 读） | `agents/feedback_agent.py` `update_operator_stats` |
| `FIELD_BLACKLIST` | 字段黑名单 | 🟢 随检索 live | `services/knowledge_service.py` `get_field_blacklist` |
| `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` / `ENABLE_ORTHOGONAL_PROMPT_STEERING` | 负面知识 / 正交 prompt 引导 | 🔴 flag OFF | node_hypothesis |
| `DiversityTracker`（指纹去重 + 新颖度） | "别再试同一个 alpha"的战术记忆 | 🔴 **零生产调用者**（死） | `diversity_tracker.py`（仅 benchmark/tests 构造） |
| `ast_distance_logger`（真正的 AST 去重记忆） | 每轮新表达式 vs 历史 AST 距离 | 🔴 `ENABLE_AST_DIVERSITY_DIM` OFF | `ast_distance_logger.py`；`knowledge_extraction.py` `ast_distance` |
| `external_knowledge` / `knowledge_seed`（101 / Alpha158 / Alpha191 / 学术 / openassetpricing） | 外部套路导入 | 🔴 **手动**（无 beat/router，仅脚本 + benchmark；forum 无 mcp_client 恒 0） | `external_knowledge.py`；`agents/knowledge_seed.py` |

---

## 3️⃣ 执行层（Execution）— 真正每轮跑的读写机制 ⭐唯一主要 live 的层

> KB 的"手脚"：落库、检索 IO、持久化、回写、骨架化工具。系统实际靠这一层运转。

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `knowledge_entries` 表 + `compute_pattern_hash`（冻结） | **所有知识唯一落库点**，幂等 UPSERT | 🟢 live | `models/knowledge.py`；`repositories/knowledge_repository.py` `upsert_pattern` |
| `KnowledgeRepository` / `KnowledgeService` / router | 数据访问 + CRUD + HTTP API | 🟢 live | `routers/knowledge.py`（6 端点） |
| `node_rag_query`（读） | 生成子图入口，每轮无条件读 RAG（**不传 `current_expression`**） | 🟢 live·热路径 | `agents/graph/nodes/generation.py:115`；绑定 `workflow.py:433` |
| `_incremental_save_alphas` / `_incremental_save_failures`（持久化单写者） | 落 alphas / alpha_failures + enqueue 回写 | 🟢 live | `agents/pipeline/persister.py:89-94`（默认注入）；`agents/graph/nodes/persistence.py` |
| `refresh_can_submit_for_alpha`（延迟回写） | `update_pattern_brain_status` + `record_failure_pattern` | 🟢 live（滞后一个 refresh，Redis 限速 6/60s） | `tasks/refresh_tasks.py:347`（brain status）/ `:365`（failure pattern） |
| `run_daily_feedback`（23:00 beat） | alpha_failures → FAILURE_PITFALL；**SUCCESS 也只在这 + HITL 产生** | 🟢 live | `agents/feedback_agent.py`；`celery_app.py`（crontab 23:00） |
| `knowledge_extraction` 骨架化工具 | `expression_to_skeleton` / `ast_distance` / 算子链，被检索/评估/去重/originality 大量复用 | 🟢 **重度 live** | `knowledge_extraction.py` |
| `node_evaluate.record_failure_pattern` | 本应每轮写 FAILURE_PITFALL | 🔴 **全路径死** | `evaluation.py:3028`（取 `config["configurable"]["rag_service"]`，全仓零写入）/ `:3088`（调用） |
| `node_save_results.record_success_pattern` | 本应每轮写 SUCCESS_PATTERN | 🔴 **死**（pipeline 从不调 node_save_results；cascade 退役遗留注释） | `persistence.py:888-965` |

---

## 4️⃣ 认知层（Cognitive）— 元推理 / 自归因 / 从经验学习 ⭐整层休眠

> "想清楚自己为什么成 / 败、并把经验抽成规则"的自进化大脑。**这层是整个知识库最名不副实的部分——设计完整、生产零运行。** 这恰是平台对标 Alpha-GPT / RD-Agent 范式的核心卖点。

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| RD-Agent core：`EvolvingKnowledge` / `KnowledgeRule` | If-Then 可迁移规则 + 贝叶斯置信，跨实验累积 | 🔴 纯内存 + pickle，**零生产调用** | `agents/core/knowledge.py` |
| `ExperimentTrace`(DAG) / `AlphaRAGStrategy` | 实验谱系 + 相似假设 / error→fix 检索 + `generate_knowledge` | 🔴 **从不实例化** | `agents/core/trace.py`；`agents/core/evolving_rag.py` |
| `HypothesisFeedback` + `AttributionType`（HYPOTHESIS vs IMPLEMENTATION） | 归因区分"假设错还是实现错"，防污染知识库 | 🟡 仅 `ENABLE_R1A_HOOK`(OFF) 开时戳 `r1a_attribution_log`，且 **trace=None** 不累积 | `agents/core/feedback.py`；唯一入口 `evaluation.py:3181`(`_r1a_on`) / `:3192`(import) / `:3237`(`trace=None`) |
| R8-v3 `cognitive_layer_service`（研究透镜认知层） | 用不同"认知镜头"框定假设生成 + `build_cognitive_layer_block` 注入 | 🔴 `ENABLE_COGNITIVE_LAYER_PROMPT` OFF | `config.py:677`；`agents/prompts/hypothesis.py:284-289`；`agents/prompts/base.py:96` |
| `cognitive_layer_bandit_state` + 周频 `run_cognitive_layer_bandit_update` | 按 `_cognitive_layer_used` PASS/FAIL 学哪个认知镜头最优 | 🔴 flag OFF（bandit 模式无数据） | `models/cognitive_layer_bandit.py`；`celery_app.py:301`；`config.py:678`(`COGNITIVE_LAYER_SELECT_MODE`) |
| G10 `distilled_logic_library`（`ENABLE_G10_LOGIC_DISTILL` / `_INJECT`） | 把历史经验蒸馏成"逻辑库"再注入 prompt | 🔴 两 flag 均 OFF | `config.py:696`(distill) / `:709`(inject)；`base.py:103`(`distilled_logic_block`) |
| `record_failure_tree`（R1b 失败树） | 把失败链结构化成树供 L2 检索 | 🔴 `ENABLE_R1B_FAILURE_TREE` OFF | `knowledge_extraction.py` `record_failure_tree`；`agents/graph/nodes/r1b_loop.py` |
| `feedback_agent.learn_from_round`（LLM 元反思整轮成败） | 聚合一轮成功 / 失败抽成模式 | 🔴 **只 ONESHOT 可达，FLAT 不调** | `feedback_agent.py` `learn_from_round`；ONESHOT 入口 `mining_tasks.py:566` |

---

## 5️⃣ 其他（Other）— 遥测 / 死表 / API 外围

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `r8_query_log` + `r8v3_cognitive_layer_stats` 等 ops 端点 | RAG / 认知层遥测 | 🔴 `ENABLE_R8_QUERY_LOG` OFF（表空，历史 0/304 带表达式）；端点 live 但返回 0 | `models/r8_query_log.py`；`routers/ops.py:2626` |
| `rl_states` / `rl_actions`（RL Q-state/action） | 强化学习探索状态 | 🔴 **死表**：定义 + 导出，零读写，**删除候选** | `models/knowledge.py`（`RLState`/`RLAction`） |
| `ENABLE_RAG_CATEGORY_AB` A/B 评测台 | 检索改动的 PASS-per-sim A/B | 🔴 flag OFF | `scripts/rag_ab_report.py` |
| `ANCHOR_METADATA` 条目 | 跨数据集锚点元数据（**须排除出检索 SQL**，footgun） | 🟡 元数据 | `models/base.py`（`KnowledgeEntryType`） |
| `/api/v1/knowledge/*` + ops 看板 | 人工 CRUD / 监控 | 🟢 API live（但读的多是休眠层数据） | `routers/knowledge.py`；`routers/ops.py` |

---

## 6. 唯一真正运行的 live 闭环（含结构性断裂）

```
每个 FLAT 轮：
 run_mining_task(schedule=FLAT) [mining_tasks.py:420]
   → _run_flat_iteration [:438 / 定义 :1123]
   → run_flat_pipeline_session [:1504]

 ① 读（live·热路径·无 flag 守卫）
    producer 跑 _build_hyp_graph 入口 node_rag_query
    → RAGService.query(dataset_id,region,hyp_id,task_id,rag_ab_arm)  ← 不传 current_expression
    → legacy/L1 取 SUCCESS+FAILURE（capped 800, ORDER BY id DESC, 算子白名单过滤）
    → 拼进 hypothesis prompt  [generation.py:115 → :1068-1078]

 ② 生成/模拟/评估  consumer 跑 node_simulate + node_evaluate
    ⚠️ node_evaluate.record_failure_pattern [evaluation.py:3088] → 死路
       （config 永不带 rag_service，producer.py:418 consumer config 只有 trace_service/run_id）

 ③ 持久化（live·单写者）  persister [persister.py:89-94]
    → _incremental_save_alphas(→alphas) + _incremental_save_failures(→alpha_failures)
    → enqueue refresh_can_submit_for_alpha（Redis 限速 6/60s）

 ④ 延迟回写（真正 KB 写, 滞后）
    refresh_can_submit_for_alpha [refresh_tasks.py:347/365]
    → update_pattern_brain_status（给既有 SUCCESS_PATTERN 盖 BRAIN 裁决）
    + record_failure_pattern（新 FAILURE_PITFALL）

 ⑤ 日频回写  run_daily_feedback（23:00 beat）alpha_failures → FAILURE_PITFALL
    SUCCESS_PATTERN 只由日频 beat + HITL 产生
```

### 三处结构性断裂 / footgun

1. **读写不对称（最关键）**：live FLAT 每轮 **READ** SUCCESS_PATTERN 却**从不 CREATE** —— 战术层"成功剧本"被读但执行层从不为它新增条目。成功池只靠 23:00 beat + HITL 增长。
2. **两处死写者**：`node_evaluate.record_failure_pattern`（`rag_service` 从不注入 config，**全路径死**，已人工核验）+ `node_save_results.record_success_pattern`（pipeline 从不调 node_save_results）。
3. **延迟回写概率性**：`refresh_can_submit_for_alpha` 受 Redis 限速 6/60s，高 PASS 量下部分回写被丢给周期 sweep —— "每 alpha 闭环"是概率性而非保证。
4. **800 行检索窗**：`query()` 候选池 `ORDER BY id DESC LIMIT 800`，KB 增长后老的有效条目被挤出窗口（**当前行数 vs 800 上限无人测过**）。
5. **算子白名单 fail-open**：`_filter_hallucinated` 若 Operator 表加载失败则**不过滤直接返回**。

---

## 7. "点亮"优先级（若决定投入，按 ROI）

1. **执行层补断头**：把 SUCCESS_PATTERN 每轮写接回 pipeline persister（复活死写者）→ 成本最低、立刻消除读写不对称。
2. **认知层 R1a / 归因先 live**：已有 `r1a_attribution_log`，翻 `ENABLE_R1A_HOOK` → 让失败归因真正反哺（但 `trace=None` 仍不累积，需补 trace 注入才完整）。
3. **战略层数据集 bandit 确认 live**：核验 `ENABLE_DATASET_VALUE_BANDIT` 的 live `FeatureFlagOverride` 状态，让"往哪挖"被知识驱动。
4. **删死代码**：`rl_states` / `rl_actions` 表 + `DiversityTracker`（或接线到 generation）。

> ⚠️ **前置判断**：据既有结论，真瓶颈是**广度（新正交数据源）+ 抽干积压**而非 KB 检索深度（见 `docs/competitive_analysis_v3_2026-05-26.md`、`docs/per_function_llm_routing_plan` 系列与记忆 `reference_competitive_analysis_v3_2026_05_26`）。点亮认知层 / 激活 RAG 全层前，先用 `scripts/rag_ab_report.py`（PASS-per-real-sim，arm via `ENABLE_RAG_CATEGORY_AB`）测量 ROI。

---

## 附录 A：关键 flag 默认值（截至 2026-06-05，均在 `backend/config.py`，默认 OFF 除非注明）

| flag | 默认 | 门控的层 / 能力 |
|------|:---:|------|
| `ENABLE_R1A_HOOK` | False | 认知层 — 归因 hook（唯一可达 core/ 入口，且 trace=None） |
| `ENABLE_COGNITIVE_LAYER_PROMPT` | False | 认知层 — R8-v3 研究透镜注入 |
| `COGNITIVE_LAYER_SELECT_MODE` | `round_robin` | 认知层 — 透镜选择（bandit/round_robin/deficit_aware） |
| `ENABLE_G10_LOGIC_DISTILL` / `ENABLE_G10_LOGIC_INJECT` | False / False | 认知层 — 逻辑库蒸馏 / 注入 |
| `ENABLE_R1B_FAILURE_TREE` | False | 认知层 — 失败树写 + L2 检索 |
| `ENABLE_HIERARCHICAL_RAG` | False（**prod 据设计文档 DB-override ON since 2026-05-18，未对 live 表核验**） | 战术层 — 分层检索路由 |
| `ENABLE_R8_QUERY_LOG` | False | 其他 — RAG 遥测（表空） |
| `ENABLE_RAG_CATEGORY_AB` | False | 其他 — 检索 A/B 评测台 |
| `ENABLE_AST_DIVERSITY_DIM` | False | 战术层 — AST 去重记忆 |
| `ENABLE_DATASET_VALUE_BANDIT` | False（据记忆 Tier A v1 已部署） | 战略层 — 数据集导流 |
| `ENABLE_MACRO_NARRATIVE_GUIDANCE` | False | 战略层 — 宏观叙事**读侧**注入 |
| `ENABLE_MACRO_NARRATIVE_EXTRACT` | False | 战略层 — 宏观叙事 LLM 生成（Phase-2；种子写无条件） |
| `ENABLE_PILLAR_AWARE_SELECTION` | False | 战略层 — pillar 平衡 |
| `ENABLE_HYPOTHESIS_FOREST_REUSE` | False | 战略层 — 跨任务假设复用 |
| `WRITE_FIELD_HYPOTHESIS_INSIGHTS` | False | 弃用（V-26.38，无检索路径） |

## 附录 B：验证方法 / 复核入口

- 决定性裁定（node_evaluate 死写者）：`rg '"rag_service"\s*:' backend/` → 零匹配；`producer.py:418` consumer config 不含 rag_service。
- RAG 层活跃度：因 `ENABLE_R8_QUERY_LOG` OFF 无法直接查 `r8_query_log`；改用 `scripts/rag_ab_report.py`。
- 死表确认：`rg 'RLState|RLAction' backend/ --type py`（仅 model 定义 + `__init__` 导出，零读写）。
- 相关记忆：`reference_kb_architecture_dormant_scaffolding_2026_06_05`、`reference_rag_retrieval_dormant_layers`、`reference_competitive_analysis_v3_2026_05_26`、`project_depth_levers_refuted_breadth_is_answer_2026_05_25`。
