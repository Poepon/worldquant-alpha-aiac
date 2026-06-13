# Feature-Flag 可达性普查定稿删除清单 (Task 1)

- 日期:2026-06-13
- 范围:`backend/services/feature_flag_service.py` 的 `SUPPORTED_FLAGS` 白名单(约 70 个 flag)瘦身的权威输入
- 方法:全 `backend/` grep(排除 `tests/`、`__pycache__`)追每个候选 flag 的业务读者 + 四池可达性(`pool/` 拉起的 worker / celery beat / 活跃 router)
- 性质:只读普查 + 本文档。Task 2-5 以本清单为准。

> ⚠️ 非 `ENABLE_` 前缀 flag(`HYPOTHESIS_CENTRIC_LEVEL` / `FLAT_CROSS_REGION_*` / `GRAMMAR_VALIDATOR_RETRY_MAX` / `TASK_STOP_LOSS_*` / `REGIME_STAGE` / `QLIB_PRESCREEN_MODE` 等)走直读 `_flag_override_cache`,`settings.X` 因 `config.py:__getattribute__` 只认 `ENABLE_` 前缀而读不出 override。本普查对这类 flag 同时核 `settings.<FLAG>`/`getattr(...)` 与 cfg 链。

---

## 1. REMOVE_SET(最终删除的 flag)

共 **11 个**。逐个附零可达性证据。

| # | flag | 类型 | 零可达性证据 |
|---|---|---|---|
| 1 | `ENABLE_DEFAULT_FLAT_SESSION` | 仅白名单条目 | 无业务读者。grep 仅命中 `config.py:817`(默认声明)+ `feature_flag_service.py:463-470`(白名单)。FLAT 入口 `POST /mining-session/start` 默认路由已随 FLAT 退役删除。 |
| 2 | `ENABLE_FLAT_CONTINUOUS` | 仅白名单条目 | 无业务读者。grep 仅命中 `config.py:807,814`(声明+注释)+ `feature_flag_service.py:470,473`(白名单+另一 flag 描述引用)。FLAT session 子系统已删(`b89b732`)。 |
| 3 | `GRAMMAR_VALIDATOR_RETRY_MAX` | 仅白名单条目 | 自标 RESERVED / not wired。grep 命中 `config.py:720`(默认+`# RESERVED`)、`feature_flag_service.py:900,904`、`services/grammar_validator.py:316`(注释:"Kept + tested so the future wire is a..."——纯文档注释,非读取)。无 `settings.GRAMMAR_VALIDATOR_RETRY_MAX` 运行时读取。 |
| 4 | `ENABLE_R1A_HOOK` | 仅白名单条目 | 业务路径退役。grep:`evaluation.py:3059`=注释("core/ attribution shim + r5_judge removed; ENABLE_R1A_HOOK / ENABLE_LLM_JUDGE ...")、`attribution_types.py:13,16`=文档注释、`routers/ops.py:1448`=审计端点 instrumentation(纯展示 flag 值)、`tasks/canary_redflag.py:39`=红旗 canary 监控名单字符串。**无热路径 `settings.ENABLE_R1A_HOOK` 决策读取**。R1a hook shim 随 `agents/core/` 删除(`b89b732`)。 |
| 5 | `ENABLE_LLM_JUDGE` | 仅白名单条目 | R5 judge 已删。grep:`evaluation.py:3059`=注释(同上,确认 r5_judge removed)、`config.py:866,936`=注释+默认、`models/r1a_attribution.py:79`=列注释(历史行兼容)、`routers/ops.py:1420,1449,2708,2727`=审计端点 instrumentation(纯展示 + "Endpoint kept to avoid 404s")。**无热路径决策读取**。 |
| 6 | `ENABLE_G5_CROSSOVER` | 白名单条目 + 连带 `config.py` LLM_ASSISTANT_SENTINEL_FLAGS 成员 | **见 §4 G5 裁决**。生产 G5 producer(写 `g5_pending_offspring` 的 `stash_pending_offspring`)在全 `backend/` 零调用方;`settings.ENABLE_G5_CROSSOVER` 唯一真读点全在 `routers/ops.py`(`g5_crossover_stats` 审计端点 instrumentation,2817/2871 注释自认 "llm_crossover_alpha deleted (1c) + ENABLE_G5_CROSSOVER OFF")+ `config.py:553` 的 `LLM_ASSISTANT_SENTINEL_FLAGS` 列表成员。判 REMOVE。 |
| 7 | `ENABLE_TASK_SCHEMA_V2` | 仅白名单条目 | 运行时无业务读者。grep:唯一 `settings.ENABLE_TASK_SCHEMA_V2` 读点在 `alembic/versions/3b1c4e5d6a78_phase15_b_backfill.py:157`(downgrade 守卫)+ `config.py:966`(默认)。Tier 系统已删、legacy fallback 已删,运行时无 router/service/node 读它。 |
| 8 | `FLAT_CROSS_REGION_QUOTA` | 白名单条目 + 连带孤儿端点/service | 读者 `routers/ops.py:1380` `flat_region_distribution` 端点 + `services/flat_region_quota.py`。**见 §4 孤儿裁决**:该端点服务已删的 FLAT `POST /ops/start-flat-session` 准入流程,前端零引用(grep `frontend/src` 命中 0),四池无调用方。判孤儿 REMOVE。 |
| 9 | `FLAT_CROSS_REGION_ENFORCE` | 白名单条目 + 连带同上 | 读者 `routers/ops.py:1381` 同 `flat_region_distribution`。同 #8 孤儿。判 REMOVE。 |
| 10 | `ENABLE_TASK_STOP_LOSS` | 白名单条目 + 连带孤儿 service | 读者 `services/task_stop_loss_service.py`(R14)。**见 §4**:`evaluate_stop_loss`/`apply_stop_loss_decision` 原由 FLAT loop(`_run_flat_iteration`)调用,该 loop 已删(`b89b732`);四池 worker(`pool/workers.py`)、celery beat(`celery_app.py`)、活跃 router 均无调用方。判孤儿 REMOVE。 |
| 11 | `TASK_STOP_LOSS_PASS_RATE_FLOOR` | 白名单条目 + 连带同上 | 仅被 `task_stop_loss_service.py` 读;随 #10 孤儿。判 REMOVE。 |
| 12 | `TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS` | 白名单条目 + 连带同上 | 仅被 `task_stop_loss_service.py` 读;随 #10 孤儿。判 REMOVE。 |

