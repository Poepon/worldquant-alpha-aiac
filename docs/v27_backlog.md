# V-27 审查 — 待办项 backlog

> 来源:`docs/quality_review_mining_task_2026-05-14.md`(V-27 系列 160+ 条)
>      + `docs/code_review_v27_fixes_2026-05-14.md`(对 12 个 V-27 commit 的对抗式复审)
> 更新:2026-05-14(code review followup 后)

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

---

## B. ✅ 已闭环 — TOCTOU race 已根治(2026-05-14)

两个 race 都是跨进程 SELECT-then-act,经 plan 审批后落地,分支
`v27-backlog-a-root-fixes`,2 个独立 commit。

| 项 | File | 落地方案 | commit |
|---|---|---|---|
| **V-27.45** | `generation.py` node_hypothesis V-22.13 hypothesis reuse | 把校验放到 race window 末端:`hypothesis_service.filter_terminal_ids` 在 alpha/failure INSERT 时(`_incremental_save_alphas` + `workflow.run_with_persistence`)校验 hypothesis 是否已终态,终态则 link 置 NULL(行仍正常落库)。`HYPOTHESIS_REUSE_TERMINAL_GUARD_ENABLED` kill-switch。6 个集成测试 | `d472660` |
| **V-27.81** | `evaluation.py` node_simulate dedup + flip-retry dedup(2 处) | Redis 占位锁 `claim_simulate_slot`(SET NX EX,fail-open)直接防重复 simulate;`filter_unsimulated_expressions` 后拿锁才 simulate,拿不到标 in-flight duplicate。migration 加 `(expression_hash,region,universe)` 非唯一复合索引(不加唯一约束 —— hash 不含 settings;现存 48 组重复保留)。`SIMULATE_DEDUP_LOCK_ENABLED` kill-switch。9 个测试 | `3999720` |

---

## C. ✅ 已闭环 — correlation / submit 边界完整性(2026-05-14)

11 项中 **8 项已落地**(commit `813ce6b` correlation/brain + `b9720f4`
submit gate/jsonb/beat),**3 项核实后不做**。

### 已落地(8 项)

| 项 | File | 落地方案 | commit |
|---|---|---|---|
| **V-27.126** | `correlation_service.get_with_fallback` | 新增 `CorrSource.BRAIN_PENDING` —— BRAIN 返回 well-formed dict 但 `max=None`(仍在算)与 `UNKNOWN`(算不出)区分,可重试型 caller 能利用 | `813ce6b` |
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

## D. 性能 / 半成品 / 低触发脆弱性

| 项 | File | 现象 | 备注 |
|---|---|---|---|
| **V-27.154** | `routers/factor_library.py` `list_alphas_by_tier` | `submittable` 过滤每行对 `metrics->>'_self_corr'` 做 JSONB 提取 + 双 cast,无表达式索引 — 当前数据量小不致命,随表增长全表扫 | 加 `metrics->>'_self_corr'` 的表达式索引,或物化成 alpha 表的列 |
| **V-27.155** | `routers/factor_library.py` `refresh_iqc_batch` | `countdown=i*2` 按 i 递增、`eta=enqueued*2` 用成功数 — enqueue 失败时 eta 与实际最后任务的 countdown 不一致 | 用 `i` 算 eta,或忽略(罕见) |
| **V-27.160** | `evaluation.py` crisis-window 评估节点 | crisis stress test 跑了 N×N 计算,结果只塞进 `metrics._crisis_correlations`,唯一出口是 AlphaDetail 一张卡片 — 既不 gate `quality_status` 也不进 submittable 过滤 | **产品决策**:crisis corr 要不要影响提交判定?定了再实施 |
| **V-27.132 / 133** | `portfolio_skeletons.py` `_NUM_RE` | 数字提取位置对齐非语义、负号 `a-1` vs `subtract(a,1)` 抽出符号相反 | 实际 mining 表达式全函数式(无中缀),且双因子 match 要 fields 集合也全等才命中 — 真实误配概率低,**低优先** |
| **V-27.72** | `correlation_service` / `brain_adapter` 用 redis | 同步 redis client 在 async 路径里调用,阻塞 event loop —— **非本次引入**(既有代码),量小未致命 | 迁 `redis.asyncio`,或确认调用频次低到可接受 |
| **V-27.93** | node 自开 `AsyncSessionLocal()` | 阶段 D 收敛了纯读位点,但仍保留的写位点 + 未注入 config 的节点各自开 session,高并发 round 下连接池压力 | 观察连接池指标;必要时调 pool size 或进一步收敛 |

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

1. ~~**A 段**(V-27.1 / V-27.92)~~ — ✅ 已闭环(2026-05-14,commit `73dee3f` / `98a6f8d`)
2. ~~**B 段 TOCTOU**(V-27.45 / 81)~~ — ✅ 已闭环(2026-05-14,commit `d472660` / `3999720`)
3. ~~**C 段**(8 项落地 + 3 项核实不做)~~ — ✅ 已闭环(2026-05-14,commit `813ce6b` / `b9720f4`)
4. **D / E 段** — 低优先,opportunistic 或专门 sprint,现为剩余项
