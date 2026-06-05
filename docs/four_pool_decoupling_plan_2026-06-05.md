# 挖掘管道清洁解耦架构 plan **v4** — HG / Simulate / Evaluate 三池(干净重建,抛弃历史包袱)

> **changelog**:v1(1+2,4 池)→ 审查 REVISE → v2(1-alone,4 池)→ 确认审查 REVISE(H→G 证伪)→ v3(3 池,最小改动/FLAT 共存)→ **用户要「干净架构、抛弃包袱、复用/重构/删除」→ 清洁清点 workflow(wf_915f9ddd,6 agents 核对真代码)→ v4:从 v3 的「最小改动绕 legacy」转为「干净目标 + 主动删除包袱」**。
> **用户已锁**:3 池(HG/S/E)· DB 持久队列 · **纯前向 Phase 1**(R1b/G5/R5 反馈翻 OFF + 删机器,Phase 2 异步 reconcile-beat 重接)· **删 ONESHOT**(手动需求走 scheduler 插 hyp_intent)· 常驻池 + 全局日配额 · **big-bang cutover 不长 soak**(但保 flag-可逆窗口在删码前)· 1-alone(复用 LIVE 节点,core/ 整簇删)· ROI = ops-债/架构质量/复用(非提交量,已接受)。
> **终审(wf_0e545131,3 镜头)= REVISE 但无架构推翻**:「none invalidate the design;解决 9 个 must-fix 即 build-ready」。全部折入(见 §3/§4/§5 标 〔终审〕)。
> 状态:**v4-final,3 轮对抗审查收敛(架构稳、9 执行漏洞已修),build-ready。待呈批 → Phase 0。未改生产代码。** LIVE DB 实证:6 反馈 flag 当前 ON / ENABLE_AUTO_ORCHESTRATOR OFF / CODE_GEN_SOFT_REG_W_ALIGNMENT=0.0。

---

## 0. 一句话

把「单 Celery task / 单 asyncio loop 内 producer→consumer 协程串起 假设→生成→sim→eval」的单体 **干净重建** 为 3 个常驻 worker 池(**HG** 假设+生成融合 / **S** simulate / **E** evaluate),DB 持久队列两事务认领,typed 工作项契约,Redis 控制面,单一 lease 恢复路径,全局预算单源。**复用** 久经考验的节点逻辑,**重构** 挡路的状态/编排/控制/成本/溯源,**删除** ~数千 LOC 包袱(FLAT/ONESHOT/orchestrator/cascade/watchdog-revive/core/ 整簇/R1b/G5/R5/FieldScreener/StrategyAgent)。

## 1. As-is 与包袱(清点实证)

单体编排(mining_tasks.py:145 run_mining_task → _run_flat_iteration → run_flat_pipeline_session;producer/consumer/runner + 4 个 in-memory asyncio.Queue;唯一 persister DB writer)。**包袱**:轮循环=producer while(producer.py:218)· MiningState 单-bag 跨所有阶段 · cascade_lock 三职责重载 · MiningTask-status 当控制面 · ExperimentRun/run_id per-dispatch 残留 · 三时区 8h 坑 · RD-Agent core/ 整簇休眠(0 生产引用)· 双 dispatch 臂(FLAT + ONESHOT/MiningAgent)。

## 2. 干净目标架构(3 池 × 1-alone)

**池 = 常驻 supervisor-重启的进程(非 per-task Celery dispatch),各 claim→hydrate→调既有节点→写结果**:

