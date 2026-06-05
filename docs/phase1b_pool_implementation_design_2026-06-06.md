# Phase 1b 实现设计 checkpoint — HG/S/E 常驻池

> 状态:**设计稿,待用户批准后分阶段实现**(task #5)。地基:grounding wf_e302605a(4 路对真实代码取证)。
> 目标:建 3 个常驻 worker 池(HG 假设+生成 / S simulate / E evaluate),DB 持久队列两事务 claim/lease,Redis 控制面,Python supervisor。**flag `ENABLE_POOL_PIPELINE` OFF,与 FLAT 并行**,零生产影响直到 1c-flip。复用既有节点逻辑(verbatim),只新建编排/控制/恢复骨架。

## 1. 复用面(grounding 确认 verbatim 可用)

| 池 | 复用 API | 来源 |
|---|---|---|
| **HG** | `MiningWorkflow.run_hypothesis(state,config)`(rag→distill→hypothesis,含新 `HypothesisEnricherOrchestrator`)→ `run_codegen`(codegen→validate→[self_correct])→ `pending_alphas[].is_valid` | workflow.py |
| HG→S 交接 | `producer._sim_ready_payload(gen_state, candidate)` 切单候选 | pipeline/producer.py |
| **S** | `build_consumer_stages(workflow).simulate` 或直接 `run_simulate` | consumer.py / workflow.py |
| **E** | `run_evaluate` → `compute_verdict_from_signals` + `CorrelationService.get_with_fallback` | workflow.py / evaluation.py / correlation_service.py |
| **持久化** | `_incremental_save_alphas`(PASS→alphas)/ `_incremental_save_failures`(非 PASS→alpha_failures + metrics)/ `build_persister` | persistence.py / pipeline/persister.py |
| 契约 | `Candidate`/`SimResult`(pipeline/types.py)· `MiningState`(state.py)· `HypothesisIntent`/`CandidateQueue`(Phase 0 models) | — |

**关键**:run_hypothesis/run_codegen/run_simulate/run_evaluate 各自的 node 内部**自开 ephemeral session**(F1 契约:N 并发 consumer 永不共享 session)。池 worker 只需 hydrate MiningState + 调这些 run_*,**不碰节点内部**。

## 2. 架构(3 池 + 控制面 + 恢复)

```
                      ┌─────────────── scheduler beat (cron) ───────────────┐
                      │ weighted_choice(mining_weight) + pg advisory lock    │
                      │ → INSERT hyp_intent(PENDING)                          │
                      └──────────────────────────────────────────────────────┘
   hyp_intent(PENDING) ──claim──▶ [HG pool ×N_hg] ──emit──▶ candidate_queue(PENDING_SIM)
                                   run_hypothesis+run_codegen
   candidate_queue(PENDING_SIM) ──claim──▶ [S pool ×K_s=2] ──▶ (PENDING_EVAL) + sim_result
                                   run_simulate  (占 brain:concurrent_sims + budget:sims)
   candidate_queue(PENDING_EVAL) ──claim──▶ [E pool ×K_e=1] ──▶ (DONE) + verdict
                                   run_evaluate → PASS→alphas / 非PASS→alpha_failures
   ┌── lease-recycle beat (cron):CLAIMED/SIMULATING/EVALUATING ∧ lease<now → attempts<cap 回 PENDING 否则 FAILED
   └── pool_supervisor (常驻父进程):Popen-respawn HG/S/E,drain-aware,health 心跳
   控制面(Redis):pool:{hg,s,e}:drain · budget:sims:YYYYMMDD · budget:tokens:YYYYMMDD · brain:concurrent_sims
```

## 3. 模块结构(新建 `backend/pool/` 包)

| 文件 | 内容 |
|---|---|
| `pool/queue.py` | **两事务 claim/lease 原语**:`claim_one(stage, worker_id)`(TXN-1 SELECT FOR UPDATE SKIP LOCKED + UPDATE CLAIMED + COMMIT)· `renew_lease`(心跳)· `complete`/`fail_or_retry`(TXN-2)· `recycle_expired`(lease-recycle)· **stage 常量模块**(HG in-flight=`CLAIMED`,S=`SIMULATING`,E=`EVALUATING`,匹配 Phase 0 partial index WHERE) |
| `pool/hydrate.py` | `hydrate_hg_state(hyp_intent)` · `hydrate_candidate_state(candidate_queue + parent config_snapshot)` → 单候选 MiningState(含 role-snapshot 一等列 + context JSONB) |
| `pool/hg_worker.py` | HG loop:claim hyp_intent → hydrate → run_hypothesis+run_codegen → 每 is_valid 候选 `_sim_ready_payload` → INSERT candidate_queue(PENDING_SIM)→ intent DONE |
| `pool/s_worker.py` | S loop:claim PENDING_SIM → hydrate → run_simulate(占 slot+budget)→ UPDATE sim_result + stage=PENDING_EVAL |
| `pool/e_worker.py` | E loop:claim PENDING_EVAL → run_evaluate → verdict → PASS/PROV→alphas + can_submit refresh / 非PASS→alpha_failures.metrics + flush trace → stage=DONE |
| `pool/budget.py` | `budget:sims` INCR(仅成功 POST 后)+ `budget:tokens` 三段 reserve/correct(读 `POOL_NODE_TOKEN_RESERVE`)+ `llm:concurrent` 信号量 |
| `pool/drain.py` | `is_draining(pool)` · `set_drain`/`clear_drain` · purge PENDING→PURGED(不碰 CLAIMED/SIMULATING) |
| `pool/scheduler.py` | `schedule_intents()`:weighted_choice(DatasetCellStats.mining_weight)+ per-region pg_advisory_xact_lock → INSERT hyp_intent |
| `pool/supervisor.py` | Popen-respawn 父进程:启 N_hg HG + K_s S + K_e E;读 drain 优雅停(drain SET 则 park 不重启)+ health 心跳 + backoff/crash 计数 |
| `tasks/pool_tasks.py` | Celery beat:`run_lease_recycle`(每 1-2min)+ `run_pool_scheduler`(按节奏)— **注册进 celery_beat_schedule** |
| `routers/ops.py`(改) | `POST /ops/pools/{name}/drain` · `/resume` · `GET /ops/pools/status`(队列深度 + 在飞 lease) |

## 4. claim/lease 契约(必精确)

```python
# TXN-1: claim(释行锁后再跑 node)
async def claim_one(stage, worker_id, lease_sec) -> Optional[row]:
    async with AsyncSessionLocal() as s:           # 独立 session
        async with s.begin():
            row = (await s.execute(
                select(Model).where(Model.stage == stage)
                .order_by(Model.id).limit(1)
                .with_for_update(skip_locked=True))).scalar_one_or_none()
            if row is None: return None
            row.stage = INFLIGHT[stage]             # CLAIMED / SIMULATING / EVALUATING
            row.claimed_by = worker_id
            row.lease_expires_at = utcnow() + lease_sec
            row.attempts += 1
        # COMMIT 已发生(begin() 块退出)→ 行锁释放
    return row    # 此后跑 node,绝不持开放事务/行锁(避 idle-in-txn,gotcha #12)

# 心跳:长 node(sim 30-90min)期间另起 task 周期 renew_lease(row.id)
# TXN-2: 结果写在新 session;complete(stage=next) 或 fail_or_retry(attempts<cap→回 PENDING_*,否则 FAILED poison-pill)
# lease-recycle beat:UPDATE ... WHERE stage IN INFLIGHT AND lease_expires_at < now → 回 PENDING_* / FAILED
```
**守则**:claim 与 node 执行**分属不同 session**;claim 的 COMMIT 必在任何 node await 之前(gotcha #12 idle-in-txn / [[reference_flat_idle_in_txn_lock_leak_2026_06_04]])。lease > per-op timeout(允许排队,gotcha #8)。

## 5. budget / slot / quota 接线(精确点 + 足枪)

- **brain:concurrent_sims**:run_simulate 内部已正确占/释(brain_adapter.py acquire/release)——池 worker **不重复占**(consumer NO-OP acquire,沿用 FLAT 契约)。
- **budget:sims:YYYYMMDD**:**仅成功 POST 后 INCR**——hook 进 `brain_adapter.py:756-806` simulate_alpha 成功分支(200/201/202+Location);simulate_batch 按 N。**排除** slot-timeout/429/auth-fail/pre-POST/dedup-skip(`is_simulated≠BRAIN-truth` 坑,曾致 24% 虚高早停,终审 #6 + [[reference_is_simulated_not_brain_truth]])。超 `BRAIN_DAILY_SIMULATE_LIMIT` → SET drain(非 DB pause)。
- **budget:tokens** + `llm:concurrent`:下沉 llm_service.call;三段=软门 + p95/p99 悲观预扣(`POOL_NODE_TOKEN_RESERVE`)+ real−reserved 校正。`POOL_TOKEN_BUDGET_PER_DAY`(8M)超 → SET hg drain。**勿动 `MAX_TOKENS_PER_DAY`**(macro 占用)。
- **quota_guard effector → drain**:`session_watchdog.py` 现 `UPDATE MiningTask WHERE status='RUNNING'` 在池世界静默 no-op → 改 SET pool drain key;count 源自 budget:sims。
- **worker_process_init guarded reset**(终审 #5):celery_app.py 无条件删 `brain:concurrent_sims` → 常驻池下重启清零兄弟在飞计数 → 越 cap。改:仅 `pool:workers:alive` 心跳注册表空才清。

## 6. scheduler + supervisor

- **scheduler**(beat):`weighted_choice(DatasetCellStats.mining_weight)`(binary can_submit reward,verbatim)选 (region,dataset)→ **per-region `pg_advisory_xact_lock`**(仿 hypothesis_service.py:508)串行化 → INSERT hyp_intent PENDING。终止内禀(队列抽干)——无 per-session daily_goal。**不接 DatasetSelector UCB**(2026-05-23 双选择器锚死陷阱)。
- **supervisor**(§6 #4 决定 = Python Popen-respawn):新 `pool/supervisor.py` 常驻父进程,`subprocess.Popen` 启 N_hg HG + K_s=2 S + K_e=1 E(各 `python -m backend.pool.{hg,s,e}_worker`);循环 poll 子进程,死则按 backoff 重启(读 `pool:{name}:drain`:SET 则 park 不重启);写 `pool:workers:alive` 心跳(供 worker_process_init guarded reset)。run.bat/run.sh 改启 supervisor(取代 cmd /k 各 worker)。**beat 只能杀不能拉**(gotcha #11)→ 拉起全归 supervisor。

## 7. sizing(§6 #6 决定)+ 池数

- **K_s=2 / K_e=1 + 留 1 槽给 opt/auto-submit**(USER=3 槽现实)。CONSULTANT(80)升级用公式 K_s=min(slot/3,…)/K_e=max(2,slot−K_s−1)。
- **N_hg=1**(初始):HG 是 LLM-bound、一 intent 扇出 N 候选;token 预算 + `llm:concurrent` 限并发。Phase 1b 测 candidate_queue PENDING_SIM 队列深度,若 HG 喂不饱 S 再加。

## 8. 必守 gotchas(grounding 12 条,建时遵循)

1. **F1 session 契约**:run_* 内部各自 ephemeral session,池 worker 不注入共享 session。
2. **slot 泄漏**:claim_simulate_slot NX 释放 finally-shield;worker 启动 purge 泄漏槽([[reference_brain_sim_slot_leak_cascade_2026_05_31]])。
3. **budget 时序**:仅成功 POST 后 INCR(§5)。
4. **drain 生命周期**:PENDING→CLAIMED→DONE/FAILED/PURGED 终态不再 claim;孤儿 CLAIMED 待 lease-recycle。
5. **advisory lock + slot claim race**:soft-fail(producer 让出,下次心跳重试),无硬死锁。
6. **hypothesis_id 传播**(LEVEL 0→2):scalar 先,else list[0](persister.py `_resolve_hypothesis_id` 同款),否则 alphas 全 NULL 压 KB 学习。
7. **beat 不能拉 worker** → supervisor 职责。
8. **lease > per-op timeout**(允许排队,避 slot 雪崩)。
9. **idle-in-txn**:claim COMMIT 必在 node await 前([[reference_flat_idle_in_txn_lock_leak_2026_06_04]])。
10. **HG in-flight stage 字面量 = `CLAIMED`**(匹配 Phase 0 `ix_hyp_intent_claim` partial WHERE,§6.5)。
11. **role-snapshot 一等列**(终审 #7):S 读 effective_default_test_period、E 读 effective_sharpe_submit_min;NULL→User 默认。
12. **worker_process_init guarded reset**(终审 #5)。

## 9. build 子阶段(降风险,逐段验证,全 flag-OFF)

- **B1 claim/lease 原语 + stage 常量**(`pool/queue.py`)+ 单测(SQLite skip_locked 退化 OK,PG 集成测真并发 claim 无重领)。
- **B2 hydrate**(DB 行 → MiningState)+ **Phase 1c gate 预演**:hydrated 行 vs model_copy 整-state run_evaluate PASS+FAIL byte-等价测(§2 plan)。
- **B3 S 池 + E 池 loop**(复用 run_simulate/run_evaluate/persister)+ budget:sims 接线 + drain check;单 candidate 端到端(手插一行 candidate_queue → S → E → alphas)。
- **B4 HG 池 loop**(run_hypothesis+run_codegen → emit candidate_queue)+ token budget;单 intent 端到端(手插 hyp_intent → HG → N candidate_queue)。
- **B5 scheduler beat + lease-recycle beat**(注册 celery_beat_schedule)+ quota_guard→drain + worker_process_init guarded reset。
- **B6 supervisor**(Popen-respawn)+ run.bat/run.sh 改造 + drain/RESUME endpoint + ops 状态页。
- **B7 端到端 soak**(flag-OFF 手动启 supervisor,scheduler 喂 N intent,观察 HG→S→E→alphas 抽干 + lease-recycle + drain/resume),退出判据:无行卡 in-flight 超 lease。
- 每段:单测 + 集成测(PG)+ 全 unit 0 净新失败 + 对抗审查。

## 10. 待你拍(开工前)

1. **模块包名**:`backend/pool/`(推荐)vs 散在 `backend/tasks/`。推荐独立包(清洁、易测、Phase 1c 不缠 FLAT)。
2. **N_hg 初值**:1(推荐,先测队列深度)vs 2。
3. **worker 进程入口**:`python -m backend.pool.{hg,s,e}_worker`(推荐)vs Celery 专用 queue。前者纯常驻进程(plan 「非 per-task Celery dispatch」),beat 仍用 Celery。
4. **先建顺序**:B1→B7(推荐,claim/lease 是地基)。或先 B3 S/E(复用现成,最快见单候选端到端)再 B1 强化?推荐 B1 先(其余都依赖 claim)。
5. **B2 的 Phase-1c byte 等价 gate 现在做还是 1c**:推荐 B2 就预演(早发现 hydration 漏字段),1c 再正式 gate。
