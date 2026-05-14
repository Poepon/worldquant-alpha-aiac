# V-27 审查 — 待办项 backlog

> 来源:`docs/quality_review_mining_task_2026-05-14.md`(V-27 系列 160+ 条)
>      + `docs/code_review_v27_fixes_2026-05-14.md`(对 12 个 V-27 commit 的对抗式复审)
>      + A–D 段实现的对抗式代码审查(2026-05-15,4 个 agent 并行)
> 更新:2026-05-15(A–D 段实现 + 审查 followup `305fdbb` 后)

## 已闭环

V-27 审查的 bug 修复(三轮)+ 长期架构清理(四阶段)已完成并 push,
本会话 12 个 commit(`88db1b4`…`36222f5`)。覆盖审查"优先级建议"的
首轮 / 次轮 / 第三轮全部可在修复轮干净做完的项,以及"长期架构清理"的
4 类(死代码下架 + config 化 / G-refine 死链下架 / correlation 词汇表
统一 / node 纯读 session 收敛)。

**code review followup**(commit `a62a748`):对抗式复审发现 1 个真回归
(V-27.2 `_recently_revived` naive/aware 比较 TypeError → 该缓解完全不
生效)+ 6 项半修收尾(V-27.123/94/118/3/91/158),已修并 push。复审
点出但**判 backlog** 的项见下方 A/C/D 段新增行。

**A–D 段实现 + 审查 followup**(PR #1 `v27-backlog-a-root-fixes` →
master,merge `94b95e9`):A–D 段经 plan 审批后落地(commit
`73dee3f`…`38ef674`),再对实现做对抗式代码审查(4 个 agent),修掉
1 个真问题 + 4 项半修(commit `305fdbb`):🔴 V-27.1 锁无 TTL 续期、
🟡 V-27.81 `release_simulate_slot` 盲删→CAS、🟡 V-27.81 flip-retry
release 包 try/finally、🟡 V-27.126 `BRAIN_PENDING` 接 submit gate-4、
🟡 V-27.154 migration 改 `CREATE INDEX CONCURRENTLY`。审查发现但**转
后续**的 3 项见下方 F 段。

本文档收录**明确决策为 backlog** 的项 —— 不是"没看到",是"看了、判断
不宜在修复/清理轮做"。逐条价值低的 🟡/🟢 tech-debt 不在此逐列(见 E 段)。

---

## A. ✅ 已闭环 — RCA 方案 A 已落地(2026-05-14)

根因是设计/架构性问题,各写了一份 RCA 作为 plan 输入。经 plan 审批后
落地两份 RCA 各自推荐的方案 A,分支 `v27-backlog-a-root-fixes`,2 个
独立 commit。

| 项 | RCA | 落地方案 | commit |
|---|---|---|---|
| **V-27.1** watchdog force-clear cascade lock race | `docs/rca_2026-05-14_v27_1_cascade_lock_race.md` | 锁 value 升级为结构化 JSON + 原子 takeover + cascade 主循环每 round 边界 ownership 自检(UNKNOWN 安全底线);watchdog 改 takeover 替代 force_clear;`CASCADE_LOCK_TAKEOVER_ENABLED` kill-switch;`CASCADE_WATCHDOG_DEAD_MIN` 15→25。28 个集成测试 | `73dee3f` |
| **V-27.92 / 71 / 100 / 120 / 61** Hypothesis 状态机 vs `refresh_stats` 双轨 | `docs/rca_2026-05-14_v27_92_hypothesis_state_machine_dual_track.md` | 新增 `hypothesis_round_stats` 表作状态机唯一权威输入;`should_abandon` 改 async 读 DB;计数拆 real/flip/retryable(71/61);`rounds_active` 改读新表(120);`HYPOTHESIS_ABANDON_USE_DB_STATS` kill-switch。13 个集成测试 | `98a6f8d` |

落地前的临时缓解(V-27.2 `_recently_revived`、V-27.68 `alpha_count`
守卫)保留 —— 现在是 takeover / DB 真值之外的额外概率缓冲,不再是
唯一防线。

V-27.61(retryable alpha 计入 `alpha_count`)已并入 V-27.92 簇一起根治:
计数拆类时 retryable 单列、不进 `alpha_count`,且 `node_save_results`
失败 loop 跳过 retryable 不写 `AlphaFailure` —— 与 `refresh_stats` 的
`fail_count` 口径统一。

