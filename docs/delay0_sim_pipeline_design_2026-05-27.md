# 设计:挖掘流水线化 —— 让 BRAIN sim 槽持续饱和(2026-05-27)

## 问题(实测)
FLAT 一轮是**串行**:RAG→蒸馏→假设→code_gen→validate(LLM,均 **332s**,3 个 sim 槽全闲)→ SIMULATE(均 177s,槽忙)→ evaluate。
- 3728 九轮实测:**sim 槽利用率 ≈ 35%**(闲 65%,都在等 LLM 出货)。
- 瓶颈不是 3 槽上限,是槽大半时间空等生成。理论上流水线化可把利用率拉到 ~90%,吞吐 ~2.5-3x(不需升 Consultant)。

## 目标
解耦"生成(LLM)"与"模拟(BRAIN)",让两者在同一 event loop 并发重叠 → sim 槽持续饱和。

## 当前架构(要改的)
```
_run_flat_iteration  (while iterations: 串行)
  └─ _run_one_round_inline
       └─ mining_agent.run_evolution_loop(max_iterations=1)
            └─ run_mining_iteration
                 └─ workflow.run_with_persistence  (LangGraph 串行全节点)
                      RAG→DISTILL→HYPOTHESIS→CODE_GEN→VALIDATE→SIMULATE→EVALUATE→persist
```
SIMULATE(node_simulate)内部已对一轮批量做 `asyncio.gather` + Redis 槽计数(USER=3/CONSULTANT=80),但**生成在它之前串行** → 槽闲。

## 设计:producer-consumer 流水线

```
                 ┌─ Producer 协程(1 个) ────────────┐
                 │  loop:                            │
   dataset bandit│   选 dataset → RAG→DISTILL→        │
                 │   HYPOTHESIS→CODE_GEN→VALIDATE→    │ push 已验证候选(带 context)
                 │   (self-correct)                  │──────────┐
                 └───────────────────────────────────┘          ▼
                                                     asyncio.Queue(maxsize=K)   ← 背压
                 ┌─ Consumer 协程(N=槽上限 个)──────┐          │
                 │  loop:                            │◄─────────┘ pull
                 │   acquire sim slot → SIMULATE →   │
                 │   EVALUATE → persist → release    │ 槽持续饱和
                 └───────────────────────────────────┘
```

- **Producer**:复用 RAG/hypothesis/code_gen/validate 节点逻辑,循环产**已验证**候选(连同其 MiningState 切片 + hypothesis_id + dataset + sim 设置 + bandit arm)推入队列。一次 LLM 调用产 N 个,逐个 push。
- **队列**:`asyncio.Queue(maxsize=K)`(如 K=2×槽数)——背压:队列满则 producer 阻塞,不会无限超产。
- **Consumer ×N**(N=`_current_sim_slot_limit()`):各自 pull 一个候选 → `_acquire_sim_slot` → node_simulate 的 sim → node_evaluate → `_incremental_save_alphas`(单条/微批)→ release。N 个 consumer 让槽持续满。
- **并发**:producer(LLM I/O)+ N consumer(BRAIN sim I/O)同一 event loop;consumer await BRAIN 时 producer 的 LLM 调用并行跑 → 槽不空。

## 必须保留(今天/历史的修复,不能 regress)
- Redis 跨进程槽计数 + TTL 防泄漏(consumer 用);角色感知 3/80。
- client 每 N 轮重建(`_refresh_brain_client`)、僵尸协议、detail-fetch 上界、round 超时兜底 → 改造成"每 M 个候选/每 T 秒"维度。
- delay 穿线(producer 的 gen + consumer 的 sim 都带 task.delay)。
- 字段硬拦(delay-0 strict)、生成侧 prompt、移除复杂度上限。
- bandit:per-候选结果回填 reward(不再 per-round)。
- FLAT cursor / dataset 轮转 → 移到 producer 的选 dataset。
- dedup、self-correct、R1b pending-hypothesis、G5 offspring。