- **HG(假设+生成,融合)**:claim `hyp_intent` PENDING → 一进程一 session 跑 `node_rag_query → node_distill_context → node_hypothesis`(持久化 hypotheses 行=溯源锚)`→ node_code_gen → node_validate →[node_self_correct via route_after_validate]` → 产 N 条 `is_valid` 的 `candidate_queue` PENDING_SIM 行。**RAG/distill 产物(patterns/pitfalls/focused_fields)留 HG 内部 scratch、永不跨池**(generation.py:1058-1077 内禀耦合)。复用 `MiningWorkflow.run_hypothesis + run_codegen` verbatim(workflow.py:303-479)。
- **S(simulate,独占唯一稀缺 BRAIN 槽)**:claim PENDING_SIM → 从行 hydrate 单候选 MiningState → `run_simulate`(consumer.py:59 body)→ 写 sim_result + 转 PENDING_EVAL。每 S 进程自有 BrainClientRefresher。每次 BRAIN POST 前过 `brain:concurrent_sims` 原子信号量(USER=3/CONSULTANT=80)**且** `budget:sims:YYYYMMDD` 日计数器。
- **E(evaluate + 持久化)**:claim PENDING_EVAL → `run_evaluate` → `compute_verdict_from_signals`(纯 verdict fn,与 sync 共享)+ CorrelationService → 写 verdict/metrics;PASS/PROV→`alphas`(单写者 + `enqueue_can_submit_refresh` binary-can_submit bandit reward),非 PASS→`alpha_failures`(+新 metrics 列)。flush HG+S+E 缓冲 trace 于一个 per-候选 iteration(F3 scoping)。N 个 E 写者 → 抬高 can_submit 刷新速率限(persistence.py:665 的 6/60s)。

**队列(DB 持久,两事务 claim/lease,取代全部内存 asyncio 队列)**:
- **`hyp_intent`(新)**:每生成-意图一行;载 config_snapshot(thresholds band + llm_overrides + 冻结 brain_role_snapshot,**从 ExperimentRun 搬来**)+ prompt/thresholds_version + region/universe/dataset/delay。HG claim 源。
- **`candidate_queue`(新)**:HG→S→E lease 队列,把 `pipeline/types.py` Candidate+SimResult 落成持久行。stage(PENDING_SIM|SIMULATING|PENDING_EVAL|EVALUATING|DONE|FAILED|PURGED)+ expression + sim_settings + **role-snapshot 字段一等列〔终审 #7〕**(`effective_default_test_period`〔S 读 evaluation.py:1665/2237,丢则 testPeriod 错〕、`effective_sharpe_submit_min`+`delay`〔E 读 :1936,丢则 verdict 的 sharpe 门错〕,HG emit 时从 `hyp_intent.config_snapshot.brain_role_snapshot` 取)+ `dataset_category`〔node 读,project 为列〕+ current_hypothesis_id(scalar)+ context JSONB(其余 + `_validation_findings`/hypotheses 等 default-OFF screen 用,缺则降级 unknown 不崩)+ trace_records JSONB + sim_result JSONB + claimed_by + lease_expires_at + attempts。partial index(stage, lease_expires_at)。**Phase 1c gate:run_evaluate 在 hydrated 行 vs model_copy 整-state 的 PASS+FAIL byte-等价测**。

**契约(typed 工作项,取代单-bag MiningState 跨池)**:`HypothesisWorkItem`=hyp_intent 行投影;`CandidateWorkItem`=candidate_queue 行投影(expression + sim_settings + context + lineage{task_id,hyp_intent_id,hypothesis_id,bandit_arm,rag_ab_arm} + 可变结果槽)。MiningState **只存活为 HG 内部 LangGraph scratch**,emit 时投影成 CandidateWorkItem。

**控制面(Redis,取代 MiningTask-status-as-control)**:`pool:{hg,s,e}:drain` 每次 claim 前 + 每次 BRAIN POST 前查。STOP=SET drain + purge PENDING→PURGED(in-flight 检测 PURGED→优雅放弃 + 释 lease;**永不碰 CLAIMED/SIMULATED**)。RESUME=清 drain。

**恢复 = 单一路径**:lease-recycle beat(`CLAIMED ∧ lease_expires_at<now` → attempts<cap 回 PENDING 否则 FAILED poison-pill)+ **relaunch supervisor**(进程死;beat 只能 Stop-Process 不能拉起)。**砍掉 task-level watchdog revive**(避开本仓反复打的双跑/双恢复祸根)。

**成本(单一真相来源)**:`budget:sims:YYYYMMDD` 原子 INCR-compare-DECR,**仅成功 POST 后加**〔终审 #6〕——`simulate_alpha` status∈{200,201,202}+拿到 Location 的成功分支(brain_adapter.py:~802)、`simulate_batch`(CONSULTANT multi-sim,~916)**按 N 加**;**排除** slot-acquire 超时(:704 retry-loop 可不 POST 而 timeout)/429/auth-fail 早返回(在 slot-acquire 或 pre-POST 加 = 重蹈 24% 虚高早停 bug)。dedup/presim 短路**不计**(`is_simulated≠BRAIN-truth` 坑)。源自 BRAIN_DAILY_SIMULATE_LIMIT,超 → SET drain(非 DB pause)。`budget:tokens:YYYYMMDD` + **新 `POOL_TOKEN_BUDGET_PER_DAY`**(与 macro 占用的 `MAX_TOKENS_PER_DAY` 分开):三段 reserve/correct(软门 + p95 悲观预扣 + real−reserved 校正)+ HG `llm:concurrent` 信号量 bound 最坏。下沉 llm_service.call。

