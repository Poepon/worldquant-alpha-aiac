# 实施计划:挖掘流水线(producer-consumer)— 执行级(2026-05-27)

承自架构设计 `docs/delay0_sim_pipeline_design_2026-05-27.md`(v1 设计 + v2 串行化-DB + v3 槽 regime)。
本文件是**执行清单**:精确的子阶段 / 文件 / 函数 / flag / 测试 / 回滚。

决策:**先 ship Option A(已完成 `9da03db`),流水线确定要建,不等 Consultant。**
原则:flag default OFF;flag OFF 时既有路径**字节不变**;每子阶段独立可提交 + 回归 0 漂移 + fresh-agent review。

## 核心架构(定稿)
```
Producer ×P(各自 session_P)─► work_queue ─► Consumer ×N(零 DB)─► persist_queue ─► Persister(session_C)
  选 dataset(bandit)+RAG/DISTILL/         候选      acquire 槽→BRAIN sim→            串行写 trace+alpha+bandit+hyp-link
  HYPOTHESIS/CODE_GEN/VALIDATE/self-correct           evaluate 计算(纯内存)            连续排空(每 result/微批)
```
- **碰 DB 的只有 P 个 producer + 1 个 persister,各自单所有者 session** → 无并发 op、无 greenlet-poison。
- **N 个 consumer 零 DB**,复用现成 `BrainAdapter._acquire_sim_slot/_release_sim_slot`(Redis 跨进程、角色感知 3/80,无需改)。
- N = `_current_sim_slot_limit()`(自动 3/80);P 随槽数 scale(Sub-phase 2)。

## 关键集成点(已核实)
| 触点 | 位置 | 用法 |
|---|---|---|
| session 工厂 | `backend/database.AsyncSessionLocal` | 每协程 `async with AsyncSessionLocal() as s` |
| FLAT 循环(改造入口) | `mining_tasks._run_flat_iteration:1257` | 顶部按 flag 分支到 `run_pipeline_session` |
| 一轮逻辑(参照拆分) | `mining_tasks._run_one_round_inline:1011` | 拆成 gen-子图(producer)+ sim/persist(consumer/persister) |
| poisoned-session 重建 | `mining_tasks._rebuild_flat_db_session:1230` | producer/persister 各自的重建 |
| sim 槽 | `brain_adapter._acquire_sim_slot/_release_sim_slot:189/221` | consumer 直接复用,**零改动** |
| alpha 落库 | `persistence._incremental_save_alphas(db_session=...)` | 已参数化(F5),persister 传自己 session |
| trace 写入 | TraceService(待定位) | **buffered trace**:gen/consume 攒内存 dict,persister 落库 |

## Sub-phase 0 — 串行化-DB 基座 + 流水线骨架(flag OFF,功能等价)
目标:把 producer/consumer/persister 三类协程 + 双队列 + 单所有者 session 立起来,作为 round 循环的**替代路径**藏在 `ENABLE_SIM_PIPELINE`(default OFF)后。本阶段先做到**功能等价**(产出与 round 路径一致),只验证管道正确性,不改挖掘行为。

1. **config / flag**(`backend/config.py`)
   - `ENABLE_SIM_PIPELINE: bool = False`
   - `SIM_PIPELINE_QUEUE_MAXSIZE: int = 0`(0=自动=2×槽上限)
   - `SIM_PIPELINE_PRODUCER_COUNT: int = 1`(Sub-phase 2 才 scale)
   - `SIM_PIPELINE_PERSIST_EVERY: int = 1`(persister 每 N 个 result 落一批;1=逐个)
2. **新模块 `backend/agents/pipeline/`**
   - `runner.py::run_pipeline_session(db, task, run, brain, *, lock_key, lock_token)`:建 work_queue/persist_queue,`asyncio.gather(producer×P, consumer×N, persister)`,各协程独立 session(producer/persister)。负责 cursor / ownership / stop 检查(沿用 `_verify_cascade_ownership`)。
   - `producer.py`:loop 选 dataset(bandit 加权,沿用 `_ds_weight_map` 逻辑)→ 复用 generation 节点产**已验证**候选 → 每个候选连同 context 切片(MiningState 字段 + hypothesis_id + dataset + sim 设置 + bandit arm + **buffered trace records**)`await work_queue.put(...)`(队列满则阻塞=背压)。delay 穿线沿用。
   - `consumer.py`:**无 db**。loop pull → `_acquire_sim_slot` → `brain.simulate` → node_evaluate **计算路径**(verdict 纯内存)→ `persist_queue.put(SimResult)` → `_release_sim_slot`(finally 保证释放)。
   - `persister.py`:**单协程**,own session。loop pull SimResult → 写 buffered trace + `_incremental_save_alphas(db_session=own)` + bandit reward 回填 + hypothesis link → commit。连续排空。