**审查 followup**(`305fdbb`):V-27.1 实现缺锁续期 —— `CASCADE_LOCK_TTL_SEC`
默认 3h 短于 CONTINUOUS_CASCADE worker 寿命,健康长跑 worker 会在 3h 时锁
过期、下个 round 边界自检读到 `MISSING` 自我终止,且过期窗口里 watchdog
可 takeover 已释放的锁导致双跑。已加 `renew_cascade_lock`(Lua CAS EXPIRE),
`_verify_cascade_ownership` 在 `OWNED` 时一并续期。

---

## B. ✅ 已闭环 — TOCTOU race 已根治(2026-05-14)

两个 race 都是跨进程 SELECT-then-act,经 plan 审批后落地,分支
`v27-backlog-a-root-fixes`,2 个独立 commit。

| 项 | File | 落地方案 | commit |
|---|---|---|---|
| **V-27.45** | `generation.py` node_hypothesis V-22.13 hypothesis reuse | 把校验放到 race window 末端:`hypothesis_service.filter_terminal_ids` 在 alpha/failure INSERT 时(`_incremental_save_alphas` + `workflow.run_with_persistence`)校验 hypothesis 是否已终态,终态则 link 置 NULL(行仍正常落库)。`HYPOTHESIS_REUSE_TERMINAL_GUARD_ENABLED` kill-switch。6 个集成测试 | `d472660` |
| **V-27.81** | `evaluation.py` node_simulate dedup + flip-retry dedup(2 处) | Redis 占位锁 `claim_simulate_slot`(SET NX EX,fail-open)直接防重复 simulate;`filter_unsimulated_expressions` 后拿锁才 simulate,拿不到标 in-flight duplicate。migration 加 `(expression_hash,region,universe)` 非唯一复合索引(不加唯一约束 —— hash 不含 settings;现存 48 组重复保留)。`SIMULATE_DEDUP_LOCK_ENABLED` kill-switch。9 个测试 | `3999720` |

**审查 followup**(`305fdbb`):两处半修收尾 —— ① `release_simulate_slot`
原是盲删(slot value 常量 `"1"`),TTL 过期被他人重领时会误删别人的锁
(V-26.4 模式);改 `claim_simulate_slot` 返回 per-claim token、release 走
Lua CAS。② flip-retry simulate 循环包 `try/finally`,中途 raise 也释放
已 claim 的 slot,不再泄漏到 900s TTL。

---

## C. ✅ 已闭环 — correlation / submit 边界完整性(2026-05-14)

11 项中 **8 项已落地**(commit `813ce6b` correlation/brain + `b9720f4`
submit gate/jsonb/beat),**3 项核实后不做**。

### 已落地(8 项)

| 项 | File | 落地方案 | commit |
|---|---|---|---|
| **V-27.126** | `correlation_service.get_with_fallback` | 新增 `CorrSource.BRAIN_PENDING` —— BRAIN 返回 well-formed dict 但 `max=None`(仍在算)与 `UNKNOWN`(算不出)区分。**审查 followup `305fdbb`**:原本枚举加了却无 caller 真用(submit gate-4 因 corr=None 短路,等价 UNKNOWN);现 gate-4 显式处理 `BRAIN_PENDING` → 返回 `retryable=True` reason"corr 计算中,稍后重试" | `813ce6b` + `305fdbb` |
| **V-27.128** | `brain_adapter.get_alpha_pnl` | 从 `_request` 改用 `_safe_api_call`(与 `get_alpha` 一致),获得跨进程 rate-limit cooldown/retry,不再静默吞 429 | `813ce6b` |
| **V-27.129** | `correlation_service._fetch_pnl_series` | 统一两条失败路径 —— "三次都异常"不再 raise,与"三次都空"一样返回 empty Series,`last_exc` 降级 warning | `813ce6b` |
| **V-27.102** | `brain_adapter.submit_alpha` | 成功判定 `status == 200` → `200 <= status < 300` | `813ce6b` |
| **V-27.157** | `correlation_service._fetch_os_alpha_ids` | `a["id"]` → `a.get("id")` + 缺 id 跳过,不再 KeyError 炸整个 OS sync(注:实际在 `correlation_service.py` 非 `sync_tasks.py`) | `813ce6b` |
| **V-27.127** | `alpha_service.submit_alpha` | gate-3 `can_submit=False` 且 failed checks 全是 self-corr 类时放行到 gate-4 实时 precheck 翻案;非 self-corr FAIL / `can_submit=None` 仍 hard-block;`SUBMIT_GATE_LIVE_SELF_CORR_OVERRIDE` flag。收尾:submit 成功 stamp `date_submitted` 改 `datetime.utcnow()`(naive 列) | `b9720f4` |
| **V-27.140** | `alpha_service.refresh_can_submit` | metrics 写入从整列 read-modify-write 改 SQL 原地浅合并 `metrics \|\| patch::jsonb`,消除并发后写覆盖前写其他 key | `b9720f4` |
| **V-27.147** | `alpha_service.submit_alpha` | 新增 `refresh_portfolio_skeletons_all` beat 任务(每 6h 扫 distinct submitted region 逐个 refresh,per-region 容错);submit 后 inline refresh 保留作主路径 | `b9720f4` |

