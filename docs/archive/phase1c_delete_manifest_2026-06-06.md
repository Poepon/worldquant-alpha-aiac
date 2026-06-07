# Phase 1c-delete 执行清单(书面版,未动代码)

> 产出日期 2026-06-06。来源:11-group `verify → 对抗式 refute` workflow `wf_21a525ca-11d`
> (22 agents / 1.76M tok / 881 tool-uses)对 **当前 live 代码**(非 stale plan)的逐目标核查。
> 决策:用户选 **「helper 抽取后整删 mining_tasks.py」+「先只要书面清单,本轮不动手」**。
> 全部为**一个原子 commit**(跨组耦合要求),**git-revert-only**,**零 Alembic**(不动 schema)。
> 行号来自 workflow 核查,**执行时须再 grep 复核**(文件可能漂移)。

---

## 0. 核心洞察:两类「BLOCKER」

verify→refute 把所有 BLOCKER 分成两类:

- **(A) 跨组耦合**(多数):X 被 Y 引用判 BLOCKED,但 Y 本身在另一删除组里。最反复出现的是
  `backend/tasks/__init__.py` + `celery_app.py` beat —— celery `include=['backend.tasks']` 在 worker
  启动时**无条件 import** 每个待删任务函数;漏改一处 = **celery autodiscovery ImportError = 整个
  worker 起不来**。这些**只有全部落同一 commit 才解开**。
- **(B) 真·存活依赖**(少数,真正的雷):**live 代码(池 / evaluation / persistence 节点)import 待删代码**。
  这些必须在删前做「抽取 / 保留 / DEFER」处理。见 §1。

---

## 1. 真·存活依赖(执行前/同 commit 必处理)

| # | 雷 | 处理 |
|---|---|---|
| **R1** | `pool/hydrate.py:157` import `_get_dataset_fields`+`_get_operators`(定义在 mining_tasks.py:807/843)—— **live 池依赖** | **抽** 两函数 → 新模块 `backend/tasks/fetch_helpers.py`,改 hydrate import,**再整删 mining_tasks.py**(用户选)。闭包见 §2。 |
| **R2** | `evaluation.py:2692` lazy-import `_quota_guard_async`(在 `session_watchdog.py`)—— live 节点 | **保留 session_watchdog.py**;只删 `watchdog_revive_dead_sessions` 函数 + 5min beat。verifier 过度删整文件=**错**。 |
| **R3** | `persistence.py:541` 每次落库**无条件**写 `task.last_alpha_persisted_at`;`hydrate.py:132` 写 `schedule='POOL'` —— live 池在写这两列 | **所有 MiningTask 列 DROP 全部 DEFER 到 Phase 1d**;1c 不含任何 Alembic。见 §6。 |
| **R4** | `redis_pool.py:145-252` cascade-lock 函数仅 cascade 用,但同文件 `get_redis_client`(43-54)live | 只删 145-252;**保留文件 + get_redis_client**。 |
| **R5** | `r5_judge.py` 有第二个 flag-独立调用点:`evaluation.py:1148` soft-reg 对齐腿,gate `CODE_GEN_SOFT_REG_W_ALIGNMENT>0` | **已验证 = 0.0**(config:1503「reserved for P2」,无 DB override)→ 该腿是死路。删 r5_judge 时**同时删 evaluation.py soft-reg 腿**(读 `_sr_w_a` 的 ~1107-1153)。两处 r5_judge import(:1148/:3178)都是 lazy。 |

---

## 2. R1 helper 抽取(pre-flight,同 commit)

新建 `backend/tasks/fetch_helpers.py`,**逐字搬入**两函数 + 其闭包:

- `_get_operators(db)`(原 mining_tasks.py:807-840)—— 依赖:`select` · `Operator` · `logger`
- `_get_dataset_fields(db, dataset_id, region, universe, delay=1)`(原 843-892)—— 依赖:`select` · `and_` ·
  `case` · `DatasetMetadata` · `DataField` · `DataFieldCellStats` · 常量 `_FLAT_DELAY`(=1,直接内联默认值)

