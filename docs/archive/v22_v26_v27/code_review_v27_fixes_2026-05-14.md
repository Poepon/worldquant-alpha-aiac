# V-27 修复批次对抗性 Code Review

**日期**: 2026-05-14
**审查者**: Claude(5 路并行对抗性 reviewer)
**审查范围**: commit `7e9b967..bc0fa2b` 共 12 个 commit —— V-27 审查首轮/次轮/三轮修复 + 架构清理 4 阶段 + 2 份 RCA + backlog
**方法**: 每个 reviewer `git show` 对应修复 commit diff → 打开当前文件读上下文 → 对每个 claimed 修复判定:真修对 / 半修治标 / 引入回归 / 未修。对抗性靶点 —— 修复本身有没有问题、有没有把 V-26/早期修复改回去、删代码删干净没有。

> **总评**: 架构清理 4 阶段质量很高(删得干净、无残留引用、无 Alembic 缺失、无语义改变,无 🔴)。修复批次里有 **1 个真回归**(V-27.2 运行时崩溃)+ 几个**半修**;submit 子系统 V-27.121/123 的残缺互相叠加放大了后果。RCA backlog 标注诚实。

**审查的 commit**:
| commit | 主题 | reviewer 结论 |
|---|---|---|
| `88db1b4` | submit gate + correlation 11 项 | 多数真修对,V-27.121/123 半修 |
| `cd83d0f` | watchdog 双跑 + 401 自愈 + rate-limit 原子 | V-27.2 回归,V-27.91 半修,V-27.7/72 真修对 |
| `d2820bf` | KB 事务隔离 + node_evaluate 写穿 + V-27.1 RCA | V-27.63/93 真修对 |
| `85fe5c6` | node 健壮性 + KB 污染 4 项 | 全部真修对 |
| `cbcec8e` | sync 维度 + RAG 自锁 + 评分链编造数据 | V-27.77/78/95 真修对,V-27.3 半修 |
| `690b9fd` | retryable 重试 + sync 维度 + V-27.92 RCA | V-27.14 真修对,V-27.61 诚实半修 |
| `8749252` | 非原子文件写 + 跨进程 latch | V-27.130/131 真修对,V-27.94/118 半修 |
| `721c298` | 架构清理A:死代码下架 + config 化 | 真修对 |
| `9ce2c38` | 架构清理B:G-refine 死链下架 | 真修对,删得干净 |
| `3dc44a7` | 架构清理C:correlation 词汇表统一 CorrSource | 真修对,遗漏 1 caller |
| `36222f5` | 架构清理D:node 纯读 session 收敛 | 真修对(限范围) |
| `bc0fa2b` | docs: V-27 待办汇总 backlog | 如实,无粉饰 |

---

## 🔴 必须改

### 1. V-27.2 修复引入运行时崩溃 —— `_recently_revived` naive/aware datetime 比较
**File**: `backend/tasks/session_watchdog.py:155-162` **关联**: V-27.2
新增的去重逻辑 `return last is not None and last > cutoff`:`ExperimentRun.started_at` 在 `backend/models/task.py:83` 是 `Column(DateTime)` —— **没有 `timezone=True`**,asyncpg 返回 naive datetime;而 `cutoff`(`dead_cutoff`)是 `datetime.now(timezone.utc)` 减出来的 aware datetime。`naive > aware` 直接 `TypeError: can't compare offset-naive and offset-aware datetimes`。
对比:现有代码能跑是因为 `last_alpha_persisted_at`(task.py:52)、`TraceStep.created_at`(task.py:114)都是 `DateTime(timezone=True)`。
**后果**: 每次 watchdog tick 进到去重分支就抛异常 —— cascade 分支异常向上冒泡可能中断整个 revive 循环;即使被吞,V-27.2 的去重**完全不生效**,等于没修。设计是对的(去重接入点正确,符合 RCA 方案 C),实现挂在一行。
**修复方向**: `started_at` 加 `timezone=True`(需 Alembic)或比较前 `.replace(tzinfo=timezone.utc)`。