**调度器(beat)**:从 LIVE `DatasetCellStats.mining_weight`(dataset_weight_refresh,binary can_submit reward)`weighted_choice` 选 (region,dataset) **在 per-region advisory lock 下** → INSERT hyp_intent PENDING。reward 数学 verbatim。**不接 DatasetSelector UCB**(2026-05-23 双选择器锚死陷阱)。终止内禀(队列抽干)——无 per-session daily_goal/max_iters。

**溯源**:`hypotheses.id`(HG 写)=lineage 锚;hyp_intent 载 config/version/role-snapshot(取代 ExperimentRun)。`alphas.run_id` 先永久 nullable、runs.py 重指向后再 drop。trace_steps per-候选 iteration 保留、run_id FK nullable。**全部 datetime 列标准化为 tz-aware UTC**(北京只在 API 边界转)——杀掉三约定 8h 坑。

## 3. 复用 / 重构 / 删除 / 新建 账本

### REUSE(原样接,久经考验)
alphas 表(单写者 E,仅 run_id FK nullable)· simulation_cache(S 跨意图 dedup 更受益)· hypotheses 表(node_hypothesis 唯一创建者)· `brain:concurrent_sims` 原子信号量(已正确,全 pool/opt/auto-submit 共用)· `MiningWorkflow.run_{hypothesis,codegen,simulate,evaluate}`(纯 (state,config)→state)· node 链各步(rag/distill/hyp/codegen/validate/self_correct/simulate/evaluate)· `compute_verdict_from_signals` + `_eval_thresholds` + `_evaluate_single_alpha` · `_classify_alpha_failure` · record_trace 双模(累积后 flush=跨池 trace 交接)· mining_weight bandit + selection_strategy.weighted_choice · `MAX_TOKENS_PER_DAY`(留给 macro,**勿改用**)· tier/cascade 结构删除已完成(MiningTask 仅 schedule)+ 迁移历史不可变保留。

### REFACTOR(legacy 挡路)
MiningState → 3 typed 契约(只存活为 HG 内部 scratch)· AlphaCandidate → CandidateWorkItem(字段对、容器错)· types.py Candidate/SimResult → 提升 settings/lineage/findings 为一等字段,DB 行成跨池真相 · node_hypothesis 8 个 inline nudge 块 → `PromptContextEnricher` 策略(HG 已有一 session,不每块开 session)· persistence 拆:**thin E-pool finalize UPDATE**(行已被 HG 落 PENDING)+ Phase-2 异步后处理(B5/KB SUCCESS/family-cap 离线 off verdict);**删 run_with_persistence 批 buffer 路径** · consumer 拆 simulate→S / evaluate→E,丢 runner-shaped (Candidate)→SimResult 签名 → DB 行 hydrate · persister 写逻辑入 E 的 TXN-2,丢「单协程一 session」runner 工件 + Option-C reward_hook(FLAT-only)· alpha_failures **加 metrics JSONB** · **MiningTask → 常驻挖掘意图/scope**(留 region/universe/dataset_strategy/config;**删 dispatch 期列** schedule/generation_strategy/current_iteration/max_iterations/progress/last_alpha_persisted_at;status→ACTIVE/PAUSED/RETIRED;保表名+FK 避破坏性迁移)· alphas.run_id nullable-forever 停写 · trace_steps 丢 run_id FK(task_id+iteration 够)· task_service start/intervene:留 role-snapshot 冻结 + region/delay 校验 + LEVEL pinning 搬到 enqueue,**删 start/resume_flat_session/_dispatch_session_worker**,STOP/PAUSE→drain · **quota_guard effector 改 SET drain**(现 UPDATE WHERE status='RUNNING' 在池世界静默 no-op)+ count 源自 budget:sims · in-memory 队列**只换 transport**(asyncio.Queue→DB 两事务)留 per-coroutine own-session 契约 · workflow 留 4 run_*,**删 _build_graph/run_with_persistence/R1b·G5 子图** · edges 留 route_after_validate,删 R1b/_route_after_evaluate CoSTEER 路由 · AttributionType 抽到 `agents/attribution_types.py`(让 core/ 可删)· **genetic_optimizer:先抽 `enumerate_window_perturbations`(multi_fidelity_eval:502→RobustnessGate live 依赖)再删 GA 类** · **〔终审 #2 纠正〕`agents/prompts.py` shim 存活(REUSE)**——被 SURVIVING HG+E 节点引(generation.py:49 / validation.py:22 / evaluation.py:81),**不可删**;只 feedback-专属 prompt 名随反馈簇移 · **〔终审 #1〕`agents/__init__.py`**:module-level import 了 MiningAgent/create_mining_agent/FeedbackAgent(:6-7 + __all__),删 mining_agent.py 会令 `import backend.agents` 启动 ImportError → Phase 1c **同 commit** 删这些 import+__all__ 项,保 MiningWorkflow/MiningState/create_mining_graph re-export。