## 难点 / 风险(诚实评估)
1. **LangGraph 是串行设计**:要把 workflow 拆成"生成子图(RAG..validate)"和"模拟子图(simulate..persist)",producer 跑生成子图、consumer 跑模拟子图。非平凡(state/configurable/persistence hook 都要拆)。
2. **大量 round 级机制**要改成连续流:早停、round-summary、bandit round-end hook、R1b carry、self-correct、dedup batch、trace_steps.iteration(UI 按 iteration 分组)。
3. **回归面大**:刚稳定的 delay-0 + 今天 6 个修复 + 槽/挂死/僵尸全在这条路上。
4. **trace/persistence/iteration 语义**假设 round,流水线模糊了 round 边界 → 需重定义"批次"单位。

## 分阶段(推荐)
- **Phase 1 — B-lite 轮重叠(低风险,拿~大半收益)**:保留 round 结构,但在轮 N 的 SIMULATE+EVALUATE 跑的同时,**并发预生成轮 N+1 的候选**(prefetch task)。LLM 阶段(332s)藏到 sim 阶段后面 → 利用率 35%→**~60-70%**。改动局限在 `_run_flat_iteration`(加一个 prefetch 协程 + 把已生成候选传给下一轮),round 级机制/trace/persistence **几乎不动** → 回归面小。
- **Phase 2 — 全连续 producer-consumer**:上面的完整流水线,利用率 ~90%+。在 Phase 1 验证收益后再做。

## 验证
- 利用率脚本(本设计文档的量化脚本)重测:Phase 1 应见利用率↑、吞吐↑;
- 回归:`test_suite.py --all` 0 漂移 + delay-0 各修复不回归(无挂死/delay=0/无 unknown-variable/字段硬拦)+ 新单测(队列背压、consumer 槽 acquire/release、producer 选 dataset)。
- 灰度:flag `ENABLE_SIM_PIPELINE` default OFF,先 delay-0 单 session 验证。

## 工时(粗估,见下方 review 修正)
- Phase 1(B-lite):~1-2 人日(局部改 FLAT 循环 + prefetch)。
- Phase 2(全流水线):~4-6 人日。

## 对抗性 REVIEW(2026-05-27)—— 隐藏成本,修正上面的乐观估计

**🔴 F1(决定性):共享 DB session 是真正的杀手。**
`workflow.run_with_persistence` + `TraceService(self.db, iteration=…)` + persistence/hypothesis-link/bandit-read **全用一个 `self.db`**。**asyncpg 单连接不支持并发 op**(会 "another operation in progress"),且今天刚修的 **greenlet-poison**(取消的协程中途毒化共享 session)在并发下必复发。
→ **任何重叠(B-lite 或全流水线)都要给 producer + 每个 consumer 各自独立 DB session**,并把刚修好的"poisoned-session 重建"逻辑**按协程数复制**。这是一整块易错重构,**重新打开今天刚关上的那类 bug**。
→ **修正:B-lite 也不是 1-2 人日**——它的 prefetch-gen 和当轮 sim-persist 同时碰 `self.db`,同样需要独立 session。

**🟠 F2:round 级机制全要迁移到连续流。** `should_stop_early`+`round_history`(早停)、`_record_round_summary`(ROUND_SUMMARY)、G5 round-end crossover、R1b/G5 pending carry、bandit round-end 回填、batch_dedup —— 全假设 round 循环。

**🟠 F3:UI 按 `trace.iteration` 分组**(TaskDetail.jsx:229-268 "group steps by iteration")。流水线模糊 round 边界 → 要重定义"批次"作为 iteration 单位,否则前端时间线乱。

**🟡 F4:BrainAdapter client-refresh 与并发 consumer 冲突。** `_refresh_brain_client` 关闭全局 httpx client,若此时 N 个 consumer 正 await 在上面 → 崩。refresh 要协调(仅在无 in-flight sim 时,或每 consumer 独立 client)。

**🟢 F5(好消息):`_incremental_save_alphas` 已参数化 `db_session`** —— consumer 可传自己的 session,这一处不用改。但 workflow wrapper / TraceService / mining_agent 用 self.db,要改。