两函数**自含**(不调 `_get_universal_pv_fields`/`_merge_field_pools` —— PV 合并发生在 FLAT 调用方,随
mining_tasks.py 一起删)。改 `pool/hydrate.py:157`:
`from backend.tasks.fetch_helpers import _get_dataset_fields, _get_operators`。
**抽取后跑 `import backend.pool.hydrate` smoke** 再继续删。

---

## 3. 整文件删除(~10K LOC)

| 文件 | LOC | 组 |
|---|---|---|
| `backend/tasks/orchestrator.py` | 565 | orchestrator |
| `backend/agents/core/`(9 文件:__init__/integration/pipeline/trace/scenario/knowledge/experiment/evolving_rag/feedback) | ~3024 | core |
| `backend/agents/mining_agent.py` | 1387 | mining-agent(R1 抽取后) |
| `backend/agents/strategy_agent.py` | 481 | strategy |
| `backend/agents/evolution_strategy.py` | 746 | evolution |
| `backend/agents/field_screener.py` | 428 | fieldscreener |
| `backend/genetic_optimizer.py`(GA 类;`window_perturbation.py` 已抽,Phase 1a) | 1248 | genetic |
| `backend/regime_classifier.py` | 245 | regime |
| `backend/services/regime_inference_service.py` | 267 | regime |
| `backend/tasks/regime_infer.py` | 148 | regime |
| `backend/agents/llm_crossover_alpha.py`(G5 crossover) | — | g5 |
| `backend/agents/graph/r5_judge.py`(R5 judge) | — | r5 |
| `backend/tasks/r1b_tasks.py`(failure-tree pruner) | — | r1b |
| `backend/tasks/r1b_outcome_reconcile.py` | — | r1b |
| `run_real_mining.py`(根) | 381 | core |
| `backend/tasks/mining_tasks.py`(R1 抽 helper 后整删) | ~1655 | flat |

> 注:plan 列的 `feedback_r1b/r1b_loop/feedback_g5/g5_persistence/r5_judge` 多数**已不存在**
> (R1b typed 子图在 Phase C `e98dd89` 已删 1025 行)。残留 R1b/G5/R5 面比 plan 小:仅上表 4 文件 + eval 块。

**测试文件**(随源码删):`test_core_*`(6)· `test_orchestrator_skeleton.py` · `test_apply_field_filters_pv_protected.py` ·
`test_r1a_hook_evaluation.py` · r1b/g5/r5/regime 相关测试。`test_r1a_ops_telemetry.py` **保留**(测历史 telemetry 端点,不 import core)。

**前端**:`OrchestratorMonitor.jsx` · `Regime.jsx`(端点删后会 404)。

---

## 4. 外科编辑(companion,同 commit,否则启动崩)

| 文件 | 编辑 |
|---|---|
| `backend/tasks/__init__.py` | 去 import+__all__:orchestrator(84-87)· session_watchdog 的 `watchdog_revive_dead_sessions`(50-53 之一,**保留 quota_guard**)· r1b(71,89 + __all__ 162,172)· regime_infer(67 + __all__ 158)· mining_tasks `run_mining_task`(27) |
| `backend/celery_app.py` | 删 5 个 beat:`orchestrator-periodic-scan`(360-363)· `watchdog-revive`(239)· `r1b-failure-tree-pruner`(297-300)· `r1b-outcome-reconcile`(371-374)· `regime-infer`(231-234) |
| `backend/agents/__init__.py` | 去 `MiningAgent`/`create_mining_agent`/`FeedbackAgent`(6-7)+ __all__;**保留** `MiningWorkflow`/`MiningState`/`create_mining_graph` re-export |
| `backend/agents/graph/nodes/evaluation.py` | 删 **R1a+R5 块 3165-3347**(整体,R5 依赖 `_r1a_log` 不可拆)· 删 **regime 块 1968-1990 + lazy import 1973** · 删 **soft-reg r5 腿 ~1107-1153**(W=0 死路) |
| `backend/tasks/session_watchdog.py` | **只删** `watchdog_revive_dead_sessions`;保留 `quota_guard_pause_at_threshold` + `_quota_guard_async`(evaluation:2692 live) |
| `backend/tasks/redis_pool.py` | 删 cascade-lock 函数(145-252);**保留** `get_redis_client` |
| `backend/tasks/mining_tasks.py` 内 cascade | `_is_cascade_schedule` + cascade dispatch 块(~205-341)随整文件删除一并消失 |
| `backend/services/task_service.py` | 删 `start_flat_session`/`resume_flat_session`/`pause_flat_session`/`_dispatch_session_worker`(589-730 等);role-snapshot 冻结 / region-delay 校验若被别处复用则保留 |
| `backend/routers/ops.py` | 删端点:`/orchestrator/status`(5914-6018)· `/start-flat-session`(1385-1466)· `/flat-sessions/{id}/resume`(1514-1522)· `/regime/*`(835-879)+ 各 schema |
| `backend/services/ops_service.py` | 删 regime 方法(get_regime_current/snapshot/history)+ TASK_LISTS 里 `run_regime_infer` |
| `backend/config.py` | 去 `ORCHESTRATOR_*`(952-966)· `R1B_*`/`G5_*`/`R5_*` · `MAX_SIMULATIONS_PER_DAY`(0 readers)。`CODE_GEN_SOFT_REG_W_ALIGNMENT` 可留(reserved P2,无害) |
| 前端 | `OpsLayout.jsx` 路由(orchestrator/regime)· `AppSidebar.jsx` 菜单 · `api.js`(getOrchestratorStatus/startFlatSession/regime wrappers)· `TaskManagement.jsx` flat-session 调用 |

