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

## A. 已有 RCA — 待排 plan

根因是设计/架构性问题,bug-fix 轮贸然改风险高,已各写一份 RCA 作为
plan 输入。

| 项 | RCA | 根治方向 | 工时估 |
|---|---|---|---|
| **V-27.1** watchdog force-clear cascade lock race | `docs/rca_2026-05-14_v27_1_cascade_lock_race.md` | 锁 takeover 语义 + cascade 主循环每 round 边界做所有权自检 | ~1.5-2 day |
| **V-27.92 / 71 / 100 / 120** Hypothesis 状态机 vs `refresh_stats` 双轨 | `docs/rca_2026-05-14_v27_92_hypothesis_state_machine_dual_track.md` | `should_abandon` 改读 DB 真值;`refresh_stats` 落库 per-round 明细;内存 history 降级为展示 cache | ~3-4 day |

临时缓解已上线:V-27.2 的 `_recently_revived`(缓解 watchdog 重复 revive,
naive/aware 比较 bug 已于 `a62a748` 修复 — 在此之前该缓解完全不生效)、
V-27.68 的 `alpha_count` 守卫(缓解 abandon 误杀)。均不根治。

并入本簇(code review followup 判 backlog):

| 项 | 现象 | 归簇原因 |
|---|---|---|
| **V-27.61** | `evaluation.py` node_simulate — retryable(BRAIN 偶发失败)的 alpha 仍计入 `alpha_count`,与"真产出 alpha"语义混淆 | `alpha_count` 是 V-27.92 簇 `should_abandon` 的输入之一,单独修会与状态机重构冲突;并入 V-27.92 RCA 一起处理 |

---

## B. TOCTOU race — 阶段 D 明确跳过

收敛 node 自开 session **不消除** TOCTOU race(race 是跨进程
SELECT-then-act,不是 session 隔离问题),反而可能引入新风险。需独立处理。

| 项 | File | race window | 根治方向 |
|---|---|---|---|
| **V-27.45** | `generation.py` node_hypothesis V-22.13 hypothesis reuse | `get_by_id` 读 status 后、alpha 写库前,B5 可能已把 hypothesis 改成 ABANDONED | reuse 校验加 `SELECT ... FOR UPDATE`,或 alpha INSERT 前再校验一次 status |
| **V-27.81** | `evaluation.py` node_simulate dedup + flip-retry dedup(2 处) | `filter_unsimulated_expressions` SELECT-then-simulate 之间其他 worker 可能已写同表达式 → 烧 BRAIN 配额 | dedup 加 expression-hash 唯一约束 / `INSERT ... ON CONFLICT` 占位,或接受重做 |

---

## C. correlation / submit 边界 — 回应审查时判 backlog

本会话 commit `88db1b4` 修了 self_corr 三层链的核心 bug,以下是边界/
完整性的残留,不影响主路径正确性:

| 项 | File | 现象 | 根治方向 |
|---|---|---|---|
| **V-27.126** | `correlation_service.py` `get_with_fallback` | BRAIN 返回 `max=None`(corr 仍在算)落到 `UNKNOWN`,没区分"算不出"vs"还没算完"——后者本应让 caller 稍后重试 | 加 `CorrSource.BRAIN_PENDING` 或返回 retry 提示 |
| **V-27.127** | `alpha_service.submit_alpha` vs `can_submit.py` | 第三道 gate 读陈旧 `can_submit` 列,第四道 gate 实时测 self_corr — 两份不同时点数据;旧高 corr demote 后实时变低则永远卡 can_submit=False | submit gate 第三道:can_submit=False 但原因仅 LOCAL_SELF_CORRELATION 时,允许实时 precheck 翻案 |
| **V-27.128** | `correlation_service._fetch_pnl_series` | 重试只救"BRAIN 偶发返回空 records 但 HTTP 200"(实测救回 38/40),救不了真限流(`get_alpha_pnl` 内部 `except: return {}` 吞了 429) | `get_alpha_pnl` 不吞 429 / 重试退避加长 |
| **V-27.129** | `correlation_service._fetch_pnl_series` | "三次都空"静默返回空 vs "三次都异常"抛出 — caller 走两条路径(前者 `(None,UNKNOWN)`,后者 try 捕获后落 BRAIN tier) | 统一:三次失败都返回 `(None, UNKNOWN)` 或都抛 |
| **V-27.147** | `alpha_service.submit_alpha` | submit 成功后 `refresh_portfolio_from_db` 抛异常只 `logger.warning` 吞掉,skeleton 缓存可能长期陈旧,无重试/补偿 | best-effort 可接受;或加一个 beat 兜底定期 refresh |
| **V-27.121** | `brain_adapter.submit_alpha` poll | 无 `Retry-After` 头时把响应当**终态**处理 —— 这是 `ace_lib` 确认的 BRAIN 契约假设,但 BRAIN 未在 body 给出明确 status 字段,无法在 HTTP 层自证。**无法在代码层根治**:需先拿到 BRAIN submit 响应 body 的正式契约(哪个字段表"已终结")才能改判定 | 前提依赖:BRAIN submit 响应 body 契约。拿到前维持现状 |
| **V-27.102** | `brain_adapter.submit_alpha` | submit 成功判定为 `status == 200`,排除了 201/202(BRAIN 实际只回 200,但若日后改用 202 Accepted 异步语义会漏判) | 改 `200 <= status < 300`,或显式列举可接受码 |
| **V-27.140** | `routers/factor_library.py` `refresh_can_submit` | JSONB `metrics` 字段 read-modify-write 无行锁,与其他写 metrics 的路径(IQC 回填、evaluation 落库)并发时后写覆盖前写 | `UPDATE ... SET metrics = metrics || '{...}'::jsonb` 原地 merge,或加行锁 |
| **V-27.157** | `sync_tasks._fetch_os_alpha_ids` | `a["id"]` 直接下标 —— BRAIN 返回项缺 `id` 时 `KeyError` 直接炸整个 OS sync,而非跳过该项 | 改 `a.get("id")` + 跳过 None |
| **V-27.3 存量空壳** | `sync_tasks.sync_datasets` | `a62a748` 让**新建** dataset 触发 `sync_fields_from_brain` —— 但老 buggy beat 已插入的 dataset 现在是"已存在 + 无 DataField 行",不走新建分支,字段不会回填,V-27.3 原文点的"已积累的空壳 dataset"仍空 | 下一批:一次性回填脚本(扫无 DataField 的 dataset 补 `sync_fields_from_brain.delay`),或先核实 DB 确无此类历史数据 |
| **V-27.158 词汇表** | `calc_self_corr_by_window` | `a62a748` 已把 `evaluation.py` caller 迁到 `CorrSource` 常量;残留:`calc_self_corr_by_window` 的 per-window status(ok/insufficient_data/empty_pool/missing_window)是不同维度,**有意不并入** | 若日后要统一,需设计跨维度的 status 体系 |

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

1. **A 段**(V-27.1 / V-27.92)— 排 plan,这是审查里唯一"根因未消除"的两簇
2. **B 段 TOCTOU**(V-27.45 / 81)— 影响生产数据正确性 + 烧 BRAIN 配额,次优先
3. **C 段** — 边界完整性,可随相关功能迭代顺带
4. **D / E 段** — 低优先,opportunistic 或专门 sprint
