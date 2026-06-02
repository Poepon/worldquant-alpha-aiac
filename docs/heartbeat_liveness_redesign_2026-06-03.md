# 流水线 heartbeat 根本重设计 — per-coroutine liveness watchdog

- 日期: 2026-06-03
- 状态: 设计定稿(ultracode 多 agent workflow:理解→5 设计→对抗验证→综合,19 agent)
- 起源: task 3930 出第一个 PROV(证明 EVAL 修复 `9d79696` 生效)后 14min28s 被 heartbeat 误杀(`heartbeat_abort`)
- 3 个 load-bearing 事实已 Opus 主体实测复核(见 §8)

---

## 0. 决定性发现 — NullPool 推翻"池毒化"前提

5 个设计 agent 多数建立在"asyncpg **共享池**被 wait_for-cancel 毒化 → 所有后续 await park"前提上。**实测 `backend/database.py:13` = `poolclass=NullPool`**:每个 `async with AsyncSessionLocal()` 借全新连接,`__aexit__` 彻底 dispose,三类协程(producer 持久 session / persister 临时 session / N consumer ephemeral session)**永不共享连接**。

→ "池毒化级联"在本库**不存在**。真实 freeze CLASS 退化为 **per-coroutine 单连接僵死**:某协程的某个 await(被 cancel 打断 mid-protocol 的坏 socket cleanup,或未包 `_with_timeout` 的裸 await)在它**自己那条**连接上永久 park,只杀那一个协程。但 producer-consumer 拓扑下,单协程僵死足以让全 pipeline 静默死锁(`_main_chain` 卡在 `await producer_task`)。

**所以仍需会话级兜底,但判据必须是 per-coroutine 存活,不是全局产出。**

---

## 1. 根本设计缺陷(task 3930 真因)

现 heartbeat(`runner.py:200-212, 413-436`)用**进度信号**(`beat()` 只在 `_push`/`_event_done`/persister flush 三处)推断**存活性**。这是范畴错误:

- task 3930 iter12→13 的 14min 是**多个合法 0-产出 round**(LLM HYPOTHESIS 反复 retry / pre-sim skip),producer 真在工作但没 push 候选 → `_last_progress` 14min 不动 → 900s 阈值误杀
- producer **存活**(每轮 `wf.run` 在 `_with_timeout(600s)` 内正常返回)但**无产出** → 被当成 freeze

**进度 ≠ 存活。慢 ≠ 死。**

---

## 2. 推荐根本解 — per-coroutine liveness watchdog

每个被监控协程在**每次从 `_with_timeout` await 返回时**盖一个 monotonic 戳(= 它让出又重入了 = 活着)。协程 park 在裸 await(单连接 cleanup 永挂 / 死锁)→ 停止盖戳 → 戳停滞超阈值 → cancel。**IDLE**(阻塞在空/满队列)是独立豁免状态——合法等待 ≠ freeze。

判据从"整个 pipeline 多久没出 alpha"变成"**每个协程是否还在 yield-and-reenter**",与产出彻底解耦 → 根治 task 3930 误杀。

主干 = 「操作感知 watchdog」的 per-coroutine 判据 + IDLE 豁免;嫁接「liveness vs productivity」分层语义。**否决**外部 daemon loop 探针(NullPool 下 loop 仍活,探针看不见单协程僵死;且 snippet 用了不存在的 `get_redis`)。

---

## 3. 三层架构(职责正交,零冗余)

```
层1 per-op timeout (op_timeout=600, capped wd-300=1200)
    单个被包 await(sim/eval/wf.run/run_codegen/next_round_inputs + 新增 persist/acquire_slot)硬截止
    → 失败 SimResult → persist → 写 trace_step(层3 逃生信号源头)
        ↓ 兜底(层1该 fire 没 fire)
层2 per-coroutine liveness watchdog (新, L1_DEAD=op_timeout+grace=720s)
    任意协程"最近从 _with_timeout 返回"戳停滞 > 720s 且非 IDLE → cancel main_chain
    判 freeze 前 persister 仍写 trace_step → 层3 独立逃生
        ↓ 进程级最终兜底(层2自身 park / 单线程被僵尸独占)
层3 cascade watchdog (session_watchdog.py, 独立 beat 进程, 25min)
    signal=latest trace_step DB 写。NullPool → revive=fresh worker=fresh 连接,天然脱僵
(可选)层4 productivity SLA (默认 OFF):活着但 alpha/h 过低 → PAUSE 交 orchestrator,NOT freeze
```

**删除**:现 `beat()`-on-progress 的 abort 职责(范畴错误)。**保留**:`_flush` 里 trace_step 写(层3 逃生信号,与 beat 无关)。

---

## 4. 关键论证

### freeze CLASS 仍被捕获
- 单连接 cleanup 永挂:若在被包 op 内 → 层1 600s cancel 走失败路径;若 cancel 不穿透 → 层2 720s 戳停滞 fire;若仍不穿透 → 层3 trace 停滞 25min revive fresh worker
- queue deadlock:至少一协程卡在非队列裸 await(否则是正常 quiescence)→ 非 IDLE → 层2 fire
- unwrapped await:补全 persist/acquire_slot 进 `_with_timeout`;层2 对未来新增裸 await 提供 720s 兜底 + 点名日志