---

## 5. 保留(verifier 过度删 / live 依赖)

- `mining_tasks.py` 的 `_get_dataset_fields`/`_get_operators` → 已抽到 `fetch_helpers.py`(§2)
- `session_watchdog.py` 文件本体(quota_guard live)
- `redis_pool.py` 文件本体 + `get_redis_client`
- `agents/prompts.py` shim(HG+E 节点引:generation:49/validation:22/evaluation:81)
- `agents/attribution_types.py` · `window_perturbation.py`(Phase 1a 已抽)
- **MiningTask 模型全列**(列 DROP 见 §6 DEFER)
- `test_r1a_ops_telemetry.py`(历史 telemetry,不 import core)
- `r1a_attribution_log` 表(dormant,256 天数据;hierarchical_rag raw-SQL 仍读)

---

## 6. 纠正性 DEFER → Phase 1d

**所有 MiningTask dispatch 列 DROP**(`schedule`/`max_iterations`/`last_alpha_persisted_at`/
`generation_strategy`/`current_iteration`/`progress`)**全部推迟**。理由(verifier 实证):

- `persistence.py:541-544` 每个 pool persist **无条件** `UPDATE last_alpha_persisted_at`(node_save_results 热路径)
- `hydrate.py:132` 写 `schedule='POOL'`(常驻锚任务 FK)

→ **live 池正在写这些列**,1c 删列直接打挂在产的池。Phase 1d 先在 persistence/hydrate **停写**这些列,
再 Alembic DROP。`alphas.run_id`/`trace_steps.run_id`/`experiment_runs` 同样 1d。
**1c 全程零 Alembic = live 池在重启加载新代码前完全不受影响。**

---

## 7. 执行序(一个原子 commit)

1. **抽** `fetch_helpers.py`(§2)+ 改 hydrate import → `import backend.pool.hydrate` smoke。
2. **改 companion**(§4)先于删源文件:`tasks/__init__.py` import / `celery_app.py` beat / `agents/__init__.py` /
   evaluation 块 / session_watchdog/redis_pool/task_service/ops/config 的外科切除。
3. **删整文件**(§3)。
4. **前端**路由/菜单/api/TaskManagement。
5. **冒烟门**(全过才算完):
   - `python -c "import backend.main"` · `import backend.celery_app` · `import backend.tasks` · `import backend.pool.run_worker`
   - `pytest backend/tests/unit -q`(净新增回归 = 0;删的测试不计)
   - `cd frontend && npm run build`
6. **不 push**,等用户确认。
7. **重启** worker/beat/pool supervisor 加载新代码(live 池在此前跑旧代码,无碍)。

---

## 8. 风险登记

