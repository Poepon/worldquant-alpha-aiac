# 串行路线退役 / 只保留流水线 路线迁移 plan v3

- 日期: 2026-05-29
- 状态: draft (未开工)
- 目标: 删除 FLAT mining 串行路线 (`_run_flat_iteration` + `_run_one_round_inline`),只保留流水线路线
- 历史: v1 漏掉 5-27→5-29 共 25 个 pipeline commit / v2 把 R14 当搬运工作低估、缺 soak gate、数字错。v3 修这些
- **v3 修订 2 (2026-05-29 晚)**: R14 推迟,见 §2.5。当前无 orchestrator 接班,R14 PAUSE 会留死结(配额空转),反自动化目标。R14 与 orchestrator 打包,见 [orchestrator-plan](./orchestrator_plan_2026-05-29.md)
- **v3 修订 3 (2026-05-29 晚)**: R1b typed 4b 完整 retire(dormant 路径,生产 0 触发):删 `r1b_typed_pipeline.py` 整模块 + `_maybe_run_typed_pipeline_round` wrapper + dispatch + 3 个测试 + `ENABLE_R1B_TYPED_PIPELINE` + `R1B_TYPED_NUM_ITER_PER_ROUND` config + ops/feature_flag_service 引用。Phase C 测试影响清单从 4 降到 2(commit `e98dd89`)
- **v3 修订 4 (2026-05-29 晚) — Phase C 主体已 ship**: 删 `_run_one_round_inline`(166 行)+ 串行 `_run_flat_iteration`(419 行),rename `_run_flat_iteration_pipeline` → `_run_flat_iteration`,mining_tasks.py 2186 → 1601 行(-585)。bulk rename 14 处 `_run_one_round_inline` 注释 → `pipeline round`,2 处 `_run_flat_iteration_pipeline` docstring + 6 处测试 patch 改新名。删 `test_flat_round_failure_recovery.py`,skip `test_external_call_deadlines.py:test_round_deadline_soft_fails` + `test_r1b_round_boundary_wire.py:test_byte_equiv_sentinel_...`。Phase C 验证:1020 unit tests + frontend build PASS,Phase C 相关 33/33 PASS。Phase D 还剩:删 `_pick_least_covered_dataset` dead code + `_rebuild_flat_db_session`(≥7d 观察后)+ `_refresh_brain_client`(若 `BrainClientRefresher` 接管验证无遗漏)
- 前置: 0 个 blocking gap (R14 推迟后),2 个低优 gap (auth-circuit park-and-retry / trace iteration_offset)

---

## 1. 真实现状 (实测 2026-05-29)

### 流水线已接热路径,默认 flag OFF

`mining_tasks.py:1817-1823` 顶部 dispatch:
```python
_pipeline_on = bool(getattr(settings, "ENABLE_SIM_PIPELINE", False)) or bool(
    (getattr(task, "config", None) or {}).get("enable_sim_pipeline")
)
if _pipeline_on:
    return await _run_flat_iteration_pipeline(...)
```

- 全局 flag `ENABLE_SIM_PIPELINE=False` (default)
- per-session opt-in: `task.config["enable_sim_pipeline"]=True`,通过 `/ops/start-flat-session` payload `enable_pipeline=True` 写入 (`task_service.py:595,640`)
- 前端入口已实装: `frontend/src/pages/TaskManagement.jsx:445` form field,初值 false
- 任一为 True → 走 `_run_flat_iteration_pipeline()` (mining_tasks.py:1358–1794, 437 行胶水 wrapper)

### Sub-phase 1/2/3 + Unit 2c-step2 全部已 ship (5-27 到 5-29 共 25 commit)