### 核实后不做(3 项)

| 项 | 判定 |
|---|---|
| **V-27.121** `brain_adapter.submit_alpha` poll | 无 `Retry-After` 头当终态 —— **无法在代码层根治**,需先拿到 BRAIN submit 响应 body 的正式契约。维持现状。 |
| **V-27.3 存量空壳** | 已查真实 DB:**0/17** 个 dataset 缺 DataField 行,`field_count` 列与实际完全一致 —— 无历史空壳数据,不需要回填脚本。 |
| **V-27.158 词汇表** | `calc_self_corr_by_window` per-window status 是不同维度,**有意不并入** `CorrSource`。维持现状。 |

---

## D. ✅ 已闭环 — 性能 / 低触发脆弱性(2026-05-14)

6 项中 **2 项落地**(commit `77f26ec`),**4 项核实后不做**。

### 已落地(2 项)

| 项 | File | 落地方案 | commit |
|---|---|---|---|
| **V-27.154** | `routers/factor_library.py` `list_alphas_by_tier` / `refresh_iqc_batch` | migration `8100862bcef9` 加 `ix_alphas_submittable_self_corr` —— 对 `((metrics->>'_self_corr')::float)` 的 PARTIAL 表达式索引,WHERE 匹配两处共同的 `can_submit IS TRUE AND date_submitted IS NULL` 过滤前缀。**审查 followup `305fdbb`**:原 migration 用默认 `CREATE INDEX`(建索引时 `ACCESS EXCLUSIVE` 锁 alphas 全表);改 `CREATE INDEX CONCURRENTLY` + `op.get_context().autocommit_block()` | `77f26ec` + `305fdbb` |
| **V-27.155** | `routers/factor_library.py` `refresh_iqc_batch` | `eta` 从 `enqueued*2` 改为 `last_countdown`(跟踪最后成功入队任务的真实 `i*2`)—— enqueue 失败时 `i` 超出 `enqueued`,旧 eta 低估实际排队时间 | `77f26ec` |

### 核实后不做(4 项)

| 项 | 判定 |
|---|---|
| **V-27.72** `correlation_service` / `brain_adapter` 用 redis | **已核实已解决** —— `brain_adapter.py` 已全用 `redis.asyncio`,`correlation_service.py` 根本不用 redis,`redis_pool.py` 同步 client 只在 Celery worker 进程跑(无 event loop,不阻塞)。 |
| **V-27.160** crisis-window | crisis corr 要不要 gate `quality_status` / submittable 是**产品决策**,非代码问题。待产品定方向后再实施。 |
| **V-27.93** node 自开 `AsyncSessionLocal()` | backlog 标为「观察连接池指标」的观察项,无明确代码修复点。 |
| **V-27.132 / 133** `portfolio_skeletons._NUM_RE` | 真实误配概率极低(mining 表达式全函数式、双因子 match 要 fields 集合全等),backlog 自标低优先;需引入 tokenizer,性价比低。 |

---

## F. 待处理 — A–D 段实现的代码审查发现(2026-05-15)

