# Phase 1d 协调式 cutover runbook(待执行,需维护窗口)

> 2026-06-06。来源:verify→refute workflow `wv74w1aw1`(12 agents)对 **post-1c-delete live 代码**核查。
> Phase 1d = runs.py 重写 + DROP run_id/experiment_runs + MiningTask 死列。**不可逆(DROP COLUMN/TABLE)+ 碰 live 持久化热路径 + 碰 live 前端 task-UI** → 必须**协调式 cutover**(改码 → 重启加载 → 备份 → Alembic → 重启),不能 blind drop。**等用户 go + 维护窗口**。

## 0. 已先行执行(commit `c20f21f`,可逆)
config 死键(ORCHESTRATOR_*/MAX_SIMULATIONS_PER_DAY)+ 前端 orchestrator/regime 独立死页。不含本 runbook 的任何 schema/热路径改动。

## 1. DROP 分类(verify 实证)

**可 DROP(无 live 写,仅 legacy 读者在 runs.py/serializer)**:
- `alphas.run_id` · `trace_steps.run_id` · `alpha_failures.run_id`(池 persister 已写 `run_id=None`,workers.py:206 `build_persister(run_id=None)`)
- `experiment_runs` 表(post-1c 无任何写者;只 runs.py + `/tasks/{id}/runs` 读)
- `MiningTask.generation_strategy`(0 读写,立即可删)
- `MiningTask.current_iteration` / `max_iterations` / `progress_current`(无 live 写;只 task_service serializer + dashboard + 前端展示读)

**KEEP(live 池在写,必须先迁移机制才能删)**:
- `MiningTask.schedule` —— `hydrate.py:132` 写 `'POOL'`(常驻锚任务判别)+ 前端 TaskDetail 按它分支 PAUSE/RESUME。删前需:换判别方式 + 前端去 schedule 分支。
- `MiningTask.last_alpha_persisted_at` —— `persistence.py:541-545` 每次落库写(池心跳/watchdog liveness)。删前需:把心跳迁到 candidate_queue 或新轻量心跳表。

## 2. 代码改动(STEP 1,可逆,先于 schema)

**run_id 链去除(池已传 None → 字节等价)**:
- `agents/graph/nodes/persistence.py`:`_incremental_save_alphas`(去 run_id 参数 + Alpha INSERT key,~400)、`_incremental_save_failures`(去 run_id 参数 + AlphaFailure key,~758/789);去 `configurable.get('run_id')`(workflow 传递)
- `agents/pipeline/persister.py`:`build_persister`(去 run_id 参数,71)、`_flush_trace`(47/53)、passthrough(122/141)
- `agents/services/trace_service.py`:`TraceService.__init__`(去 run_id,60/65)、两处 `TraceStep(run_id=…)`(115/161)
- `pool/workers.py:206`:`build_persister()` 去 `run_id=None`

**死 serializer 列去除(current_iteration/max_iterations/progress_current/generation_strategy)**:
- `services/task_service.py`:TaskSummary/TaskDetail dataclass 字段 + `_to_summary`/`get_task_detail` 投影
- `routers/tasks.py`:TaskResponse/TaskDetailResponse Pydantic 字段(注意 max_iterations 有 hardcoded default=10)
- `services/dashboard_service.py:102,226`:progress 改由 hyp_intent/candidate_queue claim 计数算(不再读 task.progress_current)
- `services/mining_service.py`:死 ONESHOT 残留(create_task max_iterations 写、run_mining_iteration current_iteration 增)——确认是 1c 后死码,一并清

**runs.py / experiment_runs / `/tasks/{id}/runs`(REWRITE 或退役 —— 决策点 §5)**:
- 选项 A(退役):删 routers/runs.py + services/run_service.py + repositories ExperimentRunRepository;`/tasks/{id}/runs` 改返回空/弃用提示;前端 TaskDetail runs tab 隐藏。
- 选项 B(重写):runs.py + list_task_runs 改投影 hyp_intent+candidate_queue 成 ExperimentRunResponse 形状(前端零改,TaskDetail 优雅处理空)。
- ExperimentRun model(models/task.py:71-110)+ task_service import 在 A/B 完成后删。

## 3. 前端 task-UI(STEP 1b,可逆 —— 决策点 §5)
post-1c 已坏(调已删/neuter 端点):
- TaskManagement.jsx:`startTaskMutation`(start 按钮→400)、`startFlatSessionMutation`(→404)、create 模态(create_task)
- TaskDetail.jsx:PAUSE/RESUME 的 FLAT 分支 `pause/resumeFlatSession`(→404);非-FLAT 走 intervene(仍活)
- DataManagement.jsx:`startFlatSession`(delay-0,→404)
- api.js:`startTask`/`startFlatSession`/`resumeFlatSession`/`pauseFlatSession`
处理:去死按钮 + 死 api 方法;TaskDetail 去 FLAT 分支(池任务走 intervene);create 模态按 §5 决策去留。

## 4. 协调式 cutover 序(STEP 2-5,不可逆部分)
1. **改码**(§2 STEP1 + §3)→ build + import smoke + 隔离测(persistence/pool/task_service)。
2. **commit**(git-revert-only)。
3. **重启** uvicorn + celery + pool supervisor 加载新代码(此后池不再写 run_id;serializer 不再读死列)。验证池仍产出 + `/tasks/{id}` 正常。
4. **备份** DB(`pg_dump` 至少 mining_tasks/alphas/trace_steps/experiment_runs)。
5. **Alembic** 新 revision:`DROP CONSTRAINT` run_id FK(trace_steps/alphas/alpha_failures)→ `DROP COLUMN` run_id ×3 + generation_strategy/current_iteration/max_iterations/progress_current → `DROP TABLE experiment_runs`。**先 DB 副本测**(仿 cell_stats cutover SOP)。
6. **重启**;冒烟 `/tasks` 列表/详情 + 池产出 + dashboard。

> **schedule / last_alpha_persisted_at 不在本次 DROP** —— 需先(a)把 last_alpha_persisted_at 心跳迁到 candidate_queue/新表 +(b)换 schedule 判别 + 前端去分支,作为 Phase 1d-2(单独窗口)。

## 5. 产品必决 —— 已拍板 2026-06-06(全选清洁/激进路径)
1. **runs / experiment_runs** → **A 退役**:删 routers/runs.py + run_service.py + ExperimentRunRepository + /tasks/{id}/runs;DROP experiment_runs 表。
2. **task 创建/启动 UI** → **整个去掉**:删 create 模态 + start 按钮 + POST /tasks + /tasks/{id}/start + api.startTask/createTask。(「以 alpha 为蓝本优化」是独立 trigger,不动。)
3. **Tasks 页定位** → **整体退役**:删 TaskManagement/TaskDetail;`/tasks` + `/tasks/:id` 重定向 `/ops/pool-pipeline`;AppSidebar 去 /tasks 菜单。实时挖掘看 /ops/pool-pipeline,alpha 浏览看 /alphas。

## 6. 风险
- run_id 链在 live 持久化热路径 → 改后必隔离测 persistence/pool + 重启验证池产出。
- DROP 不可逆 → 备份 + DB 副本测 Alembic。
- `/tasks/{id}/runs` 是 live 前端读 → 退役/重写前端同 commit。
- schedule/last_alpha_persisted_at 误删 = 打挂 live 池 → 明确排除本次,Phase 1d-2 先迁机制。