| commit | 内容 |
|---|---|
| `062075f` | Sub0 Unit 1 — 并发安全管道骨架 (flag OFF) |
| `5440f9d` | Unit 2c-step2 — pipeline 接入 `_run_flat_iteration` |
| `ac30cf0` | live FLAT pipeline path code review |
| `c5cb6b0` | Sub-phase 1 — BRAIN auth-circuit guard 接 producer |
| `4b14b51` | Sub-phase 1 — FAIL→alpha_failures persister |
| `55b439e` | Sub-phase 1 — coordinated BRAIN client refresh (F4) |
| `fd012ba` | per-session opt-in (单 task 灰度) |
| `4c2b4b9` | superseded run ownership-loss 关闭 (orphan-run 修复) |
| `fab28b4` | Sub-phase 1 — trace flush (恢复前端轨迹) |
| `f1845d6` | SAVE_RESULTS trace step |
| `ead91a3` | Option C step-1 — coverage-greedy dataset steering |
| `b87fdd7` | Option C step-2 — margin-steered ε-greedy |
| `75d23bf` | Option C step-3 — category-stratified explore |
| `2c22934` | F2-1 feedback-channel infra + quiescence termination |
| `9015f07` / `18f4508` | F2-2 R1b retry 接入 feedback channel |
| `d307441` / `b0fce2c` | F2-3 R1b hypothesis-mutate 接入 |
| `ddb78d7` / `aa82faf` | F2-4 G5 crossover 接入 |
| `2deea15` | Sub-phase 3 — producer 在 HYPOTHESIS 处 split |
| `08f6ded` | 避免重用 gen-timeout 后毒化的 session |
| `641a865` / `be1d287` / `c166eaa` | per-op timeout backstop 三步 |
| `365ea01` | op_timeout 钳到 watchdog 窗口下 (3736 spurious revive 修) |
| `2f3dd58` | **session-level heartbeat-abort — freeze CLASS 结构性修复** |
| `db46f87` | run_mining_task 跳过 STOPPED/EARLY_STOPPED/PAUSED 重投 |
| `ede70e9` | per-session R1b mutate 上限 |

### Freeze CLASS 已有结构性修复设计 (2f3dd58)

5-28 task 3737/3738/3739 实跑暴露:每次 session 在不同的 unwrapped DB-op 处 freeze (producer DB probe / drain handler / persister write),per-op timeout 是 whack-a-mole。

**修复设计**:`run_pipeline_session` 内 heartbeat supervisor 跟踪 `last_progress` (push / persist flush / drain event_done 各处 beat),超过 `SIM_PIPELINE_HEARTBEAT_TIMEOUT_SEC` (default 900s) 无进展 → cancel 整个 pipeline task → `PipelineHeartbeatExpired` → finalize PAUSED → 下次 dispatch 到 fresh worker 进程 (fresh asyncpg pool) 继续。Hard cap 在 watchdog 窗口 (1500s) 之下。

**v3 修正 — 已知 unknown**:5-28 16:00 (commit `2f3dd58` 之后) 至 5-29 整日 0 pipeline commit / 0 shadow session 报告 (`docs/shadow_session_*.md` 不存在)。结构性修复**未经 ≥24h soak 实证**,目前只是 commit message claim。Phase B 首要任务是 first real soak。

---

## 2. 真正剩余的 gap (来源:`_run_flat_iteration_pipeline` docstring 1373-1384)

| Gap | 优先级 | 说明 |
|---|---|---|
| ~~R14 task stop-loss~~ | **推迟,见 §2.5** | 与 orchestrator 打包。当前无接班机制,R14 PAUSE → 配额空转死结,反目标 |
| BRAIN auth-circuit park-and-retry | 低 | 流水线现是 stop-on-open (cursor 保留 + 重 dispatch);串行 park 在 dataset 上 retry。**短期需运维 monitor**:若 ops 不 re-auth,会出现 dispatch → 立刻 stop → 再 dispatch 反复 |
| trace iteration_offset threading | 低 | 纯 observability,`trace_steps.iteration` per round 重启 |
| ~~F4 client refresh~~ | ~~已~~ | `SIM_PIPELINE_CLIENT_REFRESH_EVERY=32` default 已开,`BrainClientRefresher` drain-and-refresh barrier 已接 |
| ~~F1 session 隔离~~ | ~~已~~ | producer / persister 各自 session,N consumer DB-free |
| ~~F2 freeze 类~~ | ~~设计已~~ | heartbeat-abort 结构性修复(soak 验证待做) |

### §2.5 R14 推迟决策 (2026-05-29 晚)

**结论:R14 现在不做,与 orchestrator 一起做**。