### 2. V-27.123 submit 并发保护只防住「都成功」,没防住「都提交」
**File**: `backend/services/alpha_service.py:386-394` **关联**: V-27.123
`SELECT ... FOR UPDATE` 方向对,但两个实质问题:
- **(a) 长事务持锁**: winner 的事务跨越整个 `submit_alpha`(含 `get_with_fallback` 走 BRAIN API + `brain_adapter.submit_alpha` 轮询,可能数十秒),loser 一直阻塞在 `SELECT FOR UPDATE` 上。等于把一次外部 HTTP 提交塞进 DB 行锁临界区,高并发下连接池被这种长事务吃光。
- **(b) 无唯一约束**: `date_submitted` 仍无唯一约束,winner 若轮询超时(见 #3)返回 `success=False` **不** stamp `date_submitted` 也不报错,锁释放后 loser 进来照样 POST —— 两个都「失败」但都烧了一个不可逆的 BRAIN slot。行锁只防住了「都成功」。
**修复方向**: DB 层状态机 —— 加 `submit_state` 列 + `UPDATE ... WHERE submit_state='none'` 判影响行数,而非长事务行锁。

### 3. V-27.121 submit 轮询只修了一半
**File**: `backend/adapters/brain_adapter.py:1389-1437` **关联**: V-27.121 / V-27.122
`reached_terminal` 标志正确区分了「max_polls 耗尽(超时)」—— **V-27.122 真修对**。但 V-27.121 的核心场景是「首个 POST 返回 200 且**无 Retry-After**」:第一次 `while` 迭代就 `reached_terminal=True; break`,落到 `success = resp.status_code == 200` 判成功。BRAIN 异步 job 在任务还在跑时同样可能返回 200 无头。修复假设「无 Retry-After ⇒ 终态」,而 V-27.121 质疑的正是这个假设,根因未消除。
**叠加风险**: V-27.121 留下的「轮询超时 `success=False` 不 stamp」口子,恰恰喂给 #2 的「都失败但都 POST」—— 两项残缺互相放大。

### 4. V-27.94/118 latch 迁 Redis 但 `exists`-then-`set` 非原子
**File**: `backend/adapters/brain_adapter.py:638-685` **关联**: V-27.94 / V-27.118
迁移方向对(class 属性 → Redis key + TTL),消除了「per-process 独立 latch」和「worker 重启才解 latch」。但 `exists()` 检查(:640)与 403 后的 `set(...)`(:678)之间**无锁/无原子操作**:N 个 worker(或 V-20.1 并发 round)同时看到 key 不存在 → 全部 fall through → 全部 POST list payload → 全部收 403 → 全部各自 `set`。
**惊群只从 N×workers 压到「每个 latch 周期一次 N-wide 惊群」,没消除。** commit message 自称「与 `_SLOT_COUNTER_KEY` 跨进程设计对齐」,但 `_SLOT_COUNTER_KEY` 用原子 `INCR`,这里用非原子 `exists`-then-`set`,对齐的只是「放 Redis」不是「原子」。
**修复方向**: `SET NX` —— 第一个探测者写占位 key,其余 worker `SET NX` 失败即知「正在探测中」走 single-sim。

---

## 🟡 应该改

### V-27.3 半修 —— universe + UPDATE 修对了,但漏了「触发 field sync」
**File**: `backend/tasks/sync_tasks.py:232-318` **关联**: V-27.3
cbcec8e 把 beat 版 `sync_datasets` 从 INSERT-only 改成 INSERT+UPDATE 并补写 `universe=DEFAULT_UNIVERSE`,字段集与 manual 版完全一致 —— 这两项真修对。但 V-27.3 原文明确点了**三项**,第三项「不触发 field sync」未落实:manual 版 `sync_datasets_from_brain` 在 `:391-399` 有 `sync_fields_from_brain.delay(...)` 循环,beat 版至今没有。后果:beat 同步进来的新 dataset 有 `field_count` 数字,但 `DataField` 表里没有对应行 → `_get_dataset_fields` 仍拿不到字段 → 挖矿仍看不到,dataset 依旧「可见但空壳」。

### V-27.91 半修 —— `_safe_api_call` 401 自愈是一次性的,失败即放弃
**File**: `backend/adapters/brain_adapter.py:1221-1227` **关联**: V-27.91
抽 `_coalesced_reauth` helper 是真修对:`_safe_api_call` 的 401 分支现在接上了 `_auth_lock` coalescing、`_invalidate_session_cache()`(V-26.24)、`_is_auth_error` body marker(V-22.7),与 `_request` 共用 helper 无重复造轮子。**但两个收尾缺口**:
- 重试后不再检查 auth-error —— `_request`(:1154-1158)重试后会 `_is_auth_error` 再判一次,`_safe_api_call`(:1227)重试后直接走到 429/5xx 判定,reauth 后单次重试仍 401(cookie 传播延迟)时 401 会外泄给调用方。
- `_coalesced_reauth` 返回 False 时不重试、不退避、不记日志,response 保持原 401 往下。
`_safe_api_call` 的 `while retries < 5` 循环对 auth-error 没有 `continue` 重试分支(只 429/5xx 有)。建议:reauth 失败或重试仍 auth-error 时 `retries += 1; continue`。

### V-27.72 真原子了,但留一个治标尾巴
**File**: `backend/agents/graph/nodes/persistence.py:440-446` **关联**: V-27.72
Lua 脚本本身**真原子**:`INCR` + 条件 `EXPIRE` 在单个 `eval` 内,且 `TTL < 0` 分支 self-heal 了 pre-fix 崩溃留下的无 TTL 孤儿 key —— 这部分修对。但 `cli = get_redis_client()` 返回**同步** `redis.Redis`,`cli.eval(...)` 是阻塞调用,在 async `_incremental_save_alphas` 里阻塞 event loop(非本次引入,旧的 `incr`/`expire` 也同步,但修复时没顺手解决)。可选改法:async redis client 或 `run_in_executor`。

### V-27.158 半修 —— CorrSource 重构遗漏 evaluation.py caller
**File**: `backend/agents/graph/nodes/evaluation.py:1000-1003` **关联**: V-27.158
`calc_self_corr` / `get_with_fallback` 统一为 `CorrSource` StrEnum,`alpha_service.py` caller 已迁移到常量。但 `evaluation.py` 的 `get_with_fallback` caller 仍用裸字符串 `"unknown"` 比较(StrEnum 向后兼容所以不崩),`:1003` 注释 `# get_with_fallback returns None for source="unknown"` 也成了过时契约。「统一词汇表」目标做了一半。

### V-27.93 真修对,但独立 session 有代价
**File**: `backend/agents/services/rag_service.py:962-1024, 1044-1276, 1280-1311` **关联**: V-27.93
`record_failure_pattern` / `record_success_pattern` / `update_pattern_brain_status` 三个写方法全部改用 `async with AsyncSessionLocal() as kb_db`,lookup 走同一独立 session,`except` 删掉了 `self.db.rollback()`(async-with 自动回滚)。grep 确认 record/update 路径无 `self.db.commit()` 残留 —— 事务夹带根除,改得干净。**代价**:(1) 独立 session 在 caller 提交前查不到那条 alpha,将来若有 KB 行需要 FK 校验 `alpha_id` 会误判(当前无校验所以不爆,但注释只讲了好处没点明代价);(2) 每次 record 新开连接,叠加 `_track_retrieval_hit` 的独立 session,高频 feedback 批次下连接池压力 —— 建议确认 `AsyncSessionLocal` pool size。

### V-27.61 诚实半修 —— retryable held-at-PENDING,完整 re-enqueue 转 backlog
**File**: `backend/agents/graph/nodes/evaluation.py:694-710, 882-897` **关联**: V-27.61
`node_simulate` 真接上了 `res.get("retryable")`:retryable alpha 保持 `is_simulated=False` + 打 `_sim_retryable` tag,`node_evaluate` 读到就 `continue` 不判 FAIL。下游核实:held-PENDING alpha 不持久化、不计 fail、不进 failure queue —— 比修复前「永久写死 FAIL 污染 KB」是实质改善。**但**:retryable alpha 仍计入 `_process_hypothesis_feedback` 的 `alpha_count = len(pending_alphas)`(persistence.py:807),全 retryable 的 round 会被记成「失败 round」喂状态机。完整 re-enqueue 需主循环配合,commit message 已如实标注转 backlog —— 治标但诚实。

### 未修(列在 V-27 范围内但本批 commit 未碰,且未列 backlog)
| V-27.X | 文件 | 说明 |
|---|---|---|
| V-27.102 | `brain_adapter.py:1433` | submit `success == 200` 仍排除 201/202 async-accept |
| V-27.127 | `alpha_service.py:399` | 两道 gate self_corr 来源/时点不自洽,gate 顺序未调 |
| V-27.140 | `alpha_service.py:328-339` | `refresh_can_submit` 整 JSONB read-modify-write 无行锁隔离 |
| V-27.157 | `correlation_service.py:234` | `_fetch_os_alpha_ids` 的 `a["id"]` 直接下标,缺 id 即 KeyError 中断整批 |
| V-27.147 | `alpha_service.py:438-444` | skeleton 缓存刷新失败仍 `logger.warning` 吞掉,commit message 称「self-heal」但无机制保证 |

---

## ✅ 真修对的(质量可靠,值得肯定)

### 架构清理 4 阶段(721c298 / 9ce2c38 / 36222f5)—— 无 🔴,质量高
- **G-refine 死链下架**: 全代码库 grep 确认 `hypothesis_refine` / `refine_hypothesis_llm` / `RefinedHypothesis` / `mark_superseded` / `find_unused_refined` **零生产残留引用**;`hypothesis_refine.py`(209 行)+ `test_phase2_g_refinement_loop.py`(395 行)整文件删除,`node_hypothesis` / persistence.py 的 G-refine 块整块删除无孤立变量。
- **Alembic 决策正确**: 明确**不动 schema**(`parent_hypothesis_id` 列 + `SUPERSEDED` enum 保留并注释弃用)—— 无迁移缺失问题。
- **死代码下架**: `route_check_error` / `merge_state` / `_select_exploration_fields` / `increment_pattern_usage` / legacy `_get_success_patterns` / `_get_failure_pitfalls` / `Annotated`+`operator.add` import 全部删除,grep 确认无调用方,删后 import/构图正常。
- **config 化完整**: `RAG_PITFALL_*`(6 项)、`RAG_SUCCESS_SCORE_*`(6 项)、`CRISIS_WINDOWS` 全部进 config.py,默认值与原硬编码一致,无「加了配置项代码还写死」;`CRISIS_WINDOWS` tuple→list 改动经 7 处消费方核对语义等价。
- **node 纯读 session 收敛**: 新增 `nodes/base.py` 的 `resolve_db`,node_hypothesis / node_code_gen / tier_seed 的纯读自开 session 转注入 `db_session`;V-22.13 reuse / Phase2 persist / node_simulate dedup 的 TOCTOU 风险点明确按 plan 跳过并如实收录 backlog。
- 唯一瑕疵:阶段D commit message 把 `node_tier_wrap_one` 错写成 `node_tier_strategy_select`(代码正确,仅 message 笔误)。

### 修复批次里真修对的
- **V-27.63** — `model_copy(deep=True)` 1 行即根因修复(对抗性核查:10 行 diff 够,顶层字段 + 嵌套 metrics 一次性全 detach);**V-27.62** 被这个 deep copy 一并消除。
- **V-27.77 / V-27.78** — sim_result test leg 不再编造 `sharpe*0.8`(空则 `{}`);BRAIN 无 checks 时 `not check_details` → PASS_PROVISIONAL + `_brain_checks_unverified` tag,堵住 score-only 旁路。
- **V-27.68 / V-27.73 / V-27.97** — KB 污染守卫全部对称落实:`should_abandon_hypothesis` 加 `alpha_count` 守卫(fail-safe,不误杀)、`node_save_results` KB 写入 `not alpha.alpha_id` skip、`record_failure_pattern` 加 hypothesis_id=None 守卫。
- **V-27.14** — `sync_user_alphas` OS-stage 改全量拉(捕捉 submit 翻转),IS-stage 保持增量;边界:`MIN_START_DATE`(2025-07-05)前的可接受文档化边界。
- **V-27.31** — `node_hypothesis` try/except → `_failed_llm_response`,异常路径下游不崩。
- **V-27.93 / V-27.95 / V-27.108 / V-27.111 / V-27.112 / V-27.116** — RAG 6 项 claimed 修复全部真修对;d2820bf 的 448 行大 diff 经核查是纯缩进搬移,**没有把 V-26 已修对的东西改回去**(V-26.9 running-average、V-26.12 family boost 原样保留)。
- **V-27.7** — quota_guard 日志移到 `active` 查询后,`{{?}}` 占位换成 `{len(active)}`。
- **V-27.130 / V-27.131** — crisis JSON / OS PnL pickle 改 tmp-then-rename 原子写,异常路径清理 tmp。
- **V-27.139 / V-27.141 / V-27.142 / V-27.146 / V-27.151** — region 缺失拒绝提交(移除所有 `or "USA"` fallback)、本地强信号独立 demote(三态 None 保留)、死参数 `skip_precheck` 删除、`list_alphas` router 补 self_corr 接线、`AsyncExitStack` 替代手工 dunder。
- **V-27.124** — crisis-window 改 per-pair `concat + dropna`,两侧对齐到同一索引(V-27.138 被一并吸收)。

### RCA backlog —— 如实标注
- **V-27.1**(`docs/rca_2026-05-14_v27_1_cascade_lock_race.md`)、**V-27.92**(`docs/rca_2026-05-14_v27_92_hypothesis_state_machine_dual_track.md`)—— 两份 RCA 准确,如实标注根因未根治、临时缓解已上线、根治排期方案 A;V-27.68 明确标注为「只减少误杀」的部分缓解。
- **`docs/v27_backlog.md`** —— A/B/C/D 段所列项与审查原文一致,未发现把「其实没做」的项粉饰成已修。

---

## 优先级建议

1. **立即** —— #1(V-27.2 一行 datetime 修复,否则该 commit 完全无效)
2. **本轮补** —— #2 + #3(V-27.123 / V-27.121 互相叠加,submit 是不可逆操作 + 烧 BRAIN slot,风险最高)、#4(`SET NX` 占位)
3. **下一批** —— V-27.3 补 field sync、V-27.91 重试收敛、V-27.158 迁移 evaluation.py caller、V-27.72 async redis client
4. **排期确认** —— V-27.102/127/140/157/147 列在 V-27 范围内但本批未碰也未列 backlog,需明确是排后续批次还是补进 backlog

---

## 闭环动作模板

每项 🔴/🟡 需落到下列之一:
1. **commit**(`fix: V-27.X follow-up — <一句话>`)
2. **backlog 补录**(docs/v27_backlog.md)
3. **RCA doc**(若根因复杂)

> **审查方法说明**: 5 路并行 reviewer 分子系统(submit+corr / watchdog+auth / RAG / graph nodes / 架构清理),每路 `git show` 对应 commit + 逐文件核对行号。「真修对 / 半修 / 回归 / 未修」判定基于「读修复 diff → 读当前代码 → 判断根因是否消除 + 有无新引入问题」。架构清理批次额外做了全代码库 grep 验证删除残留。