| 风险 | 缓解 |
|---|---|
| 漏一处 `tasks/__init__.py` import → celery 全崩 | §4 逐行;冒烟门第一项 import backend.tasks |
| R1 抽取漏 transitive helper | 闭包已证最小(§2);抽后单独 import smoke |
| evaluation R1a/R5 块拆不干净(R5 依赖 `_r1a_log`)| 整块删 3165-3347,不外科拆 |
| 删列打挂 live 池 | §6 全 DEFER,1c 零 Alembic |
| regime lazy import 残留(flag 翻 ON 才炸)| 同 commit 删 evaluation:1973 + regime 块 + 3 文件 + 端点 |
| 不可逆 | git-revert-only;一个 commit 便于整体 revert;冒烟门把关 |

---

## 9. 待用户「go」

本清单即 runbook。用户说执行时按 §7 序做一个原子 commit + §5 冒烟门,**不 push 直到再确认**。

---

## 10. 执行记录(2026-06-06,用户「go」)

**已执行(后端核心,本 commit)**:
- 抽 `backend/tasks/fetch_helpers.py`(`_get_dataset_fields`/`_get_operators`)+ 改 hydrate/generation import。
- 整删 30+ 源文件(orchestrator/mining_agent/strategy/evolution/field_screener/regime×3/llm_crossover/r5_judge/r1b_tasks×2/genetic_optimizer/core×9/run_real_mining + **pipeline FLAT 簇 runner/producer/consumer/feedback_g5/feedback_r1b/client_refresh**)。**实跑发现 verifier 漏的依赖**:generation.py:667 也引 helper(已 repoint)、pipeline/__init__ 被 pool 经 submodule import 触发(已改只留 types+persister)、feedback_g5/r1b 也是 FLAT-only(加入删除集)、benchmark_test/test_suite/test_integration 引 GeneticOptimizer。
- companion:tasks/__init__(5 import+__all__)、celery_app(5 beat + run_mining_task route)、agents/__init__(**保留 FeedbackAgent**——daily-feedback beat 在用,verifier/manifest 误判可删)、evaluation.py(R1a+R5 块 3152-3347 + regime 乘子块 + soft-reg 对齐腿)、pipeline/__init__。
- 死码清理:session_watchdog(只删 revive 簇,留 quota_guard)、redis_pool(只删 cascade-lock,留 get_redis_client)、task_service(删 4 flat 方法 + **neuter start_task 为清晰 retired-error**)、ops_service(删 3 regime 方法 + TASK_LISTS 项)、ops.py(删 8 端点:regime×4/flat×3/orchestrator×1)。
- 测试:整删 30 个纯删测试 + 修 8 个 MIXED(repoint helper / 删死测试段)。**净新增回归 0**。
- 验证:`import backend.main/celery_app/tasks/pool.run_worker` 全过;`pytest backend/tests/unit` 收集 2315 + 全绿;每文件 py_compile。**零 Alembic**(live 池重启前不受影响)。

**DEFER(documented follow-up,非本 commit)**:
- **前端死 UI**:OrchestratorMonitor.jsx / Regime.jsx 页 + OpsLayout 路由 + AppSidebar 菜单 + api.js wrappers(getOrchestratorStatus/regime×4/startFlatSession/resume/pause)+ TaskManagement/TaskDetail/DataManagement 的 flat-session 按钮/调用 + OpsOverview regime 展示。前端**当前 build 通过**(未动),仅几个 ops 页运行时 500。woven UI 拆除风险高,留聚焦 follow-up。
- **config 死键**:ORCHESTRATOR_*/R1B_*/G5_*/R5_*/ENABLE_R1A_HOOK/ENABLE_LLM_JUDGE/MAX_SIMULATIONS_PER_DAY ——散落 2000 行 Settings、纯死默认值无害,删除高 tedium 低价值+交叉引用风险,DEFER。
- **完整 ONESHOT 端点/路由拆除**(routers/mining.py + tasks.py 的 start_task 端点 + 前端建任务 UI):start_task 已 neuter 为 retired-error;端点/UI 完整拆除归 Phase 1d(runs.py 重写)。
- **MiningTask 列 DROP**:全 DEFER 到 Phase 1d(§6,live 池在写)。