**实证**:
- 双层硬兜底已存在,"无止损"不成立:
  - 流水线单 task 上限 = `FLAT_CONTINUOUS_MAX_ITERATIONS=100` × `SIM_PIPELINE_DATASET_BATCH=4` = **400 sim** (producer self-exit @ mining_tasks.py:1511)
  - 全局 `quota_guard_pause_at_threshold` celery beat 每 10min,阈值 `BRAIN_DAILY_SIMULATE_LIMIT=1000` × 90% = **900 sim/天** → PAUSE 所有 RUNNING task (session_watchdog.py:246-372)
- **当前 0 orchestrator** (celery beat 实测无 auto-launch task):`start_flat_session` 0 自动 caller,全靠手动 `POST /ops/start-flat-session`

**为什么 R14 + 无 orchestrator = 反目标**:
```
task A 跑 5 round 0 PASS → R14 PAUSE → 配额剩 880 sim 没人用
  → 无 orchestrator 开 task B → task A PAUSE 死在那
  → watchdog 只复活 RUNNING-stale,不动 PAUSED
  → 880 sim 当天空转
  → 用户手动 resume → 同样烂 → R14 PAUSE → 循环
```

vs 不做 R14:
```
task A 跑 100 iter → COMPLETED(干净终态) → 烧 400 sim 0 PASS
  → 用户/未来 orchestrator 接班开 task B → 烧剩 500 sim
  → 当天配额无浪费
```

`COMPLETED` 是干净终态,自动化好接;`PAUSED` 是"等人处理"中间态,留死结。

**R14 真正价值依赖 orchestrator**(auto-launch + 换 region/dataset/hypothesis + 配额调度)。orchestrator 上线后,R14 PAUSE 触发"换 task"决策 → 早释放配额给新 task → 正目标。

**所以 R14 进入 [orchestrator-plan](./orchestrator_plan_2026-05-29.md) 范围,本 plan 不做**。

---

## 3. 串行代码三类清单 (mining_tasks.py 2186 行,实测行号 — 已含 stop_reason ship + R1b typed retire)

### 🔴 串行独有 — Phase C 删 (~590 行)

| 函数 | 行号 (实测 2026-05-29 晚) | 备注 |
|---|---|---|
| `_run_one_round_inline()` | 1000–1165 | 串行单轮主体 |
| `_run_flat_iteration()` | 1768–2186 | 串行主循环 + 400 行恢复/cursor/client refresh |
| `_refresh_brain_client()` | 57–71 | **保留** — 流水线 `_run_flat_iteration_pipeline` 也用它作 `BrainClientRefresher.refresh_fn` |
| `run_mining_task` FLAT dispatch | mining_tasks.py 顶部 dispatch 块 | 删 dispatch 分支,直接走 pipeline |
| `_rebuild_flat_db_session()` | 1211–1235 | **Phase D 删**(串行专属,5 处全在串行 + 注释)— 流水线靠 heartbeat-abort + 重 dispatch fresh pool 替代 |

### 🟢 `_run_flat_iteration_pipeline` 直接调 — 必须保留

| Helper | 行号 | wrapper 内调点 |
|---|---|---|
| `_get_datasets_to_mine()` | 701–748 | producer 内 dataset fetch |
| `_get_operators()` | 796–830 | producer 内 ops fetch |
| `_verify_cascade_ownership()` | 1166–1208 | producer + finalize 内 |
| `_pick_dataset()` | 1275–1288 | producer 内 ε-greedy(流水线专属 wrapper,串行 0 命中) |
| `_prepare_round_fields()` | 987–998 | producer 内 |
| `_build_dataset_pool()` | 975–985 | producer 内 |
| `_refresh_brain_client()` | 57–71 | `BrainClientRefresher.refresh_fn` 用 |
| `_task_delay()` | 35 | bandit cell-stats join |
| `_pipeline_op_timeout()` / `_pipeline_heartbeat_timeout()` | 119 / 104 | runner 超时配置 |

### 🟢 间接依赖 — 链式被流水线调,保留(v3 实测)

`_prepare_round_fields:991-996` 链式调:
- `_get_dataset_fields()` (832-898) ← :991
- `_get_universal_pv_fields()` (900-933) ← :995
- `_merge_field_pools()` (935-960) ← :996

`_build_dataset_pool:978-983` 链式调:
- `_get_active_level()` ← :978
- `_get_complementary_datasets()` (750-794) ← :981

`_pick_dataset:1286` 链式调:
- `_pick_diverse_dataset()` (1250-1265) ← :1286 (**生产唯一调用点**)
- `_dataset_mean_margin()` ← :1287