> 计数订正:REMOVE_SET = **12 个 flag**(上表 #1–#12)。`ENABLE_LLM_ASSISTANT_MODE` **不在** REMOVE_SET —— 见 §4 特判(保留标 dormant)。

---

## 2. 连带删除清单(可达性判死的孤儿代码)

明确区分:**仅删白名单条目(零连带代码)** vs **要连带删端点/service/测试**。

### 2A. 仅删白名单条目 + `config.py` 默认声明(无连带孤儿代码)

这些 flag 无独立子系统,删 `SUPPORTED_FLAGS` 条目 + `config.py` 默认即可。它们在审计/红旗/注释端点的 instrumentation 引用是纯展示,**Task 2 可保留那些审计端点**(它们不读 flag 做决策,只回显 flag 值;删 flag 默认后 `getattr(_stg, "X", False)` 仍回退 False,端点不崩):

| flag | 删白名单 | 删 config.py 默认 | 备注 |
|---|---|---|---|
| `ENABLE_DEFAULT_FLAT_SESSION` | `feature_flag_service.py:463-472` | `config.py:817` | 注意 `ENABLE_FLAT_CONTINUOUS` 描述里有交叉引用文案(`feature_flag_service.py:470`),随 #2 一并清 |
| `ENABLE_FLAT_CONTINUOUS` | `feature_flag_service.py:473-483` | `config.py:807`(+ 注释 814) | — |
| `GRAMMAR_VALIDATOR_RETRY_MAX` | `feature_flag_service.py:904-915` | `config.py:720` | `services/grammar_validator.py:316` 仅注释提及,不必改(或顺手更新注释,非必须) |
| `ENABLE_R1A_HOOK` | `feature_flag_service.py:184-189` | `config.py:734` | `routers/ops.py:1448` / `tasks/canary_redflag.py:39` 是 instrumentation 名单,保留(回退 False);`attribution_types.py` 注释保留 |
| `ENABLE_LLM_JUDGE` | `feature_flag_service.py:538-550` | `config.py:936` | `routers/ops.py:1420/1449/2708/2727` instrumentation 端点保留;`models/r1a_attribution.py:79` 列注释保留 |
| `ENABLE_TASK_SCHEMA_V2` | `feature_flag_service.py:552-562` | `config.py:966` | alembic `3b1c4e5d6a78` downgrade 守卫读 `settings.ENABLE_TASK_SCHEMA_V2`,但 **migration 文件不改**(历史迁移冻结;删 config 默认后 `getattr(..., False)` 回退 False = 守卫放行 downgrade,语义安全)。 |

### 2B. 删白名单条目 + `config.py` 默认 + 连带孤儿端点/service/测试

#### `ENABLE_G5_CROSSOVER`(+ 连带 sentinel 成员)

- 删白名单 `feature_flag_service.py:372-390`。
- 删 `config.py:1875` 默认 + 同块 `config.py:1879-1882`(`G5_CROSSOVER_MIN_PARENT_SHARPE` / `_LOOKBACK_ROUNDS` / `_TOP_K_OFFSPRING` / `_REQUIRE_DIFFERENT_PILLAR` —— 这 4 个参数仅服务 G5 producer,producer 零调用方,一并清)。
- **连带从 `config.py:552` `LLM_ASSISTANT_SENTINEL_FLAGS` 列表移除 `"ENABLE_G5_CROSSOVER"` 成员**(`config.py:553`)。
- ⚠️ **保守范围**:G5 节点源码(`agents/graph/nodes/g5_persistence.py`、`generation.py:1347-1383` 的 offspring 消费分支、`persistence.py:475-495` 的反向 attribution、`state.py:214-221` 的 `g5_offspring_candidates` 字段、`models/g5_crossover_log.py` 表 + `routers/ops.py:2815-3010` `g5_crossover_stats` 审计端点)**Task 1 不主张删除**。它们是 dead-but-harmless(producer 无调用方→`g5_offspring_candidates` 恒空→分支恒 no-op);删除属更大重构,超出 flag 清理范围。Task 3 若要删需单独评估表/迁移 + ops 端点 404 影响。**最小连带 = 仅白名单 + config 默认 + sentinel 成员**。

#### `FLAT_CROSS_REGION_QUOTA` / `FLAT_CROSS_REGION_ENFORCE`(孤儿端点 + service)

- 删白名单 `feature_flag_service.py:652-674`。
- 删 `config.py:528`(`FLAT_CROSS_REGION_QUOTA` dict)、`config.py:535`(`FLAT_CROSS_REGION_ENFORCE`)、`config.py:536`(`FLAT_CROSS_REGION_LOOKBACK_DAYS` —— 同孤儿端点专属)+ `config.py:522` 注释。
- 连带删孤儿端点:`routers/ops.py:1365-1382` `flat_region_distribution`(`GET /ops/flat-region/distribution`)+ 其 response_model `FlatRegionDistributionOut`(若专属)。
- 连带删孤儿 service:`backend/services/flat_region_quota.py`(`compute_region_share` / `check_quota` / `build_distribution_summary`)—— 唯一调用方是上述端点 + 已删的 FLAT start 流程。
- 连带删测试:`backend/tests/integration/test_ops_flat_region_quota.py`、`backend/tests/unit/test_flat_region_quota.py`。
- 前端:`frontend/src` 零引用(grep `flat-region`/`flatRegion` 命中 0),无前端连带。

#### `ENABLE_TASK_STOP_LOSS` / `TASK_STOP_LOSS_PASS_RATE_FLOOR` / `TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS`(孤儿 service)

- 删白名单 `feature_flag_service.py:603-634`。
- 删 `config.py:500` 块(`# ----- A2 R14 task_stop_loss -----` 下的 3 个默认)。
- 连带删孤儿 service:`backend/services/task_stop_loss_service.py`(`evaluate` / `apply_stop_loss_decision`)—— 原由已删的 `_run_flat_iteration` 调用,四池/beat/router 零调用方。
- ⚠️ **保守范围**:`models/task_stop_loss_event.py`(`TaskStopLossEvent` 表)+ alembic `j5b1a7e3c2f4_task_stop_loss_events.py` 迁移 + `models/__init__.py:115-118` 导出。Task 1 建议 Task 3 评估是否删表;**最小连带 = service + 白名单 + config 默认**。`services/llm_mode_service.py:38` 仅注释提及 R14(不改)。
- 连带删测试:任何 `test_task_stop_loss*`(grep 未直接命中独立测试文件名,Task 3 执行时按 `task_stop_loss` 再扫一遍)。
- 前端:`frontend/src` 零引用(grep `task-stop-loss`/`taskStopLoss` 命中 0)。

---

## 3. 保留 flag 的 lifecycle / domain 表

基准采用设计稿 §B 分类法;下表标注 Task 1 对灰区的最终改判。

### 3A. 灰区最终改判(相对设计稿初版)

| flag | 设计稿初判 | Task 1 终判 | 依据 |
|---|---|---|---|
| `ENABLE_G5_CROSSOVER` | "确认真死"待 G5 复核(冲突 CLAUDE.md) | **REMOVE** | producer 零调用方;唯一真读点 = ops 审计端点 + sentinel 成员。CLAUDE.md "G5 crossover path SURVIVE" 指代码路径仍在(dead-but-present),非 flag 可达。 |
| `ENABLE_TASK_SCHEMA_V2` | 灰区(疑实质死) | **REMOVE** | 仅 alembic downgrade 守卫读,运行时零业务读者。 |
| `FLAT_CROSS_REGION_QUOTA` / `_ENFORCE` | 灰区(端点孤儿?) | **REMOVE** | `flat_region_distribution` 端点前端零引用 + 服务已删 FLAT start。 |
| `ENABLE_TASK_STOP_LOSS` (+2 参数) | 灰区(四池有调用方?) | **REMOVE** | service 唯一调用方是已删的 `_run_flat_iteration`。 |
| `ENABLE_MACRO_NARRATIVE_EXTRACT` | 灰区(DORMANT?) | **保留 / dormant** | **可达**:celery beat `celery_app.py:232` 调度 `run_macro_narrative_extract`,`tasks/macro_narrative_extract.py:91` 真读 `settings.ENABLE_MACRO_NARRATIVE_EXTRACT` 门控 LLM 批生成。default OFF → beat 跑 Phase 1 仍跑、Phase 2 LLM 段 skip。运维型 flag。domain=generation。 |
| `ENABLE_COST_TELEMETRY` | 灰区(DORMANT?) | **保留 / dormant** | **可达**:`cost_tracker.py:155,238` 真读 `settings.ENABLE_COST_TELEMETRY` 门控 `llm_call_log` 写;调用点 `agents/services/llm_service.py:1218-1300`(热路径 LLM 调用)+ ops 端点 `cost_telemetry`(3302)。default OFF=no-op。domain=llm-routing(或 misc telemetry)。 |
| `ENABLE_LLM_ASSISTANT_MODE` | 灰区(sentinel 联动) | **保留 / dormant** | **可达且不可孤立删**:`services/llm_mode_service.py:92` 真读 `settings.ENABLE_LLM_ASSISTANT_MODE`(`resolve_mode`);四池生成节点 `generation.py:1075-1211` 读 `state.llm_mode_used`(assistant 分支真改 expression);sentinel cascade 写在 `feature_flag_service.py:1354-1413`,restore 端点 `routers/ops.py:1241` `/llm-mode/restore-sentinel` + 比较端点 `/llm-mode/comparison`(1152)/`go-gate`(1202)。整套 assistant-mode 机制 **live**(非孤儿),删它牵动 sentinel cascade + `llm_mode_comparison` service + 3 端点 + 一批测试。判保留标 dormant(default OFF + opt-in)。domain=generation。 |
| `HYPOTHESIS_CENTRIC_LEVEL` | 灰区(DORMANT?) | **保留 / dormant** | **读者在但池路径恒 0**:`generation.py`/`evaluation.py:2972`/`persistence.py:937` 读 `cfg.get("hypothesis_centric_level")`,但四池 `pool/hydrate.py:97 hg_run_config()` 只返 `{"configurable": {"trace_service": ...}}` —— **不 seed 该 key**(generation.py:628-629 注释自认 "hg_run_config() drops that key, so it was always 0")。`settings.HYPOTHESIS_CENTRIC_LEVEL` 在池里实际不被消费。非 `ENABLE_` 前缀。**有疑点(读者代码在 + 可重新接线)→ 保守保留标 dormant**,不冒删错风险。domain=generation。 |

### 3B. 全部保留 flag 分类表

> lifecycle ∈ {operational, experimental, dormant};domain ∈ {submit, rag, evaluation, generation, llm-routing, regime, breadth, brain, kb, misc}

| flag | lifecycle | domain |
|---|---|---|
| `ENABLE_AUTO_SUBMIT` | operational | submit |
| `ENABLE_CAN_SUBMIT_REFRESH` | operational | submit |
| `ENABLE_OPTIMIZATION_LOOP` | operational | submit |
| `ENABLE_REGIME` | operational | regime |
| `REGIME_STAGE` | operational | regime |
| `ENABLE_REGIME_MONITOR` | operational | regime |
| `ENABLE_RESIM_BACKLOG` | operational | submit |
| `ENABLE_FIELD_HYGIENE` | operational | generation |
| `ENABLE_DATASET_VALUE_BANDIT` | operational | breadth |
| `ENABLE_PER_FUNCTION_LLM_ROUTING` | operational | llm-routing |
| `ENABLE_LLM_API_CIRCUIT` | operational | llm-routing |
| `ENABLE_SIMULATION_CACHE` | operational | evaluation |
| `ENABLE_HIERARCHICAL_RAG` | operational | rag |
| `ENABLE_FAIL_ALPHA_PERSIST` | operational | evaluation |
| `ENABLE_BRAIN_CONSULTANT_MODE` | operational | brain |
| `LLM_FUNCTION_MODEL_MAP` | operational | llm-routing |
| `LLM_PROVIDERS` | operational | llm-routing |
| `LLM_AVAILABLE_MODELS` | operational | llm-routing |
| `ENABLE_R8_L0` | operational | rag |
| `ENABLE_FACTOR_LENS` | experimental | evaluation |
| `FACTOR_LENS_MODE` | experimental | evaluation |
| `FACTOR_LENS_RESIDUAL_SHARPE_MIN` | experimental | evaluation |
| `ENABLE_AST_ORIGINALITY_GATE` | experimental | evaluation |
| `ENABLE_FAMILY_CAP` | experimental | evaluation |
| `ENABLE_FAMILY_HARD_BAN` | experimental | evaluation |
| `FAMILY_BAN_MIN_PAIRWISE_CORR` | experimental | evaluation |
| `ENABLE_CAPACITY_SCORE` | experimental | evaluation |
| `CAPACITY_SCORE_WEIGHT` | experimental | evaluation |
| `ENABLE_COGNITIVE_LAYER_PROMPT` | experimental | generation |
| `COGNITIVE_LAYER_SELECT_MODE` | experimental | generation |
| `COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET` | experimental | generation |
| `ENABLE_G10_LOGIC_DISTILL` | experimental | kb |
| `LOGIC_DISTILL_MAX_COST_USD_PER_WEEK` | experimental | kb |
| `LOGIC_DISTILL_TOP_K_PER_GROUP` | experimental | kb |
| `LOGIC_DISTILL_MIN_PASS_COUNT` | experimental | kb |
| `LOGIC_DISTILL_LOOKBACK_DAYS` | experimental | kb |
| `LOGIC_DISTILL_SIMILARITY_THRESHOLD` | experimental | kb |
| `ENABLE_G10_LOGIC_INJECT` | experimental | kb |
| `G10_LOGIC_INJECT_TOP_K` | experimental | kb |
| `CODE_GEN_SOFT_REG_MODE` | experimental | generation |
| `CODE_GEN_SOFT_REG_LAMBDA` | experimental | generation |
| `CODE_GEN_SOFT_REG_W_COMPLEXITY` | experimental | generation |
| `CODE_GEN_SOFT_REG_W_ORIGINALITY` | experimental | generation |
| `CODE_GEN_SOFT_REG_W_ALIGNMENT` | experimental | generation |
| `CODE_GEN_SOFT_REG_COMPLEXITY_C0` | experimental | generation |
| `CODE_GEN_SOFT_REG_COMPLEXITY_CMAX` | experimental | generation |
| `CODE_GEN_SOFT_REG_ALIGNMENT_TOPK` | experimental | generation |
| `CODE_GEN_SOFT_REG_ALIGNMENT_SHADOW_SAMPLE` | experimental | generation |
| `ENABLE_QLIB_PRESCREEN` | experimental | evaluation |
| `QLIB_PRESCREEN_MODE` | experimental | evaluation |
| `ENABLE_RAG_CATEGORY_AB` | experimental | rag |
| `ENABLE_DUAL_CHANNEL_RAG` | experimental | rag |
| `ENABLE_DIRECTION_BANDIT` | experimental | generation |
| `ENABLE_AST_DIVERSITY_DIM` | experimental | evaluation |
| `ENABLE_R8_QUERY_LOG` | experimental | rag |
| `ENABLE_HYPOTHESIS_FOREST_REUSE` | experimental | generation |
| `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` | experimental | generation |
| `ENABLE_PILLAR_AWARE_SELECTION` | experimental | generation |
| `ENABLE_ORTHOGONAL_PROMPT_STEERING` | experimental | generation |
| `ENABLE_MACRO_NARRATIVE_GUIDANCE` | experimental | generation |
| `ENABLE_GRADED_SCORE` | experimental | evaluation |
| `ENABLE_ROBUSTNESS_CHECK` | experimental | evaluation |
| `ENABLE_SELF_CORRECT_SEMI_ACCEPT` | experimental | generation |
| `ENABLE_R1A_KB_SKELETON_FREQUENCY` | experimental | kb |
| `ENABLE_SIGNAL_CONTROL_DUAL_RUN` | experimental | evaluation |
| `ENABLE_GRAMMAR_VALIDATOR` | experimental | generation |
| `ENABLE_POOL_COGNITIVE_RECONCILE` | experimental | kb |
| `ENABLE_MACRO_NARRATIVE_EXTRACT` | dormant | generation |
| `ENABLE_COST_TELEMETRY` | dormant | llm-routing |
| `ENABLE_LLM_ASSISTANT_MODE` | dormant | generation |
| `HYPOTHESIS_CENTRIC_LEVEL` | dormant | generation |

> 共保留 **~73 行**(部分为参数 flag)。`ENABLE_AUTO_SUBMIT`/`ENABLE_OPTIMIZATION_LOOP` 虽 default OFF,但属 operator 直接拨的生产开关 → operational。`ENABLE_LLM_ASSISTANT_MODE`/`HYPOTHESIS_CENTRIC_LEVEL`/`ENABLE_MACRO_NARRATIVE_EXTRACT`/`ENABLE_COST_TELEMETRY` 可达但 default OFF + 当前不在主热路径决策 → dormant。

---

## 4. 关键裁决依据(证据细节)

### G5 裁决(REMOVE)
- 生产 G5 producer(round 末选 2 PASS → `llm_crossover_alpha` → 写 `task.config['g5_pending_offspring']`)的入口函数 `stash_pending_offspring` / `load_pending_offspring`(`g5_persistence.py`)在全 `backend/`(排除 tests/pycache)**零调用方**(`grep stash_pending_offspring backend --include=*.py` → 仅命中 g5_persistence.py 自身定义,无 caller)。
- 四池 `backend/pool/` 内无任何 `g5` / `crossover` / `FeedbackEvent` 引用(`grep ... backend/pool` → 空)。`FEEDBACK_PASS_LANDED`(types.py:57)只是常量定义,无消费者。
- `state.g5_offspring_candidates`(generation.py:1352 消费)因 producer 无调用方 → 恒空 → 分支恒 no-op。
- `settings.ENABLE_G5_CROSSOVER` 唯一真读点 = `routers/ops.py:2898/3005`(`g5_crossover_stats` 审计端点,纯 instrumentation,注释 2871 自认 "llm_crossover_alpha deleted (1c) + ENABLE_G5_CROSSOVER OFF")+ `config.py:553` sentinel 成员。
- **结论**:flag 不可达 → REMOVE。CLAUDE.md "G5 crossover path SURVIVE" 描述的是代码路径源码仍存(dead-but-present,Task 1 不删源码),不等于 flag 可被生产消费。符合设计稿 §A "纯 instrumentation 才删" 判据。

### `flat_region_distribution` 端点孤儿(REMOVE)
- 端点 `routers/ops.py:1365 GET /ops/flat-region/distribution` 唯一读 `FLAT_CROSS_REGION_*`;它原服务已删的 `POST /ops/start-flat-session` 准入(quota 检查)。
- 前端 `frontend/src` grep `flat-region`/`flatRegion`/`flat_region` → **0 命中**。
- 四池/beat 无调用 `flat_region_quota` service。→ 孤儿,连端点 + service + 2 测试一并删。

### `task_stop_loss_service` 孤儿(REMOVE)
- service 文件 docstring(`task_stop_loss_service.py:19`)自认 "flat loop already `continue`s before calling stop_loss_service"——它的设计调用方就是 FLAT loop。
- `_run_flat_iteration` / `run_mining_task` 已删(`b89b732`)。
- 四池 `pool/workers.py`、`celery_app.py` beat、活跃 router 均无 `evaluate_stop_loss` / `apply_stop_loss_decision` 调用(grep 仅命中 service 自身 + model + alembic + 白名单 + 一处 llm_mode_service 注释)。→ 孤儿。

---

## 5. 给 Task 2-5 的执行注记

- **Task 2(白名单瘦身)**:删 §1 全部 12 个 flag 的 `SUPPORTED_FLAGS` 条目 + §2 列出的 `config.py` 默认。`ENABLE_G5_CROSSOVER` 连带删 `config.py:553` sentinel 成员 + G5 4 个参数默认。保留所有 instrumentation 审计端点(回退 False 不崩)。
- **Task 3(孤儿端点/service/测试)**:删 `flat_region_distribution` 端点 + `flat_region_quota.py` + 2 测试;删 `task_stop_loss_service.py`。G5 源码 + g5/task_stop_loss 的表/迁移/model **Task 1 标为超范围**,Task 3 若删需单评估(404 / 表 drop)。
- **Task 5(Alembic 孤儿行)**:`:removed` = 上 12 flag + 已知历史孤儿(`ENABLE_CASCADE_LEGACY`、`ENABLE_HIERARCHICAL_RAG_CACHE`、`ENABLE_R5_L2_RANKING`)。注意非 `ENABLE_` 前缀的 `FLAT_CROSS_REGION_QUOTA`/`_ENFORCE`、`TASK_STOP_LOSS_PASS_RATE_FLOOR`/`_CONSECUTIVE_FAIL_ROUNDS`、`GRAMMAR_VALIDATOR_RETRY_MAX`、`ENABLE_TASK_SCHEMA_V2` 同样按 `flag_name` 字符串清 `feature_flag_overrides` + `feature_flag_audit`。
- **疑虑/保守点**:
  1. G5 与 task_stop_loss 的 **源码/表** 未列入连带删(避免 flag 清理任务越界做大重构);若用户要求"取最干净",Task 3 可单独评估。
  2. `HYPOTHESIS_CENTRIC_LEVEL` 读者代码在但池路径恒 0,严格说"运行时实质死",但保守保留标 dormant(可重新接线 + 非 `ENABLE_` 前缀,删错代价高)。如需更激进可改 REMOVE,但本普查判保留。