### LLM retry 14min 不再误杀
- 14min = 多个 round 串行,每 round `wf.run` <600s 返回并 tick `liveness['hyp']` → 层2 看到的永远 `now-ts<720s`,不累积跨 round 静默
- 旧 `beat()` 只在 `_push` 触发,零产出 14min → 误杀。新 tick 在"协程从 `_with_timeout` 返回",零产出也持续 tick → 根治

---

## 5. 改动清单(file:line)

- **A1** `runner.py:47` `_with_timeout` 加 `on_return` 回调(finally 触发,向后兼容默认 None)
- **A2** `runner.py:200-212` 替换 beat 块为 liveness registry(`_liveness: dict[owner,float]` + `_idle: set` + `_touch/_enter_idle/_exit_idle/_register`)
- **A3** `runner.py:287-335` consumer:work_q.get 包 IDLE;acquire_slot/simulate/evaluate 三处 `_with_timeout(on_return=lambda:_touch(f"consumer-{cid}"))`
- **A4** `runner.py:340-347` persister:`await persist(...)` 纳入 `_with_timeout(op_timeout, on_return=_touch("persister"))` — **独立纯增益**,堵上唯一无界裸 await
- **A5** `runner.py:413-436` supervisor 重写:扫 `_liveness`,跳 IDLE,per-owner debounce(连续 2 扫描同戳才 fire),点名 owner
- **A6** `producer.py:195/204/254` hyp/code producer 各自 owner + IDLE,经 contextvar 传 liveness 回调(保 `produce` 签名稳定)
- **B** `mining_tasks.py:104` `_pipeline_heartbeat_timeout` 改 `L1_DEAD=min(max(900,op+grace), wd-180)=720`(安全垫 780s > 现状 600s,更宽)
- **C** `config.py:1551` 后加 `SIM_PIPELINE_LIVENESS_GRACE_SEC=120`
- **D** 测试:裸 await park→fire 点名 / 慢多-round→不 fire / 全-IDLE→不 fire / persist 超时→层1 失败不 freeze / debounce。`test_suite.py --all` + baseline 6/6 0 漂移

**总工时 2.0 人日**(无新线程/无 OpRegistry 状态机,集中在 runner.py+producer.py 接线)

---

## 6. 分层交付

### Band-aid(已被根本修复取代)
- **B-A** `.env` `SIM_PIPELINE_HEARTBEAT_TIMEOUT_SEC 900→1320`:**已 moot** — Sub-phase 1 根本修复(per-coroutine liveness)直接替换了进度 heartbeat,不再需要调大窗口止血。
- **B-C** celery `--max-tasks-per-child=50`:**实测不适用(2026-06-03 核查)**。`--max-tasks-per-child` 是 **prefork pool 特性**(回收子进程);本项目 celery 用 `--pool=solo`(run.bat:184-185),无子进程可回收 → 该选项在 solo 下是 **no-op**。综合方案此处事实错误。`task_acks_late` 当前也未设(默认 False)。
  - **solo 下"单线程被僵尸独占"的真实缓解** = 分层 in-task 超时本身:op_timeout 现包住所有热路径 await(A4 补上了最后一个裸 persist),liveness watchdog 捕获 bare-await park 并 cancel。只有 cancel 自身不穿透坏 socket(真最坏)才需进程重启(运维 `run.bat --restart`),这在 solo 单进程模型下无法在 Celery 内自动化。

### 根本修复(2.0 人日)
- **Sub-phase 1(1.4d)**:A1+A4(persist 纳入 `_with_timeout`,独立纯增益)+ A2/A3/A5/A6 + B/C + 测试
- **Sub-phase 2(0.6d,flag OFF)**:层4 productivity SLA(floor 需先收集多任务 alpha/h 分布)

---

## 7. 残余风险(诚实)
1. **cancel 不穿透坏 socket**:NullPool 限污染到单条临时连接;层3 25min revive fresh worker;B-C `max-tasks-per-child` 最终保险。Windows solo 单线程物理上限,任何同进程方案都无法强杀不响应 cancel 的协程
2. **层2 supervisor 自身 park**:只 `asyncio.sleep`,不碰 DB/网络,概率极低;层3 兜底 → **watchdog 不可退役**(否决"退役 trace-beat"的核心理由)
3. **同步 CPU 段 >600s**:层1 整 op 上界覆盖;需审计热路径无 >600s 纯同步段(低概率)

---

## 8. 关键事实勘误(均 Opus 主体实测复核)
- `database.py:13` = **NullPool** ✅(决定性,推翻池毒化)
- `runner.py:345` `await persist(...)` = **唯一无 `_with_timeout` 裸长 await** ✅(:347 beat 在成功后,await 本身无界)
- `evaluation.py:2230` flip-retry = **`asyncio.gather`** ✅(并发非串行,F6 误报前提不成立)
- 否决方案 2 的额外理由:snippet 用 `get_redis`,实际是 `redis_pool.py:get_redis_client`(ImportError)

---

## 9. workflow 元信息
- 19 agent / 1.69M token / 5230s。2 个设计 agent(liveness-vs-productivity / cooperative-instrumentation)未产出结构化输出(StructuredOutput 重试失败),综合仍基于 3 完整方案 + 验证完成
- 评分:operation-aware 3/6 > external-probe 2/6 > liveness-productivity 0/6(但综合是嫁接非选最高分:主干 operation-aware 的 per-coroutine 判据 + liveness-productivity 的分层语义,否决 external-probe)