### ⚪ Dead code — Phase D 直接删

- ~~`_pick_least_covered_dataset()`~~:**已删** (2026-05-29 晚,Phase D 提前)。生产 0 命中,只被 test 调,早期 Option C step-1 使用后被 step-3 `_pick_diverse_dataset` 取代

### 📝 docstring / 注释引用清理 (Phase C 顺手,一次性)

`grep -rn "_run_one_round_inline\|_run_flat_iteration" backend/ 2>&1 | grep -v mining_tasks.py` 命中 11 处全是注释/docstring(原 12 处,`r1b_typed_pipeline.py:24` 随模块 retire 已删),删串行后是 stale 引用:
- `backend/agents/graph/state.py:342,359`
- `backend/agents/graph/workflow.py:521,542`
- `backend/agents/graph/nodes/generation.py:459`
- `backend/agents/graph/nodes/g5_persistence.py:5,80`
- `backend/agents/graph/nodes/r1b_persistence.py:14,21,23,94`
- `backend/agents/graph/nodes/persistence.py:1117`
- `backend/agents/mining_agent.py:278`
- `backend/adapters/brain_adapter.py:644`
- `backend/celery_app.py:70`
- `backend/routers/ops.py:1014`
- `backend/config.py:279`

`_run_flat_iteration_pipeline` 引用 (Phase C 重命名时同步改):
- `mining_tasks.py:1813` 注释、`mining_tasks.py:1821` 调用
- `agents/pipeline/__init__.py:8` docstring
- `agents/pipeline/producer.py:13` docstring
- `agents/pipeline/feedback_g5.py:22` docstring
- `agents/pipeline/feedback_r1b.py:6` docstring
- 监控脚本 `scripts/_poll_session.py` / `scripts/_watch_shadow.py`:0 命中(走 `task.config`,不 import 函数)

### 🟢 完全共享 — 不动

- LangGraph 节点 `backend/agents/graph/nodes/*.py`
- `BrainAdapter._acquire_sim_slot()`
- `_with_timeout()` (`backend/agents/pipeline/runner.py:47`)
- persistence (`agents/graph/nodes/persistence.py` / `r1b_persistence.py` / `g5_persistence.py`)
- bandit / dataset_selector / evaluation 阈值 (`config.py`)
- `BRAIN_AUTH_CIRCUIT`
- `flat_cursor` / `runtime_state` 续跑 (流水线 producer 1492-1503 自己 session 持久化)
- ops endpoint: `/ops/start-flat-session`(含 `enable_pipeline`)、`/ops/flat-sessions/{id}/resume`、`/ops/sim-slots`
- 前端 `TaskManagement.jsx:445` 灰度入口、`frontend/services/api.js:680` startFlatSession
- 监控脚本 `_poll_session.py` / `_watch_shadow.py`

---

## 4. 灰度路径 — per-session opt-in

`POST /ops/start-flat-session` payload 加 `enable_pipeline: true` → 写 `task.config["enable_sim_pipeline"]=True`。

监控:`scripts/_watch_shadow.py`(专为 shadow session 写)。失败回滚只需 STOP 该 task。

---

## 5. Phase A/B/C/D 执行顺序 (gated)

### Phase A — no-op (0d) ← v3 修订 2

R14 推迟到 orchestrator-plan(见 §2.5)。Phase A 没剩工作。

**Phase B soak 前置**:`ENABLE_TASK_STOP_LOSS=False` 强制(.env 或 config)。流水线现行止损双层够:
- `FLAT_CONTINUOUS_MAX_ITERATIONS=100`(单 task 硬上限)
- `quota_guard` celery beat(全局 900 sim/天)

### Phase B — 三级 soak gate (v3 重写)

**B.1 — 6h smoke (gate: 流水线基本能跑)**