### REMOVE(确认无 live caller / 被取代)
run_mining_task + schedule 分支 · _run_flat_iteration · run_flat_pipeline_session + build_producer · run_pipeline_session runner + _Liveness watchdog · **MiningAgent.run_evolution_loop + ONESHOT 路径**(用户确认删)· **StrategyAgent + EvolutionStrategy**(仅 ONESHOT 消费)· **tasks/orchestrator.py 整文件 + 1h beat + finalize hook**(常驻池不需 relaunch,flag-OFF 无行为损失)· **cascade lock 协议三职责**(风险门控:还接 FLAT mining_tasks.py:440,**仅 Phase 1c 删**)· cascade dispatch stub + _is_cascade_schedule · **watchdog_revive_dead_sessions + 5min beat**(双跑风险,lease-recycle 取代)· **RD-Agent core/ 整簇** pipeline/trace/scenario/knowledge/experiment/evolving_rag(~2377 LOC,0 live caller)+ run_enhanced_mining + 死 integration 工厂 + test_core_* + run_real_mining.py(**删前先搬 AttributionType + R1a shim**)· core/ 数据类 Hypothesis/AlphaExperiment/EvoStep/ExperimentTrace + HypothesisFeedback · **R1b/G5/R5 反馈机器**(feedback_r1b/r1b_loop/feedback_g5/llm_crossover_alpha/g5_persistence/r5_judge + prompts + R1B_/G5_/R5_ config + reconcile/g5/r5 表;**先翻 flag OFF 验稳再删**)。**〔终审 #3〕`r5_judge.py` 例外:`run_r5_judge` 有第二个 flag-独立调用者**(evaluation.py:1148/1153 soft-regularizer alignment leg,gated `CODE_GEN_SOFT_REG_W_ALIGNMENT>0`,**非** ENABLE_LLM_JUDGE;LIVE=0.0 dormant 但 runtime 可翻)→ 删 r5_judge.py 前**断言 `CODE_GEN_SOFT_REG_W_ALIGNMENT==0` 并清零**,否则破 SURVIVING E 池(静默)· FeedbackEvent + pipeline feedback wiring · **FieldScreener**(~420 LOC,0 live caller 已确认)· config.MAX_SIMULATIONS_PER_DAY(0 readers,死)+ 退役 spike_launch.py(引退役 T1/T2/T3)· per-session 成本旋钮(daily_goal/max_iters 当停止条件;ALPHAS_PER_ROUND→HG per-intent 扇出 N)· runtime_state current_tier/dag 残留子键 · **〔终审 #9〕`_rebuild_flat_db_session`(mining_tasks.py:1056,孤儿引 MiningAgent/ExperimentRun,0 非-doc caller)** · **〔终审〕`agents/core/__init__.py`**(star-import 死簇,随 core/ 整簇删,免 strand)。**StrategyAgent 删后 `ENABLE_REGIME_AWARE_THRESHOLDS` + evaluation.py regime 块永久死(scheduler 不注入 regime)→ §6 决:scheduler 重注入 regime vs 回收该块**。