对 A–D 段实现做对抗式代码审查(4 个 agent),1 个真问题 + 4 项半修已在
`305fdbb` 修掉(见上方各段 followup)。以下 3 项审查时明确**转后续**,
未在本轮动 —— 都不是会崩溃的回归,但应排期。

| 项 | File | 现象 | 根治方向 |
|---|---|---|---|
| **V-27.92 savepoint 粒度** | `agents/graph/nodes/persistence.py` `_process_hypothesis_feedback` | 一轮多个 hid 共用**一个** `begin_nested()` savepoint —— 第 2 个 hid 的 `upsert_round_stats` 失败回滚,会连带回滚第 1 个已成功的 upsert,放大单 hid 失败的丢数据面 | savepoint 移进 `for hid` 循环内,逐 hid 隔离 |
| **V-27.92 flip-only 轮 attribution 隐身** | `persistence.py` attribution + `should_abandon` | 一轮全是 flip 产物时 `real_alphas=[]`、`alpha_count=0`:V-27.68 guard 挡住 abandon、`mark_active` 因 count=0 不触发 —— hypothesis 实际产出了 flip alpha 却既不 active 也不计入,RCA 方案 A 第 4 点"flip 产物 attribution 单独标"未实现 | flip-only 轮单独标 attribution / 给 hypothesis 一个 flip-active 状态 |
| **V-27.1 claim 时序** | `tasks/session_watchdog.py` + `tasks/mining_tasks.py` claim 路径 | watchdog `db.commit()` → `takeover_cascade_lock` 顺序下,若 `config_snapshot` 因任何原因没落库,新 worker `_handed_token=None` 走 normal acquire 撞 takeover 锁 → 直接当 duplicate 退出,task 无人跑直到 3h TTL | 先确保 config_snapshot 持久化再 takeover,或 normal-acquire 失败时检查 holder lineage 是否 `WATCHDOG_TAKEOVER` 并认领 |

前两项归属 V-27.92 状态机簇,建议与该簇的后续迭代一起做;V-27.1 claim
时序是独立小修,优先级低于前两项(触发需 config_snapshot 落库失败这一
本就罕见的前提)。

---

## E. 大批 🟡 / 🟢 tech-debt — 未逐列

审查文档 V-27.4~60、83~120 区段里,本会话三轮 + 架构清理未覆盖的剩余
🟡 中等 / 🟢 改进项(数十条),多为:裸 `except:` 吞异常、magic number、
重复逻辑、aspirational 注释、模块级常量在 import 时快照 settings 等。

逐条价值低、互相独立,**不单独排期**。建议:
- 触碰相关文件时顺带清理(opportunistic)
- 或某次专门起一个"tech-debt 批量清理" sprint

清单见 `docs/quality_review_mining_task_2026-05-14.md` 的 🟡 / 🟢 段,
比对本会话 12 个 commit 的 V-27.X 编号即可得出未覆盖项。

---

## 优先级建议

1. ~~**A 段**(V-27.1 / V-27.92)~~ — ✅ 已闭环(2026-05-14,commit `73dee3f` / `98a6f8d`;审查 followup `305fdbb`)
2. ~~**B 段 TOCTOU**(V-27.45 / 81)~~ — ✅ 已闭环(2026-05-14,commit `d472660` / `3999720`;审查 followup `305fdbb`)
3. ~~**C 段**(8 项落地 + 3 项核实不做)~~ — ✅ 已闭环(2026-05-14,commit `813ce6b` / `b9720f4`;审查 followup `305fdbb`)
4. ~~**D 段**(2 项落地 + 4 项核实不做)~~ — ✅ 已闭环(2026-05-14,commit `77f26ec`;审查 followup `305fdbb`)
5. **F 段** — A–D 段实现的代码审查发现的 3 项遗留,**应排期**:V-27.92 savepoint 粒度 + flip-only attribution(并入 V-27.92 状态机簇后续)、V-27.1 claim 时序(独立小修,低优先)
6. **E 段** — 数十条 🟡/🟢 tech-debt,**维持不单独排期**(opportunistic 顺带清理,或专门 sprint)

A–D 段实现 + 审查 followup 已闭环;backlog 余 F 段(3 项应排期)+ E 段(性质上不排期)。