### 修正后的判断
- **Phase 2 全流水线**:含并发-session 重构 + round 机制迁移 + trace/persistence 重定义 + BrainAdapter refresh 协调 → **实际 > 6 人日,高回归风险**(重开 greenlet-poison 类 bug,危及刚稳定的 delay-0 + 今天 6 个修复)。
- **Phase 1 B-lite**:也需独立 session(F1)→ 不是低风险小改。
- **关键洞察**:**任何"重叠"的真实成本 = 并发-safe DB session 重构**(asyncpg 单连接 + greenlet-poison),这才是大头。

### 替代:Option A(批量加大)重新进入视野
- **纯串行,无并发,无 DB-session 重构,无 round 机制改动**:把每轮 code_gen 批量 4→~15(一次 LLM 调用产更多)→ 固定 LLM 阶段(332s)摊到更多 sim → 利用率 35%→~65-75%,吞吐 **~2x**,**近零风险**。
- 流水线把 2x→3x 的边际收益,要付"并发-session 重构 + 高回归风险"的代价 —— **性价比远不如 A**。

### 评审结论
1. 想要**低风险拿 2x**:上 Option A(改一个 `num_alphas_per_round`,几行)。
2. 真要 **~3x 流水线**:当作正式项目分两步——**Sub-phase 0:并发-safe DB session 基座**(producer/consumer 各自 session + 重建逻辑)先做透并回归;**Sub-phase 1**:在基座上接 producer-consumer。别当快改。
3. 不建议:直接全 Phase 2 一次性上(回归风险吞掉收益)。

## 修正 v2(2026-05-27)—— "串行化 DB 访问" 大幅降低 F1 成本

用户提出:DB 不支持并发 → 用缓存、最终再落库。**这是破 F1 的正确方向**,且比"给每协程一个 session"便宜得多。本质:**F1 不是要求 N 个 session,而是要求"同一 asyncpg 连接上不能有并发 op"。解法 = 串行化 DB 访问 + 让 N 个 sim consumer 完全不碰 DB。**

### 修正架构:单所有者协程 + DB-free consumer
```
Producer(1 协程, 独占 session_P)──► 工作队列 ──► Consumer ×N(零 DB)──► 落库队列 ──► Persister(1 协程, 独占 session_C)
  读 bandit/RAG/假设 + LLM 生成+validate     候选         acquire 槽→BRAIN sim→evaluate(纯内存)        串行写 trace+alpha+bandit reward
```
- **碰 DB 的只有 2 个单所有者协程**:Producer 一个 session、Persister 一个 session。各自单协程串行 → 无并发 op、无 greenlet-poison(中途取消毒化的前提是并发,此处不存在)。
- **N 个 sim consumer 零 DB**:只做 BRAIN I/O + 内存算 verdict/metrics,结果推落库队列。
- **session 数 = 2(固定)**,不是 F1 设想的 N+1,也不需把 poisoned-session 重建逻辑按协程复制。**F1 从"高危的 N+1 重构"降为"2 个固定单所有者 session + 一处缓冲"。**

### 残留真活(诚实)
1. **trace_steps 目前是节点内联写**(各 node 调 `TraceService.add_step`)→ 需让并发 workflow **不内联写、改吐内存对象**,由 Persister 排空落库。受限改动(缓冲 + flush),非并发重构。
2. **不能 end-of-session 才落库**:BRAIN sim 烧不可逆日配额;崩溃丢已 sim 未落库结果 = 白烧。→ Persister **持续排空**(每 N 个/每 T 秒一批),压小崩溃丢失窗口。
3. **验 `node_evaluate` 计算路径是否碰 DB**(self-corr 是 pickle 缓存还是 DB)。若纯内存,consumer 保持零 DB;若读 DB,把那次读挪到 producer 预取。
4. F2(round 机制迁移)、F3(UI iteration 单位)仍在,但 Persister 在落库时统一分配 iteration/batch id,反而更干净。