### NEW BUILD
candidate_queue + hyp_intent 表(Alembic)· HG/S/E 常驻 worker loop · 两事务 claim/lease 原语 + 心跳续约 · lease-recycle beat · **relaunch supervisor**(run.bat 重构 / NSSM / Popen-respawn)· scheduler beat · budget:sims + budget:tokens 计数器 + 接线 · pool drain keys + STOP/RESUME endpoint · alpha_failures.metrics 列 · **datetime UTC 标准化迁移**(AT TIME ZONE backfill)· runs.py 基于 hyp_intent+candidate_queue 重实现 · PromptContextEnricher 接口。

## 4. 迁移序(含删除,big-bang 但保 flag-可逆窗口)

- **Phase 0(地基,零行为变)**:Alembic 建 candidate_queue + hyp_intent + alpha_failures.metrics;标定 p95-per-node token reservation;定 experiment_runs 命运。FLAT/ONESHOT 仍 live。
- **Phase 0b(datetime hygiene,独立)**:ALTER naive-北京 datetime → tz UTC + AT TIME ZONE backfill;**先在 DB 副本测**(错的 backfill 会静默移 8h 腐蚀全部历史时间戳/配额/排序)。**〔终审 #8〕同 revision 必含 sync_tasks 写读对**:`_parse_to_beijing` 改存 tz-aware UTC(去 +8h)+ `_iso_bj`(sync_tasks.py:778)改发 `+00:00`(它给 BRAIN `dateCreated>` filter 重建 UTC 锚点)+ quota_guard naive `today_start`(session_watchdog.py:~284)改 aware-UTC(否则 asyncpg naive/aware 比较抛错)+ round-trip 测;留 3-day buffer 兜底。北京只在 API 边界。
- **Phase 1a(只抽取,无删除)**:搬 AttributionType + R1a shim 出 core/ → 新 `agents/attribution_types.py`(**该模块只 import `prompts.alignment` + 两枚举,绝不 import core.pipeline/trace/scenario**,否则又拖死簇;断掉 core/integration.py:41 的 module-level core.pipeline import = 让死簇可删的前提)· 抽 enumerate_window_perturbations 出 genetic_optimizer · 抽 node_hypothesis nudge 块 → PromptContextEnricher · feedback_agent import 重指向 prompts.analysis(prompts.py shim **存活**)。
- **Phase 1b(建池,flag `ENABLE_POOL_PIPELINE` OFF,与 FLAT 并行)**:实现 HG/S/E loop + 两事务 claim/lease + lease-recycle beat + scheduler beat + budget 计数器 + drain keys + relaunch supervisor;接 run_* 为池 body;quota_guard effector 改 drain(FLAT 在跑时仍 no-op-safe);E persist=thin UPDATE + 抬速率限。
- **Phase 1b-flip(live 行为变,非 no-op)**:DB 翻 OFF 6 反馈 flag(ENABLE_R1B_RETRY_LOOP/HYPOTHESIS_MUTATE/FAILURE_TREE/G5_CROSSOVER/LLM_JUDGE/R1A_HOOK,**当前全 ON**)→ **生产验稳**。**〔终审 minor〕byte-等价仅在 routing/verdict 层**(compute_verdict_from_signals 只吃 sharpe/fitness/turnover/self_corr/score,不受影响);**data/cost 层有变**:R1A_HOOK+LLM_JUDGE(evaluation.py:3165-3290 独立块)关掉会停写 `_r1a_*/_r5_*` metrics 键 + 停 r1a_attribution_log INSERT + 省一次 LLM 调用/alpha;`ENABLE_R1B_FAILURE_TREE` 还 gate 一个**前向** RAG 读(hierarchical_rag.py:1036,每次生成重排 pitfalls)——翻它会改前向 RAG 排序,须一并翻 OFF 才真纯前向。禁 orchestrator periodic scan。
- **Phase 1c-flip(cutover)〔终审 #4 重写可逆性〕**:翻 `ENABLE_POOL_PIPELINE` ON(**该 flag 现不存在,是新建**;wire 成 **(a) 每池 claim 前的 gate + (b) FLAT start/resume endpoint 的 guard** —— flag gate DISPATCH/CLAIM,**进程存活由 supervisor+drain 管,非 flag**);scheduler 改喂 hyp_intent。**短验证几小时**(池产出 + claim/lease 无 bug;**退出判据:无行卡在 SIMULATING/EVALUATING 超 lease**),非长 soak。**回滚不是单翻 flag**(config flag 停不了常驻进程,且 FLAT/pool 状态 disjoint)→ **3 步手动 runbook**:① SET pool drain + 停 supervisor 令 HG/S/E 进程退出 ② 处理孤儿 candidate_queue/hyp_intent(PENDING 停在原地待下次 flip-ON,CLAIMED 等 lease 回收;**不丢,park**)③ **手动 `/ops/flat-sessions/{id}/resume` 每个 PAUSED FLAT 任务**(flat_cursor 保留;orchestrator OFF 不自动复活)。
- **Phase 1c-delete(验证后,不可逆 git-revert-only)**:删 run_mining_task + _run_flat_iteration + **`_rebuild_flat_db_session`** + flat pipeline/producer/runner + MiningAgent/ONESHOT + cascade lock 三职责 + cascade stub + watchdog_revive + orchestrator + start/resume_flat_session;**同 commit 改 `agents/__init__.py`**(去 MiningAgent import+__all__);**同窗死码回收**:删 core/ 整簇 + `agents/core/__init__.py` + run_enhanced_mining + test_core_* + run_real_mining.py + FieldScreener + StrategyAgent + EvolutionStrategy + R1b/G5/R5 机器(r5_judge **须先确认 W_ALIGNMENT==0**)+ MAX_SIMULATIONS_PER_DAY + spike_launch.py;**prompts.py shim 不删(存活)**;DROP MiningTask dispatch 期列;**改 celery_app.py worker_process_init 的无条件 `brain:concurrent_sims` delete 为 restart-safe**(见 §5);重构 run.bat 启 + supervise 三池。
- **Phase 1d(后续迁移,runs.py 重实现后)**:DROP alphas.run_id + trace_steps.run_id FK + experiment_runs 表;清 daily_goal/max_iters 旋钮;drop runtime_state tier/dag 子键。
- **Phase 2(非本次)**:反馈以 **async reconcile-beat 读 EVALUATED 行 → 插新 hyp_intent** 重接(非 in-loop 队列);重建 minimal feedback DTO;rewire run_retry/run_mutate(留 dormant)。