- [x] ~~前置 0.2d 加 auth-circuit dispatch 告警~~ → **替换为已 ship**:`_run_flat_iteration_pipeline` 写 stop_reason + 前端显示(见 §6 风险点 #3,实证 v3 plan 原方向错了)
- [ ] 启动 1 个 task,`enable_pipeline=True`,USA + mid-size dataset 池(~5 datasets),`ENABLE_TASK_STOP_LOSS=False` 不干扰
- [ ] 验收:
  - [ ] ≥10 PASS alpha (证明 generation → simulation → persist → evaluate 全链路)
  - [ ] sim throughput ≥ 串行 baseline 的 70% (拿同等条件历史 task 比)
  - [ ] 0 heartbeat-abort 触发 (若触发 → 仍有未捕获 freeze 类,停下修)
  - [ ] persister errors / slot_timeouts < 5
- [ ] 失败 → 修复后重跑 6h,不进 B.2
- [ ] `scripts/_watch_shadow.py` 全程跑,日志归档 `docs/shadow_session_smoke_{date}.md`

**B.2 — 24h soak (gate: 长时稳定性 + 主动场景)**

- [ ] 同 task 续跑到 24h(或新 task,看 B.1 结果)
- [ ] 验收:
  - [ ] ≥40 PASS alpha
  - [ ] 主动触发 1 次 pause-resume:`POST /ops/flat-sessions/{id}/pause` → 等 ≥10min → `POST /ops/flat-sessions/{id}/resume` → cursor 续跑无漂移
  - [ ] 主动模拟 1 次 watchdog 移交:强 kill worker 进程 → watchdog 抢锁 → 新 worker 接管 → 旧 run 关 STOPPED + task 继续(`4c2b4b9` 修复路径)
  - [ ] 0 heartbeat-abort
  - [ ] 0 sim 卡死 (refresher 期间无连接泄漏:检 httpx pool stats)
  - ~~Phase A R14 验证~~ (R14 推迟,见 §2.5 + orchestrator-plan)
- [ ] 日志归档 `docs/shadow_session_24h_{date}.md`

**B.3 — 48h 灰度 (gate: 产能 + 实战适配)**

- [ ] B.2 通过后,**手动**启 2-3 个并行 shadow task(不同 region/dataset),共 48h — 目的是验证流水线产能上限 + 共享资源(BRAIN slot / asyncpg pool / heartbeat supervisor)无干扰,**不是 orchestrator 仿真**(orchestrator 是独立 plan,本 plan 不涉及)
- [ ] 验收:
  - [ ] ≥1 SUBMIT 候选(经 marginal_analysis 推荐 SUBMIT)
  - [ ] 跨 task 总 sim throughput ≥ 串行 baseline 90%
  - [ ] 0 fail-stop 事件
- [ ] 通过 → 进 Phase C

### Phase C — 删串行 (gate: B.3 全过)

- [ ] 删 `_run_one_round_inline()` (1060–1233)
- [ ] 删 `_run_flat_iteration()` (1797–2213)
- [ ] 重命名 `_run_flat_iteration_pipeline` → `_run_flat_iteration` + 同步替换:
  - `mining_tasks.py:1821` 调用 + 1813 注释
  - `agents/pipeline/__init__.py:8` / `producer.py:13` / `feedback_g5.py:22` / `feedback_r1b.py:6` docstring
- [ ] mining_tasks.py:1817-1823 dispatch 简化:删 `_pipeline_on` 判断,直接 `return await _run_flat_iteration(...)`
- [ ] 清理 §3 列的 12 处 docstring/注释 stale 引用 (state.py / workflow.py / generation.py / g5_persistence.py / r1b_persistence.py / persistence.py / r1b_typed_pipeline.py / mining_agent.py / brain_adapter.py / celery_app.py / routers/ops.py / config.py)
- [ ] 测试影响处理 (2026-05-29 晚实测 + R1b typed retire 后更新):
  - 删 `test_flat_round_failure_recovery.py` (串行 `_rebuild_flat_db_session` 专属)
  - 保留 `test_flat_pipeline_dispatch.py` 但简化 dispatch 测试(只剩一条路径)
  - **R1b typed 4b 已 retire**(2026-05-29 晚 ship,见 commit history):dormant 路径 + 流水线 0 接入 + `.env` 永远不会分配 variant=3 → 4 个相关测试 + 整 module + wrapper + config 项 + ops/feature_flag_service 引用全删
  - **剩余 2 个测试仍触串行 `_run_one_round_inline`**(实测,从 4 降到 2):
    - `test_external_call_deadlines.py:119` — asyncio.wait_for 超时行为 → 候选 (a) 行为可映射到 pipeline `op_timeout`
    - `test_r1b_round_boundary_wire.py:45,141,227` — R1b round 边界 → 候选 (b) `@pytest.mark.skip(reason="serial round model deprecated")`,round 模型耦合深
- [ ] regression baseline 重建,0 漂移
- [ ] CLAUDE.md / `backend/agents/IMPROVEMENT_ANALYSIS.md` 同步,删串行段落
- [ ] memory 更新:[[project_flat_session_greenlet_timeout_fix_2026_05_25]] / [[project_split_producer_first_live_freeze_2026_05_28]] 标 deprecated

### Phase D — cleanup (≥7d 观察后)

- [ ] 删 `_rebuild_flat_db_session()` (Phase C 后实测 grep 行号) — heartbeat-abort 替代已稳
- [ ] 删 `_pick_least_covered_dataset()` (Phase C 后实测 grep 行号) — dead code
- [ ] 删 `_refresh_brain_client()` (57-71) — 验证 `BrainClientRefresher` 接管后无遗漏
- [ ] 共享 helpers 集中到 `backend/agents/pipeline/dataset_prep.py` (可选,看代码可读性)
- [ ] 低优 gap 视需要补:auth-circuit park-and-retry / trace iteration_offset
- [ ] ops 看板 / 前端"串行 vs 流水线"指标(如有) 合一

---

## 6. 风险点

1. ~~R14 触发节奏决策~~ (推迟,见 §2.5 + orchestrator-plan)
2. **流水线 ≥24h soak 未验证 (R-A)**:`2f3dd58` 结构修复是 commit message claim,实战 0 数据。Phase B.1 是 first real soak,失败概率不可忽略 → 留 buffer
3. ~~BRAIN auth-circuit 反复 dispatch 循环~~ — **实证修正 (2026-05-29 晚)**:watchdog 只复活 `status=="RUNNING"` (session_watchdog.py:71),流水线 stop-on-open → finalize 走 `task.status=COMPLETED`,**没有自动 dispatch 循环**。真实风险是**静默失败/用户体验**:task 5 秒 COMPLETED + 0 alpha,用户看不出原因(auth-circuit OPEN 还是 max_iters 达成),手动重启又复刻同样静默 COMPLETED → 凭感觉循环烧无谓 BRAIN auth + LLM 请求。**已 ship B 方案**:`_run_flat_iteration_pipeline` finalize 写 `task.config["last_stop_reason"]` + `run.runtime_state["stop_reason"]`,前端 TaskManagement/TaskDetail 显示退出原因 tag(`auth_circuit_open` / `heartbeat_abort` 醒目)
4. **per-session 灰度 task 选择**:不选关键产能 task 做实验,B.1 用单 task 中小池
5. **`_pick_dataset` 内部依赖** (mining_tasks.py:1343-1356):唯一调 `_pick_diverse_dataset` (1354),删串行不影响
6. **`_rebuild_flat_db_session` 删除时机** (Phase D 而非 Phase C):heartbeat-abort 已 prod 验证 1 周以上 后才删,留 Phase C/D 之间 7d 观察期
7. **`run_mining_task` cascade lock 224-322 takeover-token 保留**:watchdog 移交还需要,只删串行专属分支
8. **B.2 主动模拟 watchdog 移交**有破坏性(kill 进程),只在 shadow task 上做,不上生产

---

## 7. 不在范围

- 流水线骨架内部设计(已 ship,见 `062075f` 起 25 commit)
- ONESHOT 路径
- 系统 cascade 退役(已完成,见 [v26_retrospective])
- 优化闭环 plan(独立 work,见 `docs/optimization_closure_plan_v1_2026-05-28.md`)

---

## 8. 参考

- 流水线 ship 路径:`062075f` → … → `2f3dd58`(25 commit) — `git log --oneline backend/agents/pipeline/ backend/tasks/mining_tasks.py` 查全列
- 串行修复链(历史价值):`dc7c8e5`(greenlet 暴毙)/ `d650222`(sim 永挂)/ `36bc39b` / `855c709`
- R14 service:`backend/services/task_stop_loss_service.py` `check_should_pause` / `apply_stop_loss_decision`
- 实测验证依据:本对话 2026-05-29 grep + Read,行号截至 mining_tasks.py 2215 行
- v1/v2 plan(废):同文件 git history,留 audit trail