3. **buffered trace**:定位 TraceService;producer/consumer 把每步 trace 攒成 dict 列表挂在候选/result 上;persister 用 own-session 落库(新增 `add_steps_buffered(records)` 或逐条 add_step)。
4. **整合**:`_run_flat_iteration` 顶部 `if settings.ENABLE_SIM_PIPELINE: return await run_pipeline_session(...)`。
5. **测试**:队列背压(满则阻塞)、consumer 槽 acquire/release 配对无泄漏(mock brain)、producer dataset 选择、persister 真 aiosqlite 顺序写 + read-back、consumer 零-DB 断言(传 None session 不报错)。`test_suite.py --all` 回归 0 漂移(flag OFF=既有路径不变)。

### Sub-phase 0 开放问题(已验)
- [x] **`node_evaluate` 核心 verdict 计算 DB-free。** 仅 3 处 DB 触点,全是可延迟的软失败副信道:`_pr06_lookup_mutated_hypothesis_ids`(evaluation.py:41,R1b 归因,`db=None` 自开 session、出错返空集)、Q10 prescreen log(1529,`_q10_db`)、R1a attribution log(3278,`_r1a_db`)。→ **consumer 零-DB 可行**:这三者延迟到 persister(软失败,延迟无正确性损失)。真不变量 = "两协程不共享一个 session"(独立短 session 并发也安全,受 asyncpg pool 限)。
- [x] **TraceService 在 `backend/agents/services/trace_service.py`。** buffered 写:gen/consume 攒 dict,persister own-session 落库。
- [ ] greenlet:确认 producer/persister 的 await **永不在 asyncpg-op 中途被 cancel**(给它们的 wait_for/cancel 包排空逻辑)——build 时落实。

## Sub-phase 1 — 连续流(去 round 边界)+ round 机制迁移
- 早停 `should_stop_early`/`round_history` → 会话级停止判据。
- `_record_round_summary`(ROUND_SUMMARY)→ 周期性批次摘要,**"批次"= persist 的每 K 个 = UI/trace 的 iteration 单位**。
- G5 round-end crossover / R1b/G5 pending carry → producer 侧 carry。
- bandit round-end 回填 → 已在 persister 逐 result 回填。
- batch_dedup → producer 侧滚动去重。
- **UI**(`TaskDetail.jsx:229-268` 按 `trace.iteration` 分组)→ persister 落库时盖 iteration/batch id。
- **F4 client-refresh**:`_refresh_brain_client` 从 per-round 改 per-T-seconds + in-flight 守卫(仅无 sim 在飞时 refresh,或每 consumer 独立 client)。

## Sub-phase 2 — 80-slot scale(Consultant,条件性)
- `SIM_PIPELINE_PRODUCER_COUNT` 自动随槽数 scale(80 槽 ≈ 5 producer,按 sim_time/gen_time/batch 算)。
- 抬 `MAX_SIMULATIONS_PER_DAY` / `MAX_TOKENS_PER_DAY`(为 USER 设,80 槽下 0.4 sims/s=34,560/天会撞)。
- producer 加配额/成本背压(日配额将尽则节流)。
- **仅在 Consultant 升级后(`ENABLE_BRAIN_CONSULTANT_MODE`)才验 80 槽**;3 槽期不验。

## 灰度 / 回滚
- flag OFF → 在 delay-0 单 session shadow 验(util↑ + 吞吐↑ + 质量 0 回归)→ 再 flip。
- 任何阶段回归:flag OFF 立即回退既有 round 路径(字节不变)。

## 风险与缓解
| 风险 | 缓解 |
|---|---|
| 砸刚稳定的挖掘热路径 | flag default OFF;OFF 时既有路径字节不变;独立模块 |
| 重开 greenlet-poison | 单所有者 session;producer/persister await 不中途 cancel |
| 崩溃丢已 sim 未落库结果(白烧配额) | persister 连续排空(非 end-of-session) |
| 80 槽下 producer 喂不动 | Sub-phase 2 并行 producer;3 槽期不需要 |