## 5. 风险 + 缓解(清点 risks)

| 风险 | 缓解 |
|---|---|
| cascade lock / experiment_runs 是**风险门控删除非裸删**(还有 live 接线/读者)| 仅 Phase 1c/1d 删,删前确认 FLAT 退役 / runs.py 重指向 |
| genetic_optimizer 非裸删(enumerate_window_perturbations live)| Phase 1a 先抽 helper 再删 GA |
| Phase 1c big-bang git-revert-only 回滚 | **保 flag-可逆窗口**(1c-flip 翻 ON 短验证 → 1c-delete 才删码);本仓反复跳 soak,这次至少留可逆窗口 |
| 删 watchdog-revive 同时留任何 task-level revive = 双跑 | **lease-recycle 是唯一恢复路径**,删前确认无残留 revive dispatch |
| budget:sims 误在 alpha 行创建**或 slot-acquire/pre-POST** 时加〔终审 #6〕| 仅**成功 POST 后**(200/201/202+Location)加,simulate_batch 按 N;排除 slot-timeout/429/auth-fail 早返回(is_simulated≠BRAIN-truth 坑,曾致 24% 虚高早停) |
| **〔终审 #5〕celery_app.py worker_process_init 无条件删 `brain:concurrent_sims`** —— 常驻池下重启一个 pool 进程会清零兄弟在飞 sim 的共享计数 → 越 BRAIN cap → 429/wedge | 重启前 drain 到 0,**或**改 guarded reset(仅 stale + 无 live lease 才清);禁在兄弟持 sim 时重启单池(除非 reset restart-safe);入 supervisor + cutover 序 |
| **〔终审 #1/#2/#3〕漏的存活引用**:agents/__init__.py module-import MiningAgent / prompts.py 被 HG+E 引 / r5_judge 有 soft-reg flag-独立调用者 | __init__.py 同 commit 改;**prompts.py 不删**;r5_judge 删前断言 W_ALIGNMENT==0 |
| **〔终审 #4〕单翻 flag 回滚被证伪**(ENABLE_POOL_PIPELINE 不存在 + config flag 停不了常驻进程 + FLAT/pool 状态 disjoint)| §4 三步手动 runbook(drain+停 supervisor / park 孤儿行 / 手动 resume FLAT);flag 双 wire(claim gate + FLAT endpoint guard)|
| **〔终审 #7〕role-snapshot 字段(test_period/sharpe_submit_min)漏出行投影 → S/E 静默用错值**| §2 列为 CandidateWorkItem 一等列 + round-trip 契约测 + Phase 1c hydrated-vs-state byte-等价测 |
| quota_guard effector 在池世界静默 no-op | **必改 drain-key effector**,否则唯一日 backstop 无声死 |
| R1b/G5/R5 flag **当前 ON** = 翻 OFF 是 live 变更 | Phase 1b-flip 单独验稳再删机器 |
| 删 core/ 须先搬 AttributionType+R1a(core/integration.py:41 module-level import 死簇)| Phase 1a 先搬,顺序错 = 启动 import error |
| MiningState→3 契约 + 换队列 transport 碰热路径 | per-池守 F1 契约(无两协程共享 session),否则重蹈 idle-in-txn/session-thrash |
| datetime UTC 迁移是数据迁移非纯类型变 | 先 DB 副本测 AT TIME ZONE backfill 表达式 |
| scheduler beat 重叠 + 手动触发并发 INSERT hyp_intent | per-region advisory lock 串行化(仿 hypothesis_service.py:508);**只一个选择器**(不接 DatasetSelector UCB) |