### 修正后工时/风险
- 去掉了 F1 最吓人的 N+1 session + 多份 greenlet 重建 → **staged-B 可行性明显变好**;主成本转为"buffered-trace 模式 + 持续 Persister + round 机制迁移"。
- 仍建议分步(Sub-phase 0 = 串行化-DB 基座 + buffered trace + 持续 Persister 验证;Sub-phase 1 = 接 producer/N-consumer),但每步都比 v1 估的低危。
- Option A(批量 4→15,~2x,近零风险)仍是"想立刻拿收益"的首选;此修正主要让"将来真做 3x 流水线"不再是高危项目。

## 修正 v3(2026-05-27)—— 槽数 regime:USER 3 槽 vs CONSULTANT 80 槽,瓶颈换位

`_current_sim_slot_limit()` 已是角色感知:USER=3 / CONSULTANT=80。**升 Consultant 后,瓶颈从"sim"换到"生成",架构选择反转。** 实测基线:一次 LLM gen 调用产 ~12-15 候选 / ~175s ≈ **0.086 候选/s**;单 sim ≈ 200s。

### Regime 1 — USER 3 槽(现状):瓶颈 = SIMULATE
- 最大 sim 吞吐 = 3 / 200s = **0.015 sims/s**(~1296/天)。
- 生成速率 0.086/s **远超** sim 消费 0.015/s → 生成不是瓶颈,sim 是。
- **Option A(加大 batch)直接填满 3 槽,利用率 63%→~84%。流水线只多拿 84%→90% 边际,不值。** ✓ A 赢。

### Regime 2 — CONSULTANT 80 槽:瓶颈 = 生成(GENERATION)
- 最大 sim 吞吐 = 80 / 200s = **0.4 sims/s**(~34,560/天)。
- **单生成器只 0.086/s → 只能喂满 0.086/0.4 ≈ 21% 的槽 ≈ 17 个**。剩 ~63 槽永远空。
- **Option A 在 80 槽下封顶 ~15-17 并发 sim(一批在飞 + 下一批在生成),槽利用率掉到 ~19%** —— 比现在绝对产能高,但 80 槽白买大半。
- 要喂满 80 槽:**必须 (a) 连续 producer-consumer(不能 round-batch)+ (b) ~5 个并行 producer(并发 LLM 调用)或单调用产 40+ 候选**。单 producer 的流水线也不够 —— 生成端本身要并行。

### 关键推论
1. **架构随槽数变**:consumer 数 N 已 = `_current_sim_slot_limit()`(自动 3/80);**但 producer 数 / batch 也必须随槽数放大** —— 当前单 producer 设计在 80 槽下喂不动,设计要加"producer 数随角色 scale"。
2. **80 槽下真正的绑定约束可能不再是槽**:0.4 sims/s = 34,560 sims/天,远超 `MAX_SIMULATIONS_PER_DAY`(为 USER 设);并行生成的 token 烧 `MAX_TOKENS_PER_DAY`/成本。**升 Consultant 后,瓶颈大概率换成"日 sim 配额 / LLM token 成本",不是槽。** 这两个上限要同步抬,否则 80 槽喂不满也烧不起。
3. **Option A 不浪费**:batch-size 旋钮在 3 槽和 80 槽都有用(80 槽下大 batch 还是减少 gen 调用次数的手段之一),是任何 regime 的基础积木;只是它**单独**在 80 槽不够。

### 据此的执行排序(随 Consultant 时间线分叉)
- **现在(USER 3 槽)**:上 **Option A**(改 batch,1 行)拿即时 1.5-1.8x。**无论 Consultant 来不来都不浪费。**
- **Consultant 已确认/临近**:把流水线当正式项目启动(Sub-phase 0 串行化-DB 基座 → Sub-phase 1 连续 producer-consumer → **Sub-phase 2 并行 producer + 抬 sim/token 日配额**)。>6 人日的成本由"80 槽吞吐 = 升级的全部意义"justify。
- **Consultant 不确定/还远**:先只做 A,流水线等 Consultant 确认再投(别为或然收益付回归风险)。
- **不变的反对**:在 3 槽期为"将来 80 槽"提前全量上流水线 —— 3 槽下它毫无收益(84%vs90%),回归风险却实打实砸在刚稳定的 delay-0 上。**等 Consultant 信号再投。**