## 6. 开工前必决(剩余)

1. **experiment_runs 命运**(Phase 0):只读 legacy 归档一版(最安,保 runs.py + 300+ 行)vs 合成 pool-run vs 立即 run_id-nullable。
2. **R1a attribution**:Phase 1 保留日志(搬 shim,flag 留 OFF,免费随行)vs 整删(删 evaluation.py R1a 块 + R1aAttributionLog 表)。决定 AttributionType 是否现在就抽。
3. **ENABLE_ROBUSTNESS_CHECK**(默认 OFF 但 E 路真 opt-in):清洁目标保 RobustnessGate(+抽出的 helper)vs 推迟/砍。定 genetic_optimizer 清理范围。
4. **supervisor 机制**:run.bat 循环 vs NSSM vs Popen-respawn(Windows solo 约束)。Phase 1b 前定。
5. **p95-per-node token reservation 标定**:Phase 0 测量窗口数据源(cost_tracker 历史)。
6. **K_s/K_e 池 sizing + per-类 sim 预算预留**(K_s+K_e+opt+auto-submit ≤ BRAIN_DAILY_SIMULATE_LIMIT;opt double-acquire 2 槽/变体):USER=3 vs CONSULTANT=80 怎么切。
7. **〔终审 minor〕regime 块去留**:删 StrategyAgent/MiningAgent 后 `ENABLE_REGIME_AWARE_THRESHOLDS` + evaluation.py regime 块永久死(无人注入 regime)→ scheduler 重注入 regime label vs 回收该块。

## 7. Sequencing / 工时(删+建,1.5× 修正)

- Phase 0 + 0b(schema + datetime 迁移 + experiment_runs 决):~4-5d
- Phase 1a(抽取:AttributionType/window-helper/nudge/shim 重指向):~3-4d
- Phase 1b(建 HG/S/E 池 + claim/lease + lease-recycle + scheduler + budget + drain + supervisor):~8-10d
- Phase 1b-flip(翻反馈 OFF + 验稳):~1-2d
- Phase 1c(flip + 短验证 + 大删除 + run.bat 重构):~4-5d
- Phase 1d(runs.py 重实现 + drop run_id/experiment_runs):~3-4d
- 横切(budget 闸 + quota_guard 改写 + PromptContextEnricher):~3-4d
- **合计 ~26-34 人日**(不含 Phase 2);**gate 在 Phase-0 + Phase-1b 池验证后复估**。删除净减 ~数千 LOC。

## 8. 非目标(Phase 1)

CoSTEER 反馈环(Phase 2 async reconcile-beat 重接)· core/ 重建(整删,Phase 2 需 DAG/knowledge 再说)· settings/region 扇出(optimization loop,Phase 2)· H 池独立拆分(反馈驱动异步假设时,Phase 2)· 多账号/跨 tier 热切。
