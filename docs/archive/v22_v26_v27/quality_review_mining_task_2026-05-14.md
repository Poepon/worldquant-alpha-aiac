# Alpha Mining Task 全流程质量审查 — V-27 系列

**日期**: 2026-05-14
**审查者**: Claude(多角度对抗性审查,5 路并行 agent)
**审查范围**: 接续 V-26 全链路 —— Celery `run_mining_task` 入口 → LangGraph workflow(generation/validation/evaluation/persistence)→ BRAIN simulate/submit → KB 写回 + Hypothesis lifecycle + watchdog;**新增覆盖** V-26 之后落地的三块功能:submit 全栈(7e9b967)、crisis-window 相关性压测(b4c5f0d)、decay-curve 快照采集(23add72)
**严重度**: 🔴 阻断 / 🟡 中 / 🟢 改进
**编号约定**: `V-27.X` 顺接 V-26 系列

> 本审查不规定修法,只暴露问题。**两个靶点**:① 对抗性验证 V-26 那批修复 commit 是否真的修对了(半修 / 治标 / 引入回归 / 注释说修了实际没生效);② 找 V-26 漏掉的、或修复后新引入的问题。
> 每一项编号注明 file:line 锚点 + 类型(Bug / Race / Dead-code / Performance / Tech-debt / Half-done)。
> 子系统分工:V-27.1-30 编排与生命周期 · V-27.31-60 生成+校验节点 · V-27.61-90 评估+持久化节点 · V-27.91-120 BRAIN适配器+RAG+Hypothesis · V-27.121-160 新增三功能。

---

## 🔴 阻断级

### V-27.1 [Race] watchdog force-clear 锁 + 旧 worker 仍存活 → 双 cascade 并发
**File**: `backend/tasks/session_watchdog.py:144-151` + `backend/tasks/mining_tasks.py:80-118`
**V-26.5 修复验证:半修 + 引入新回归。** force_clear 解决了 SIGKILL 死 worker 的锁残留,但无条件 DELETE。watchdog 死活判据是 `last_alpha_persisted_at < NOW()-15min` —— 卡在长 BRAIN simulate / 慢 LLM 的 worker 完全可能 15min 不写 heartbeat 却仍活在 `while True` 内。force_clear 删掉它的锁后新 worker 成功 acquire → **两个 worker 同跑同一 cascade**,正是 V-26.1 锁机制要防的根因。旧 worker 后续 `finally:_release_lock` 因 Lua-CAS token 不匹配是 no-op,无法纠正。

### V-27.2 [Bug] 离散任务 revive 路径完全无重复派发保护
**File**: `backend/tasks/session_watchdog.py:95-127`
V-26.1/4/5/7/27 的 Redis cascade 锁**只保护 `CONTINUOUS_CASCADE`**。离散任务(`AUTONOMOUS_TIER1/2/3`/`SPECIFIC`/`AUTO`)revive 走同一个 `run_mining_task.delay()`,discrete 分支从不 acquire 任何锁。watchdog 每 5min 一跑:离散任务 dead 且首次 revive worker 15min 内没写 trace_step → 第二个 tick **再派发一次** → 两个离散 worker 跑同一 task。V-26.1 在 cascade 侧的 bug 原样搬到离散侧且无任何缓解。

### V-27.3 [Bug] `sync_datasets`(每日 06:00 beat)只 INSERT 不 UPDATE,且不写 `universe`
**File**: `backend/tasks/sync_tasks.py:249-275`
beat 版 `sync_datasets` 对已存在行完全跳过,从不刷新 `field_count`/`value_score`/`pyramid_multiplier`/`coverage`;新建行**根本不设 `universe`**(对比 manual 版 `:339` 有 `universe=universe`)。而 `_get_datasets_to_mine`/`_get_dataset_fields` 都按 `universe == task.universe` 过滤 → 每日 beat 同步进来的 dataset 因 `universe=NULL` **对挖矿完全不可见**,beat 同步等于白跑。

### V-27.31 [Bug] `node_hypothesis` 的 LLM 调用没有 try/except — V-26.49 修复半做
**File**: `backend/agents/graph/nodes/generation.py:350-355`
V-26.49 给 `node_distill_context`/`node_code_gen` 都加了 `try/except → _failed_llm_response()` 兜底,但 `node_hypothesis` 的 `llm_service.call` 是裸调用。LLM 超时 / 网络抖动 / JSON 模式异常时整个节点抛出 → LangGraph 节点崩溃 → 整条 workflow.run() 失败。三个生成节点里最关键的 hypothesis 节点没被覆盖。**V-26.49 修复验证:不完整。**

### V-27.32 [Race] `node_self_correct` 内嵌 `_record_correction` 在 fix 重验证前写 Redis KB — V-26.18 根因未消除
**File**: `backend/agents/graph/nodes/validation.py:455-467`
correction 在 `is_valid` 还是 `None`(未重验证)时就写进 Redis KB。下一轮 `node_validate` 可能把这个 "fix" 判为 invalid,但 KB 里已记为 "successful correction"。V-26.17 把 KB 从进程内 list 换成 Redis 共享后,**这条噪声现在跨 worker、跨重启永久污染** `_find_similar_errors` 检索池。**V-26.18 修复验证:未修,且 V-26.17 放大了影响面。**

### V-27.33 [Bug] `state.fields[:50]` 截断喂给 SELF_CORRECT — V-26.60 未修
**File**: `backend/agents/graph/nodes/validation.py:383`
`node_self_correct` 构造 `allowed_fields` 时 `for f in state.fields[:50]`。原始表达式合法引用第 51+ 字段时,SELF_CORRECT prompt 看不到它,LLM 会把它当 "unknown field" 误删/替换 —— 把一个只是 syntax 小错的表达式 "修" 成语义错误。**V-26.60 修复验证:未修。**

### V-27.34 [Bug] `node_validate` 把 `is_valid=None` 当 invalid,与 V-26.58 三态语义冲突现网放大
**File**: `backend/agents/graph/nodes/validation.py:97-99` + `edges.py:26`
`route_after_validate` 用 `any(not a.is_valid ...)` 判定,`not None == True`。retry 耗尽后 router 走 `simulate` 分支,此时 pending_alphas 里仍有 SELF_CORRECT 改完没被重 validate 的 `is_valid=None` alpha,下游对其 truthiness 检查行为不一致。V-26.58 已 backlog 化,但 backlog 期间这是活 bug。**V-26.58 修复验证:确认 backlog 化未修;现网风险仍在。**

### V-27.61 [Half-done] V-26.75 retryable 协议在本子系统 caller 端完全没接
**File**: `backend/agents/graph/nodes/evaluation.py:683-711`
commit f4da587 在 brain_adapter 把 429 改成返回 `retryable:True` + `retry_after_sec`,commit message 明说 caller 可 re-enqueue。但 `node_simulate` 整个文件 grep 不到 `retryable` —— 结果处理只读 `success`/`alpha_id`/`error`。429 的 alpha 仍被 `simulation_success=False` 永久写死成 FAIL。**V-26.75 修复验证:半修 —— 适配器侧改了,LangGraph caller 侧没接,现网行为与修复前一致。**

### V-27.62 [Bug] V-26.79 只修了 PASS 路径,V-26.20 新增的 PROV 路径仍直接 mutate 共享 metrics
**File**: `backend/agents/graph/nodes/evaluation.py:1197-1198, 1218-1219`
V-26.79(e53b742)把 PASS 路径改成 `alpha.metrics = dict(alpha.metrics)` 再写 `_v16_suspicion_flags`。但 e53b742 晚于 V-26.20(47d0030),47d0030 新增的 `near_pass` 分支里 `alpha.metrics[...] = ...` 仍是原地 mutate。同文件两条几乎相同的路径,一条修了一条没修。**V-26.79 修复验证:半修 —— PROV 路径漏修,写穿 state、污染 replay 在 near_pass alpha 上依旧发生。**

### V-27.63 [Bug] `node_evaluate` 通篇在 `state.pending_alphas` 的共享对象上原地改写 quality_status
**File**: `backend/agents/graph/nodes/evaluation.py:801, 854-859, 1182, 1220, 1226, 1367`
`updated_alphas = state.pending_alphas.copy()` 是浅拷贝,`alpha` 与 `state.pending_alphas[i]` 同对象;`alpha.quality_status = "FAIL"/"PASS"` 全部写穿回输入 state。V-26.63 当初只点了 `node_simulate`(已用 `model_copy()` 规避),`node_evaluate` 这条更大的同类问题被漏掉且**反而没拷贝**。LangGraph replay / 中断恢复时输入态已被破坏性改写。

### V-27.91 [Bug] `_safe_api_call` 的 401 分支完全没吃到 V-26.24 修复
**File**: `backend/adapters/brain_adapter.py:1186-1189`
V-26.24 只给 `_request` 加了 `_invalidate_session_cache()`,`_safe_api_call` 的 401 处理既不清 Redis cache、不走 `_auth_lock`、也不识别 V-22.7 body marker。`submit_alpha`/`get_alpha`/`get_datasets`/`get_datafields`/`get_user_alphas` 全走 `_safe_api_call`。**V-26.24 修复验证:半修 —— poison cache 自愈只覆盖 simulate/poll 路径,所有 data-fetch + submit 路径仍能从 Redis 复活死 session,并发 re-auth 仍会雷击 BRAIN auth 端点。**

### V-27.92 [Half-done] V-26.13/26 修了计数但 Hypothesis 状态机仍不靠它
**File**: `backend/agents/graph/nodes/persistence.py:786, 874-875` vs `backend/services/hypothesis_service.py:352-408`
`refresh_stats` 现在正确合并 `alpha_failures` 计数(V-26.13 真修),workflow 也补了 FAIL 路径 `touched_hids`(V-26.26 真修)。**但**真正驱动 `mark_active` 的 `_process_hypothesis_feedback` 用内存 `alpha_count = len(pending_alphas)`,`should_abandon_hypothesis` 只读内存 `history_out` —— 与 `refresh_stats` 是两条独立链路。**V-26.13/26 修复验证:治标 —— 修的是前端展示的 denormalized 列,V-26 文档点名的"卡 PROPOSED 真根因"是状态机转换,状态机至今不读 `refresh_stats` 结果;worker 重启后内存态丢失,FAIL alpha 仍无法推动状态机。**

### V-27.93 [Bug] V-26.11 record_* 提交夹带未修,evaluation/persistence 主路径仍现网生效
**File**: `backend/agents/services/rag_service.py:1154, 1282` ← `backend/agents/graph/nodes/evaluation.py:1607, 1653`
`record_failure_pattern`/`record_success_pattern`/`update_pattern_brain_status` 仍用 `self.db` 并在方法体内 `await self.db.commit()`,与节点共享同一 session。**V-26.11 修复验证:partial 如实(7bdfc7d 自标 partial + 写了 backlog),但"caller 未提交事务被夹带 commit → alpha rollback 后 KB 已落盘引用不存在 alpha_id"这个原始阻断级现象在 evaluation/persistence 主路径仍 100% 存在,只隔离了最低风险的 `_track_retrieval_hit`。打折修复挑了最不痛的一项。**

### V-27.94 [Race] V-26.25 re-probe latch 是 class 级、无锁,多 worker 24h 一到惊群 403
**File**: `backend/adapters/brain_adapter.py:631-647, 669-671`
`_no_multisim`/`_no_multisim_at` 是 class 属性,跨进程不共享。re-probe 窗口到期后 `if since_latch < REPROBE` 检查与 `_no_multisim_at = time.time()` 重置之间无锁:N 个 worker + V-20.1 并发 round 会同时穿过 latch、同时 POST list payload、同时收 403。**V-26.25 修复验证:引入新 race —— 修掉了"worker 重启才能解 latch",但 24h 一到就是一次跨进程 403 惊群,每进程独立计时实际 re-probe 频率是 N×。**

### V-27.95 [Bug] `_track_retrieval_hit` 刷 `updated_at` 与 `get_recent_pass_examples` 的 7 天窗口形成自锁死循环
**File**: `backend/agents/services/rag_service.py:294-328` + `backend/repositories/knowledge_repository.py:391-397` vs `rag_service.py:759`
`bulk_increment_usage` 显式 `updated_at=func.now()`(V-24.D),而 `get_recent_pass_examples` 硬过滤 `updated_at >= cutoff`(7 天)。每次 retrieve 命中一个 entry 就把 `updated_at` 刷到当下 → 它永远留在窗口里 → 下次必再被 candidate → 再命中。**V-26 漏掉的新问题:L1 anti-collapse 想根治的"搜索邻域锁死"换马甲复活,冷门 entry 被这批永鲜 entry 挤出窗口。**

### V-27.121 [Bug] submit 轮询把「计算中/已拒绝」误判为提交成功
**File**: `backend/adapters/brain_adapter.py:1356-1369`
`submit_alpha` 轮询只在响应有 Retry-After 头时继续。若首个 POST 直接返回 200 无 Retry-After(或 GET 轮询某次返回 200 无头但任务还在跑),循环立即 `break`,`success = resp.status_code == 200` 判成功并 stamp `date_submitted`。**新代码 Bug:把"进行中"当"已接受",一个实际未提交的 alpha 被永久标记 submitted 且不可重试。**

### V-27.122 [Bug] submit 轮询超时(max_polls 耗尽)被当作成功
**File**: `backend/adapters/brain_adapter.py:1352-1369`
`while polls < max_polls` 正常退出和"Retry-After 消失"退出走**同一条路径** —— 都落到 `success = resp.status_code == 200`。轮询超时时 `resp` 是最后一次 GET 响应,若那次返回 200(进行中)则超时被判成功。没有 `polls == max_polls` 分支区分"轮询耗尽"与"正常终结"。

### V-27.123 [Race] submit 四道 gate 无并发保护,可重复提交同一 alpha
**File**: `backend/services/alpha_service.py:375-433`
`submit_alpha` 先 `get_by_id` 读 `date_submitted`,gate 通过才 POST,成功才写 `date_submitted` + commit。两个并发请求(双击 Popconfirm、前端+脚本同时)都会看到 `date_submitted IS NULL` 双双 POST。`date_submitted` 无唯一约束、读取无 `FOR UPDATE`、无 advisory lock → 同一 alpha 提交两次,烧两个 BRAIN slot。

### V-27.124 [Bug] crisis-window 相关性切片用错对齐方式,corrwith 在不重叠索引上几乎全 NaN
**File**: `backend/services/correlation_service.py:462`
`os_w.corrwith(target_w)`:`os_w` 保留全部窗口日期(含 NaN 行),`target_w` 是 `.dropna()` 后索引被抽稀的 Series。`corrwith` 按索引对齐,target 缺失日期让每列重叠观测数骤降。per-window max corr 系统性偏低,**"危机窗口收敛"告警基本测不出来 —— 这块功能的核心目的失效。**

### V-27.125 [Bug] decay 快照在循环中途崩溃时随整循环回滚,但成功计数已 +1
**File**: `backend/tasks/sync_tasks.py:155-156, 221`
`maybe_append_decay_snapshot` 把 `alpha.decay_curve` 重新赋值到 session 对象,循环结束后才 `await db.commit()`。循环中任一 alpha 抛异常、或进程在 commit 前被 kill(Windows `--pool=solo` 下 Celery 重启常见),**所有** alpha 的 decay 快照连同 metrics 刷新一起丢失,但 `decay_snapshots_added` 计数已反映"添加成功"。计数与持久化不一致,6 天 dedup gate 下要再等一周才补。

---

## 🟡 中等

### V-27.4 [Half-done] cascade `progress_current` 写了但仍无判停 — V-26.3 只修了一半
**File**: `backend/tasks/mining_tasks.py:846-857` vs `1035-1172`
`_stamp_heartbeat` 现在用 SQL update-with-expression 累加 `progress_current`,前端进度条解决了。但 `_run_continuous_cascade` 的 `while True` 主循环**从头到尾没有 `progress_current >= daily_goal` 检查**(对比 discrete 路径 `:198`)。**V-26.3 修复验证:半修 —— cascade 能写进度但写完没人读,daily_goal 对 cascade 仍是死字段。**

### V-27.5 [Bug] cascade 正常退出把 EARLY_STOPPED 误标成 COMPLETED
**File**: `backend/tasks/mining_tasks.py:1179-1185`
`while True` 在 `task.status in ("PAUSED","STOPPED","EARLY_STOPPED")` 三态都 break,但收尾只判 `if task.status in ("PAUSED","STOPPED"): run.status = task.status` —— **EARLY_STOPPED 落入 `else` 被标成 `run.status="COMPLETED"`**。run 历史失真:early-stop 显示为正常完工。

### V-27.6 [Bug] 配额守卫把 pre-simulate 拒绝也计入 BRAIN sim 数 — V-26.31 过度修
**File**: `backend/tasks/session_watchdog.py:258-270`
修复加了 `AlphaFailure` 计数,但 `select(func.count(AlphaFailure.id))` **无任何 failure_type/stage 过滤**,把语义校验失败、hallucinated-field 拒绝等**从未触达 BRAIN** 的 pre-sim 拒绝行也计进去了。**V-26.31 修复验证:治标 + 引入反向偏差 —— 原来低估,现在系统性高估,cascade 会话被提前误暂停;根因(无"真打过 BRAIN"标记)未消除。**

### V-27.7 [Tech-debt] quota_guard 日志占位符 `{{?}}` 永不填充
**File**: `backend/tasks/session_watchdog.py:298-303`
`f"... PAUSING {{?}} sessions ..."` —— f-string 里 `{{?}}` 转义后渲染成字面量 `{?}`。审计时看不到实际暂停了几个 session。

### V-27.8 [Half-done] V-26.32「PAUSE 不取消 in-flight sim」只补了日志没补行为
**File**: `backend/tasks/session_watchdog.py:284-303, 312-331`
commit 注释和代码注释都承认"in-flight sim 不能 server 端取消",然后**只 log 了 `brain:concurrent_sims` 计数**就算完。pipeline 两深的在途 round 会继续烧配额。**V-26.32 修复验证:未修,仅加观测;实际降级为"观测项"而非修复。**

### V-27.9 [Bug] `iqc_audit_backfill_sweep` claim 锁后若 `apply_async` 抛错则锁泄漏
**File**: `backend/tasks/refresh_tasks.py:506-518`
sweep `claim_iqc_audit_lock(pk)` 成功后 `apply_async` 包在 try/except,except 分支**只 log 不释放锁**。`release_iqc_audit_lock` 只在 `_audit_iqc_marginal_async` 的 finally 里调,而那个 task 因 enqueue 失败根本没跑。celery broker 抖动时一批 alpha 锁被占 10min。**V-26.84 修复验证:有缺口。**

### V-27.10 [Race] `_prefetch_round_isolated` 的隔离 BrainAdapter 不走 V-26.2 的 force_refresh
**File**: `backend/tasks/mining_tasks.py:738-757` vs `1056-1059`
V-26.2 在 cascade **前台** `brain` 上 phase 边界调 `ensure_session(force_refresh=True)`,但 prefetch round 走 `_prefetch_round_isolated` **自建 `BrainAdapter()`**,只惰性走默认 `ensure_session()`(Redis 缓存短路)。**V-26.2 修复验证:覆盖不全 —— prefetch 路径漏修,"Redis 缓存 token 还剩 30min 就过期"在 prefetch 路径依旧成立。**

### V-27.11 [Bug] watchdog 多次 revive 后 `watchdog_revive` 配置键被逐次覆盖
**File**: `backend/tasks/session_watchdog.py:164-186`
`prior_run` 取 `id.desc()` limit 1 —— 第二次以后 revive 时 `prior_run` 是上次 revive 的 run,新 run `inherited_config["watchdog_revive"] = {...}` 直接覆盖,丢上一次 revive payload。**V-26.33 修复验证:部分有效 —— config_snapshot 继承基本对,但 revive 链多跳不可追溯。**

### V-27.12 [Race] `_redispatch_task` 两次 commit 之间 `.delay()` 已发但 `celery_task_id` 可能丢
**File**: `backend/tasks/session_watchdog.py:195-203`
`commit()` → `.delay()` → `run.celery_task_id = id; commit()`。第二个 commit 失败时 celery 任务已在跑但 `celery_task_id` 留 NULL,后续靠它关联 worker / 排障会找不到。`.delay()` 副作用无法 rollback。

### V-27.13 [Performance] `refresh_os_correlation_cache` 单 task 内 5 region × 3 leg 串行,易撞 `task_time_limit=3600`
**File**: `backend/tasks/sync_tasks.py:39-84` + `backend/celery_app.py:28`
一个 celery task 内串行跑 5 region PnL 刷新 + 5 region metrics 刷新(逐个打 BRAIN `get_alpha`)+ 5 region crisis stress test。metrics leg 单独可能几十分钟,1h SIGKILL 后该 region 之后全不刷新且无断点续跑。与 V-26.6 同根,但不在 V-26 覆盖内。

### V-27.14 [Bug] `sync_user_alphas` 增量同步起点用 `date_created`,漏掉「旧创建、近期才 submit」的 alpha
**File**: `backend/tasks/sync_tasks.py:553-569, 604-605`
增量锚在 `max(Alpha.date_created) - 3天`,按创建时间拉。但 V-23.E 核心需求是检测**提交翻转**(`date_submitted` NULL→有值)。30 天前创建、昨天才提交的 alpha BRAIN 不返回 → `submission_flip_regions` 漏检 → stale 标记不触发,IQC marginal Δscore 用过期组合状态。增量窗口维度(created)与要捕捉的事件维度(submitted)不一致。

### V-27.15 [Tech-debt] `IQC_AUDIT_BACKFILL_LIMIT` 模块级常量在 import 时快照 settings
**File**: `backend/tasks/refresh_tasks.py:426-427`
SQL 处确实直接读 `_iqc_settings.IQC_AUDIT_BACKFILL_LIMIT`(live),但模块级 `IQC_AUDIT_BACKFILL_LIMIT = ...`(行 427)是 import 时的值拷贝,任何 `from ... import IQC_AUDIT_BACKFILL_LIMIT` 的旧 caller 拿到冻结值。**V-26.83 修复验证:不完整 —— 别名仍冻结。**

### V-27.16 [Tech-debt] cascade discrete 路径 `_run_one_round_inline` 参数 `brain` 收了不用,死参数
**File**: `backend/tasks/mining_tasks.py:669-694`
形参列出 `brain` 但函数体内从不引用,实际 sim 走 `mining_agent`(内部持自己的 brain)。误导:读代码会以为 serial 路径和 pipeline 共享 brain session。

### V-27.17 [Performance] `_count_pass_global_region` 每 phase 边界全表 COUNT,无 task 维度索引保证
**File**: `backend/tasks/mining_tasks.py:611-622`
T2/T3 phase 入口每次 `SELECT count(*) FROM alphas WHERE region=? AND factor_tier=? AND quality_status='PASS'` —— 跨所有 task 全 region 扫描。cascade `while True` 每轮 T2+T3 各算一次,alphas 表单调增长,没看到复合索引证据。

### V-27.35 [Tech-debt] `_ERROR_KNOWLEDGE_BASE` in-memory fallback 与 Redis 双写产生不一致语义 — V-26.17 半修
**File**: `backend/agents/graph/nodes/validation.py:310-312, 326`
V-26.17 换 Redis 但保留 in-memory list 双写。`_load_correction_kb` 在 Redis 返回空但可达时 fallback 到 in-mem → 不同 worker 看到不同 KB,正是 V-26.17 声称要消除的"跨 worker 不共享"。双写 cap 还不一致(200 vs 100/50)。**V-26.17 修复验证:半修 —— 主路径修对,fallback 分支重新引入原始分歧。**

### V-27.36 [Bug] `trace_update` 里 `similar_errors_found` 统计读的是 in-mem list 而非实际用的 KB
**File**: `backend/agents/graph/nodes/validation.py:492`
实际检索用 `_load_correction_kb()`(Redis 优先),但 trace 输出 `sum(1 for _ in _ERROR_KNOWLEDGE_BASE)` 数的是 in-memory list。Redis 模式下该数字与真实检索池规模脱节,审计/监控被误导。

### V-27.38 [Race] `node_hypothesis` 自开 `AsyncSessionLocal` 四处,绕过注入的 `db` — V-26.23 未修
**File**: `backend/agents/graph/nodes/generation.py:437, 525, 590, 642` + `node_code_gen` `:782`
`node_hypothesis` 有四处 `async with AsyncSessionLocal()`,`node_code_gen` 一处,全部绕过注入的 `self.db`/`rag_service`。V-22.13 reuse 读到的 hypothesis 状态可能与主事务未提交的写不一致;连接池被额外占用。**V-26.23 修复验证:未修,且 V-22.13/G-refine 又新增 3 处同类反模式。**

### V-27.39 [Tech-debt] V-26.50 `expected_sharpe` 清洗只覆盖 `node_code_gen`,`node_hypothesis` 直读 LLM 输出
**File**: `backend/agents/graph/nodes/generation.py:372-374`
`node_hypothesis` 把 LLM 返回的 hypotheses dict 列表原样塞进 state,里面 LLM 自填的 `confidence`/`novelty`/`expected_signal` 同样是未校验的注入向量。**V-26.50 修复验证:`node_code_gen` 内修对,hypothesis 侧同类问题仍在。**

### V-27.40 [Bug] V-26.48 list-of-dict 校验只在 `node_code_gen`,`node_hypothesis` 的 `hypotheses` 不验证类型
**File**: `backend/agents/graph/nodes/generation.py:372, 381-391`
`node_hypothesis` `hypotheses = parsed.get("hypotheses", [])` 后直接 `for h in hypotheses: h.get(...)` 并 `h["selected_datasets"] = sel` 写回。LLM 把 `hypotheses` 返回成 dict/str/含非 dict 元素时 `AttributeError`,且无 try/except(见 V-27.31)。**V-26.48 修复验证:hypothesis 节点完全未防护。**

### V-27.41 [Tech-debt] V-26.61 prompts.yaml 迁移在模块 import 时求值,YAML 改动不热生效且加载失败 silent 吞
**File**: `backend/agents/prompts/validation.py:64-67`(经由 `validation.py:22` import)
`SELF_CORRECT_SYSTEM = _get_prompt_loader().get_system_prompt(...) or _FALLBACK` 是 import 时一次性求值:YAML 改了要重启 worker;YAML 里 key 拼写漂移/被删时静默 fallback 到 Python 常量无 warning。**V-26.61 修复验证:统一到 registry 达成,但引入 import-time 求值 + silent fallback 新风险。**

### V-27.42 [Bug] `node_validate` 的 `batch_dedup` 每次调用重建,self_correct 回环时丢失跨 pass 去重状态
**File**: `backend/agents/graph/nodes/validation.py:62-64`
`batch_dedup = ExpressionDeduplicator(...)` 在函数体内每次重建。`self_correct → validate` 回环时第 2 次 validate 的 dedup 池是空的 → 同一对重复表达式可能两个都判 valid 放过。去重在回环语义下不稳定。

### V-27.45 [Race] V-22.13 reuse 的 `get_by_id` 读与主 workflow 写之间存在 TOCTOU
**File**: `backend/agents/graph/nodes/generation.py:534-543`
用独立 session `_reuse_db` 读 `existing.status` 判 `in ("ACTIVE","PROPOSED")` 后直接复用。从这次读到 alpha 真正写库之间,B5/B6 feedback 可能已把该 hypothesis 改成 ABANDONED/SUPERSEDED → alpha 挂到已废弃 hypothesis。独立 session(V-27.38)放大了窗口。

### V-27.46 [Tech-debt] `node_hypothesis` 三个 hge_level>=2 分支串行 if,其中 G-refine 块已是死代码仍在 hot path 执行
**File**: `backend/agents/graph/nodes/generation.py:505, 585, 624`
三块共 ~140 行靠 `current_hypothesis_id is None` 互斥串联,圈复杂度极高。其中 G-refine 块(`:585-622`)的 `find_unused_refined` 在 V-26.14 已判死代码(0/673 rows 有 parent),仍每 round 执行一次无效 DB 查询。**V-26.14 修复验证:未修,死代码仍在 hot path 执行。**

### V-27.47 [Bug] `current_hypothesis_id` 作为 LangGraph scalar 字段,跨节点丢失靠 list[0] fallback 打补丁但未根治
**File**: `backend/agents/graph/state.py:174` + `generation.py:88-92, 512-520`
代码注释多处承认"LangGraph scalar-field propagation can drop"它,于是到处加 `current_hypothesis_ids[0]` fallback。这是把 state 传播 bug 用 N 处补丁绕过而非修根因,每加一个新读取点要记得抄一遍 fallback,漏抄就是 bug。

### V-27.64 [Bug] crisis-window 块只在 `source=="local"` 跑,`brain` 源 alpha 永远拿不到危机相关性
**File**: `backend/agents/graph/nodes/evaluation.py:975-999`
b4c5f0d 注释自辩"global max-corr 测不到时 per-window 数据更少",但 `get_with_fallback` 的 `"brain"` 源是**实测到了真值**并非测不到。`calc_self_corr_by_window` 内部用自己的 PnL cache。结果:凡走 BRAIN API 兜底的 alpha,`_crisis_correlations` 永远缺失。

### V-27.65 [Bug] V-26.71 兜底分支 `bucket_results` 可能为 None 时 `len()` 直接崩
**File**: `backend/agents/graph/nodes/evaluation.py:622-661`
retry 循环若 `brain.simulate_batch` attempt 0 **返回 None**(而非抛异常)就 `break`,`bucket_results` 留 None,随后 `if len(bucket_results) < ...` 抛 TypeError。**V-26.66/V-26.71 修复验证:基本到位,但留了 None 返回这个边界缝。**

### V-27.66 [Bug] V-26.89 atomic fields_used 修了,但后面那段 post-commit UPDATE 死代码仍每批全跑
**File**: `backend/agents/graph/nodes/persistence.py:355-388`
`fields_used` 已进 INSERT 的 `values_dict`,但后面 `:362-383` 对每个 inserted alpha **无条件再算一次 `_extract_used_fields` 并 UPDATE**,没有"已存在则跳过"判断。**V-26.89 修复验证:主修对了(atomic),但旧 UPDATE 没降级成真正的 backfill,变纯冗余写。**

### V-27.67 [Bug] `should_stop_early` 文档承诺的 `max_iter/2 floor` 在缺省路径退化成与 warmup 同一道
**File**: `backend/agents/graph/early_stop.py:51-59` + `backend/agents/graph/nodes/persistence.py:664-671`
`node_save_results` 拿不到 `max_iterations` 时缺省 10,`should_stop_early` 里 `WARMUP_ROUNDS`(=5)与 `n < max_iterations/2`(=5)边界完全重合 —— 两道 guard 退化成一道。cascade 的 `max_iterations` 可能 None/0,`if max_iterations and ...` 直接短路掉 floor。

### V-27.68 [Bug] `should_abandon_hypothesis` 的 V-26.15 根因未消除 —— 仍只看 `pass_count`,不看 `alpha_count`
**File**: `backend/agents/graph/early_stop.py:177-192`
判定窗口里仍只有 `pass_count` 和 `attribution`,`alpha_count` 字段虽在 entry 里有(persistence.py:838 写入),但 abandon 决策完全没读它。当前靠 `classify_attribution` 在 `alpha_count==0` 返回 `"unknown"` 的副作用兜底不误杀,不是显式逻辑。**V-26.15 修复验证:未见对应修复 commit,根因仍在。**

### V-27.69 [Tech-debt] V-26.16 abandon 日志「convert to SUPERSEDED via G-refine」仍是 aspirational
**File**: `backend/agents/graph/early_stop.py:201-205`
`[B6 abandon-trigger]` 日志仍写"downstream may convert to SUPERSEDED via G-refine loop",而 V-26.14 已判定整条 G-refine 链是死代码。**V-26.16 修复验证:未修 —— 日志措辞照旧,继续误导审计。**

### V-27.70 [Race] `_process_hypothesis_feedback` 用独立 session 跨 session 读刚写入的 hypothesis,与 B3 写入 session 未提交存在可见性缝
**File**: `backend/agents/graph/nodes/persistence.py:819-823, 871`
B5 v2 用 `async with AsyncSessionLocal() as _qdb` 新开 session 读 `Hypothesis.statement`,注释说"just persisted by B3"。但 B3 写在哪个 session、是否已 commit 没保证 → 独立 `_qdb` 可能读到旧值/None,LLM attribution 拿空 statement 降级成 heuristic。本子系统有 3 处同类(_qdb/_hdb/node_simulate 自开)。

### V-27.71 [Bug] `_process_hypothesis_feedback` 的 `alpha_count` 用 `len(pending_alphas)` 含 flip-retry 追加的 alpha
**File**: `backend/agents/graph/nodes/persistence.py:786` + `evaluation.py:1563`
flip-retry 把翻转 alpha `append` 进返回的 `pending_alphas`。`alpha_count = len(pending_alphas)` 把 flip 产物也算进 hypothesis round 计数,但 flip alpha 的 `hypothesis` 文本是"原文 + (sign-flipped)",语义上未必还属于该 hypothesis。hypothesis 的 alpha_count/pass_count 被 flip 产物虚高。

### V-27.72 [Race] V-26.90 rate-limit 用 `INCR` + 仅在 `current==1` 时 `EXPIRE`,worker 在两步间崩溃会留永不过期的 key
**File**: `backend/agents/graph/nodes/persistence.py:432-435`
`current = cli.incr(rate_key)`;`if current == 1: cli.expire(rate_key, 60)`。进程在 incr 返回 1 之后、expire 之前被 SIGKILL → key 无 TTL 永久驻留,`current` 永远 >6 → **之后所有 worker 的 can_submit refresh 入队被永久 rate-limit 掉**。**V-26.90 修复验证:方向对,但 INCR+EXPIRE 非原子,引入"永久限流"新失效模式。**

### V-27.73 [Bug] V-26.92 skip 了无 alpha_id 的 PASS alpha,但 V-22.1 KB 写入路径不 skip
**File**: `backend/agents/graph/nodes/persistence.py:204-211` vs `546-580`
`_incremental_save_alphas` 对 `not alpha.alpha_id` 的 PASS alpha `continue` 跳过 INSERT,但同一 `node_save_results` 里 `:546` 起的 V-22.1 `record_success_pattern` 循环只检查 `quality_status` 和 `expression`,**不检查 `alpha_id`** → sim 返回 None 的"PASS"alpha 进不了 alphas 表却进了 KB SUCCESS_PATTERN 池。**V-26.92 修复验证:只堵了 DB 侧,KB 侧同缺陷未堵。**

### V-27.74 [Bug] V-26.93 用 `configurable.get("hypothesis_centric_level")` 判 level,但无人保证注入这个 key
**File**: `backend/agents/graph/nodes/persistence.py:559-560`
`active_level = configurable.get("hypothesis_centric_level") or 0`。调用方没注入时恒为 0,V-26.93 guard 永不触发,KB 又回到"untyped 与 hypothesis-tagged 混写"。同节点判 incremental 用 `state.factor_tier`、判 hypothesis 用 `state.current_hypothesis_ids`,唯独 level 走 `configurable`,三个来源不一致无断言。**V-26.93 修复验证:逻辑对,但实际是否生效未知。**

### V-27.75 [Tech-debt] V-26.22 `locals().get("self_corr_source", ...)` 反模式仍在
**File**: `backend/agents/graph/nodes/evaluation.py:1059`
原封不动。此后 self_corr 校验链被 V-26.77 follow-up #2/#5 反复改动,`self_corr_source` 现在在 5 个分支被赋值,`locals().get` 仍在赌所有分支都赋过值。**V-26.22 修复验证:未修,分支增多后风险比 V-26 时更高。**

### V-27.76 [Bug] crisis-window 块只对 local 源 alpha detach metrics,metrics「是否已 detach」取决于 self_corr 源 + quality 分支
**File**: `backend/agents/graph/nodes/evaluation.py:980-984`
crisis 块里 `if self_corr_source == "local"` 时执行 `alpha.metrics = dict(alpha.metrics)`,只有 local 源被 detach;brain/unknown 源在后面 V-16 PASS 路径才 detach,PROV 路径根本不 detach。同一函数内 metrics 是否共享引用状态不一致,后续读 `alpha.metrics` 的代码无法假设。

### V-27.77 [Tech-debt] V-12 `test_sharpe` 缺失编造 `sharpe*0.8` 的 V-26.19 根因只在 hard_gate 链修了,scoring 链仍在编
**File**: `backend/agents/graph/nodes/evaluation.py:879`
`_check_is_os_consistency` 改成两者皆空就 `return False`(这条修对)。**但 `sim_result` 构造里 `"sharpe": test_sharpe_val if ... else metrics.get("sharpe",0) * 0.8` 的编造仍在**,这个 `sim_result["test"]` 喂给 `calculate_alpha_score`/`should_optimize`/`get_failed_tests`。**V-26.19 修复验证:半修 —— IS/OS gate 修对,但 score/optimize 判定仍吃 `sharpe*0.8` 假数据。**

### V-27.78 [Bug] V-26.21 扩展了 downgrade 集合,但 score-only 旁路 + checks 缺失 fail-open 根因未动
**File**: `backend/agents/graph/nodes/evaluation.py:1117, 1172`
downgrade 条件 `elif brain_actionable_fails and not brain_can_submit`。当 `check_details` 为空(BRAIN 没返 checks)时 `meets_thresholds = brain_can_submit or (not brain_failed_checks)` → `not []` 为 True,downgrade 分支不进,`score >= score_pass_threshold` 直接判 PASS。**V-26.21 修复验证:防御性扩集合到位,但 BRAIN 不返 checks 时(V-26.24 点的 session 失效期)score-only PASS 旁路立即复活。**

### V-27.79 [Bug] flip-retry 生成的 alpha 不进 `failure_feedback_queue`,FAIL 的 flip alpha 不产生 KB 学习信号
**File**: `backend/agents/graph/nodes/evaluation.py:1559-1564`
主评估循环 FAIL alpha 走 `failure_feedback_queue.append(...)`,但 flip-retry 段 `else: new_alpha.quality_status = "FAIL"` 后直接 `append` 到 updated_alphas,**不进 feedback queue、不做 attribution**。一个翻转后仍 FAIL 的 alpha(正反方向都不行,强负面信号)对 KB 完全不可见。

### V-27.80 [Tech-debt] flip-retry 段重新实现了一遍 hard_gate,与主 hard_gate 已漂移
**File**: `backend/agents/graph/nodes/evaluation.py:1518-1530` vs `1077-1086`
flip-retry PASS 判定缺 `meets_thresholds or score >= threshold` 层,且没跑 `evaluate_with_brain_checks`/`brain_actionable_fails` 的 BRAIN-aware downgrade —— 翻转 alpha 可带着 BRAIN HIGH_TURNOVER FAIL 直接判 PASS。两份 gate 逻辑注定持续漂移。

### V-27.81 [Race] `node_simulate` 自开 `AsyncSessionLocal` 做 dedup,V-26.23/V-26.64 点的 race window 仍在
**File**: `backend/agents/graph/nodes/evaluation.py:402-405, 1421-1424`
`node_simulate` 里 `async with AsyncSessionLocal()` 跑 `filter_unsimulated_expressions`,flip-retry dedup 又开一次。SELECT-then-simulate 之间 race window 原样保留。**V-26.23/V-26.64 修复验证:未修。**

### V-27.82 [Bug] V-26.91 `_resolve_metrics_snapshot_at` 用 `dateModified` 当 sim 完成时间,语义错配
**File**: `backend/agents/graph/nodes/persistence.py:62, 217-220`
`dateModified` 是 BRAIN 对 alpha **资源**的最后修改时间戳 —— re-fetch、check、submit 都会刷新它,不专指"这条 metrics 何时算出"。`sim_completed_at` 才对,但注释说它只"set by some retry paths",主路径不写。**V-26.91 修复验证:方向对(不再全批一个 wall-clock),但首选字段语义错配,多数 alpha 落 `datetime.utcnow()` fallback。**

### V-27.96 [Bug] V-26.8 的 800 行 cap + `ORDER BY id DESC` 直接架空 V-26.12 hypothesis-family boost
**File**: `backend/agents/services/rag_service.py:435-443, 572-580` vs `502-514, 612-622`
V-26.8 把 SQL 截成 `id DESC LIMIT 800`,V-26.12 的 hypothesis-family boost 在 Python 端对这 800 行打分。某 hypothesis 家族 KB 行 id 较老时根本进不了候选窗口,family boost 永远加不到。**V-26.8/V-26.12 修复验证:互相打架 —— cap 是 id 序而非相关性序,FAILURE_PITFALL 现 ~1660 行,一半家族行不可见。**

### V-27.97 [Bug] `record_failure_pattern` 没有 V-26.93 的 hypothesis_id=None 守卫
**File**: `backend/agents/services/rag_service.py:1056-1160` vs `backend/agents/graph/nodes/persistence.py:551-566`
V-26.93 给 `record_success_pattern` 调用点加了"level≥2 且 hypothesis_id=None 则跳过"守卫,但 `record_failure_pattern` 没有对称守卫,FAILURE_PITFALL 池照样混入 `hypothesis_id=None` 行。**V-26.93 修复验证:不对称半修 —— success 池干净了,failure 池继续脏。**

### V-27.98 [Dead-code] V-26.41/42 把诊断往 `abandon_reason` 塞,但 G-refine 链仍是死代码(V-26.14 未修)
**File**: `backend/services/hypothesis_service.py:281-302`(`mark_superseded`)+ `435-492`(`find_unused_refined`)
`mark_superseded:291` 仍 `if child.parent_hypothesis_id != hypothesis_id: raise ValueError`,`find_unused_refined:473` 仍 JOIN `parent.status==SUPERSEDED`。**V-26.14 修复验证:未修 —— G-refine 整条链仍死,V-26.41/42 等于给永远走不到的状态机精修日志格式。**

### V-27.99 [Bug] `mark_abandoned`/`set_active_flag` 的 1000 字符 cap 从头部保留,append-only 反而先丢最新诊断
**File**: `backend/services/hypothesis_service.py:258, 333`
`(prefix + " | " + reason)[:1000]` —— 滚动日志超 1000 字符时 `[:1000]` 砍掉的是**尾部最新**那条 reason(切到一半)。**V-26.41/42 修复验证:边界 bug —— append-only 语义在 cap 边界处破裂。**

### V-27.100 [Race] V-26.45 的 `untouched_first` 排序键基于 stale 的 `alpha_count` 列
**File**: `backend/services/hypothesis_service.py:174-188`
`untouched_first = case((Hypothesis.alpha_count == 0, 0), else_=1)` 读 denormalized `alpha_count`,该列只在 `refresh_stats` 后更新,而状态机不靠 refresh_stats(V-27.92)。已跑 FAIL alpha 但 refresh_stats 没被调到的 hypothesis `alpha_count` 仍 0,被永久排在"untouched"桶反复采样跑更多 FAIL。**V-26.45 修复验证:依赖未修的前置。**

### V-27.101 [Bug] `_is_session_valid` 任何异常都返回 False → 网络抖动期间无脑 re-auth 风暴
**File**: `backend/adapters/brain_adapter.py:471-492`
`except Exception: return False` —— BRAIN `/authentication` GET 超时/5xx/连接重置时 session 其实可能还有效,但被判 invalid 触发 `authenticate()`。无法区分"session 真失效"与"探测请求本身失败",放大 auth 端点压力。**V-26 漏掉的新问题。**

### V-27.102 [Bug] `submit_alpha` 轮询把非 200 终态当失败,与 `simulate_alpha` 的 [200,201,202] 不一致
**File**: `backend/adapters/brain_adapter.py:1372-1377`
`"success": resp.status_code == 200` —— BRAIN submit 的异步 job 完成后返回 201/202(async accept)会被判 `success=False`。提交不可逆,这个判定错误会让上层误以为提交失败而重试或误报。

### V-27.103 [Bug] `_acquire_sim_slot` INCR/DECR 占位在并发下 counter 瞬时虚高把所有人挡回
**File**: `backend/adapters/brain_adapter.py:150-166`
`count = await r.incr(...)` → 超 cap → `await r.decr(...)`。N 个 coroutine 同时 incr,counter 瞬时冲到 `3+N`,所有人都 `> 3` 全部 decr 回退、全部 sleep 1.5s,即使此刻只有 1 个 slot 实际占用。经典 thundering-herd,高并发时吞吐打到地板。

### V-27.104 [Bug] V-26.9 running-average 用 `success_count` 做样本数 n,取值时机脆弱
**File**: `backend/agents/services/rag_service.py:1193, 1200-1214`
`:1193` 先 `success_count = success_count + 1`,`:1200` `n = existing.meta_data.get('success_count', 1)` 读到的是已 +1 后的值。**V-26.9 修复验证:基本正确但脆弱 —— 依赖 `success_count` 与 avg 字段写入严格同步,无防御。**

### V-27.105 [Tech-debt] V-26.35 fail-open 计数器 `_valid_ops_load_failures` 写了但无人读
**File**: `backend/agents/services/rag_service.py:206, 230, 233`
V-26.35 注释说"surface as counter so dashboards can pick it up",但 `_valid_ops_load_failures` 是纯实例属性,无 metrics 导出/无 endpoint/无测试断言,且实例 per-request 计数器跟着实例死。**V-26.35 修复验证:counter 半实现,"dashboards can pick it up"是 aspirational。**

### V-27.106 [Bug] `_filter_hallucinated` fail-open(valid_ops 为空返回全部)叠加 800 cap,DB 抖动时把幻觉算子直接喂 LLM
**File**: `backend/agents/services/rag_service.py:262-264`
`if not valid_ops: return entries` —— V-26.35 让 `_get_valid_ops` 失败返回 `set()`,`_filter_hallucinated` 就 fail-open 放行全部 800 行。daily sweep 之间新写入的 hallucinated 行在 DB 抖动窗口内整批进 LLM prompt。**V-26.35 把"永久 fail-open"改成"瞬时 fail-open"是进步,但 fail-open 方向在"喂 LLM 幻觉算子"后果下值得商榷。**

### V-27.107 [Race] `get_recent_pass_examples` 内多次 `_track_retrieval_hit`,同一 entry 在嵌套调用里被多次 +usage_count
**File**: `backend/agents/services/rag_service.py:540, 649, 893`
`_get_success_patterns_enhanced`/`_get_failure_pitfalls_enhanced`/`get_recent_pass_examples` 各自末尾 `_track_retrieval_hit`。caller 既调 `query()` 又调 `get_recent_pass_examples` 时同一轮 retrieve 把重叠 entry 的 `usage_count` 多加。`usage_count` 既是 LRU 惩罚输入又是 hit 指标,多重计数扭曲 anti-collapse 惩罚阈值。

### V-27.108 [Tech-debt] `_get_failure_pitfalls_enhanced` 的 severity/category 权重仍硬编码,V-26.36 只 config 化了 success 侧
**File**: `backend/agents/services/rag_service.py:590, 604, 609-610`
`severity_weights = {'high':30,'medium':20,'low':10}`、category +20.0、error_type +15.0 全是字面量。**V-26.36 修复验证:半修 —— success 侧 config 化、failure 侧没动,调一边参数另一边还得改代码。**

### V-27.109 [Bug] `record_failure_pattern` 的 existing 分支 `avg_sharpe` 直接覆写,没做 V-26.9 同款 running-average
**File**: `backend/agents/services/rag_service.py:1090-1091`
`existing.meta_data['avg_sharpe'] = metrics.get('sharpe', 0)` —— 重复命中同一 failure skeleton 时 `avg_sharpe` 被最新一次直接覆盖。**V-26.9 修复验证:不对称 —— V-26.9 修了 success 侧,failure 侧的镜像缺陷原样保留。**

### V-27.126 [Bug] `get_with_fallback` 的 BRAIN tier 把「corr 仍在算」当「无法判定」
**File**: `backend/services/correlation_service.py:396-397`
BRAIN 返回 `{"min":..., "max":null}`(corr 仍在算)会落到 `return None, "unknown"`,无法区分"BRAIN 说算不出"和"BRAIN 还没算完",而后者本应让 caller 稍后重试。

### V-27.127 [Bug] submit precheck 与 can_submit gate 用的 self_corr 来源可能不一致
**File**: `backend/services/alpha_service.py:398-414` vs `backend/can_submit.py:120-131`
第四道 gate 现场实时测;第三道 gate 读 `alpha.can_submit` 列(由 `refresh_can_submit` 用可能几天前的陈旧 `metrics["_self_corr"]` 算)。两个 gate 用两份不同时点的 self_corr。若 can_submit 列基于旧高 corr 已 demote 但实时变低,alpha 永远卡在 can_submit=False,实时 precheck 根本没机会跑。gate 顺序与数据源不自洽。

### V-27.128 [Bug] `_fetch_pnl_series` 重试对真限流远远不够
**File**: `backend/services/correlation_service.py:232-245`
`get_alpha_pnl` 内部已 `except: return {}`(adapter 吞 429/超时),重试看到的还是 `{}`,3 次 1.5+3=4.5s 退避对真限流端点不够。commit message 声称"burst 下空响应重试救回",实际只能救"BRAIN 偶发返回空 records 但 HTTP 200",救不了真限流。

### V-27.129 [Bug] `_fetch_pnl_series`「三次都空」静默返回空 vs「三次都异常」抛出,行为不可预测
**File**: `backend/services/correlation_service.py:244-248`
最后一次拿到空 series 不 sleep 不 raise(`last_exc` None),`return pd.Series(empty)`;三次都抛异常则 `raise`。同样"测不出"走两条路径,caller `calc_self_corr` 对前者返回 `(None,"empty")`、对后者 try 捕获后落 BRAIN tier。

### V-27.130 [Race] crisis snapshot 读写无锁,06:30 beat 与 refresh=1 请求并发可损坏 JSON
**File**: `backend/services/correlation_service.py:645-665` + `backend/routers/correlation.py:90-98`
`save_crisis_snapshot` 直接 `path.open("w")` 非原子写。daily beat 与用户点击 `GET /crisis-summary?refresh=1` 可同时写同一 `crisis_corr_{region}.json`,`load_crisis_snapshot` 同时在读。写到一半被读 → `json.load` 抛异常被吞返回 None → UI 显示空。无 tmp-then-rename。

### V-27.131 [Race] OS PnL pickle 缓存同样非原子写,refresh 脚本与 beat 并发可损坏
**File**: `backend/services/correlation_service.py:170-181`
`_save_cache` 直接 `path.open("wb")` + `pickle.dump`。新脚本 `refresh_os_corr_cache.py` 和 06:30 beat 都写 `os_pnls_{region}.pkl`。半个 pickle 被 `_load_cache` 读 → 抛异常被吞返回 None → 所有 self_corr 降级到 BRAIN tier 或 unknown。缓存是整个 submit gate 的数据基础。

### V-27.132 [Bug] portfolio skeleton 数字提取把 ts-window 和小数、常数混在一起按位置比较
**File**: `backend/agents/seed_pool/portfolio_skeletons.py`(`_NUM_RE` / `_numerics_match`)
`_NUM_RE` 按出现顺序抽所有数字成 tuple,`_numerics_match` 要求同长度且逐位置 ±20%。`ts_rank(...,20)` 里的 window `20` 与另一表达式里位置相同但语义完全不同的常数(如 lag 的 `1`)会被做 ±20% 比较。位置对齐而非语义对齐 → 跨语义误配,两个不相关 alpha 被判 near-duplicate 直接 skip simulate,**错杀真信号,正是这个 commit 想修复的反面。**

### V-27.133 [Bug] 负数提取破坏数字配对:`a-1` 与 `subtract(a,1)` 抽出符号相反的 numerics
**File**: `backend/agents/seed_pool/portfolio_skeletons.py`(`_NUM_RE`)
`-?\d+` 在 `a-1` 里会匹配 `-1`,而 `subtract(a, 1)` 抽出 `1`。两个等价写法 numerics 一个 `(1.0,)` 一个 `(-1.0,)`,长度同符号反,`_numerics_match` 判不等 → 该 skip 的没 skip。数字提取对表达式书写形式敏感。

### V-27.134 [Bug] decay 快照 anchor 缺失时 `days_since_submit=None` 仍然写入
**File**: `backend/services/decay_service.py:68-94`
`anchor = alpha.date_submitted or alpha.created_at`,两者都 None 时 `days_since_submit` 写成 None,`build_decay_snapshot` 不因此返回 None。OS alpha 理论上一定有 created_at,真出现 None 说明数据已损坏,这里静默写入而非告警,把数据问题往后推。

### V-27.135 [Bug] decay 6 天 dedup gate 用数组末位,乱序或回填会永久卡死
**File**: `backend/services/decay_service.py:104-110`
`should_append_snapshot` 取 `decay_curve[-1]` 作为"最后一次快照"。若某次写入了 `snapshot_date` 在未来的条目(时区错误/回填脚本插入),`(today - last_date).days` 是负数永远 `< 6` → 该 alpha decay_curve **永久停止增长**。盲信末位 + 末位日期单调,没有"取 max date"。

### V-27.136 [Bug] `crisis_stress_test` 把所有 window 全空的结果也包成 `status:"ok"` 存盘
**File**: `backend/services/correlation_service.py:626-639`
顶层只在"cache 完全空"时返回 `status:empty`。cache 非空但每个 window 都返回 `missing_window`/`empty` 时顶层仍 `return {"status":"ok",...}`,router `if payload.get("status")=="ok": save_crisis_snapshot` 把全空快照存盘覆盖掉可能还有效的旧快照。

### V-27.137 [Bug] `compute_portfolio_matrix` 的 `n_obs` 在 window 模式下含 NaN 行,误导 UI 样本量
**File**: `backend/services/correlation_service.py:519-554`
window 模式 `returns = _slice_returns_to_window(...)` 后直接 `n_obs = returns.shape[0]`,切片只按日期范围过滤**没有 dropna**。`n_obs` 报的是日期跨度行数不是有效观测数,UI"样本量"一栏显著高估,reviewer 误判数据充足。

### V-27.138 [Bug] `calc_self_corr_by_window` 的 os_returns 首日 NaN 未处理,与 V-27.124 叠加双重侵蚀样本
**File**: `backend/services/correlation_service.py:438`
`os_returns_full = _pnls_to_returns_df(...)` 每列首日 NaN,且不同 alpha 首日不同,`os_w` 未 dropna 直接进 `corrwith`。结合 V-27.124,per-window corr 的有效样本被双重侵蚀。

### V-27.139 [Tech-debt] submit gate 用 `alpha.region or "USA"` 默认值,跨区误判 self_corr
**File**: `backend/services/alpha_service.py:401-402, 442`
`alpha.region` 列为 NULL 时 precheck 拿它去和 **USA** 的 OS PnL pool 比相关性,一个 CHN alpha region 丢失后会用 USA pool 测出随机低 corr 然后放行提交。应在 region 缺失时直接判 unknown。

### V-27.140 [Race] `refresh_can_submit` 读 metrics、改 metrics、commit 之间无隔离
**File**: `backend/services/alpha_service.py:319-340`
`new_metrics = dict(alpha.metrics or {})` 写回三个 `_brain_*` 键。批量/单个 `refresh-can-submit` 端点、或与 evaluation 节点写 `_self_corr` 并发跑在不同 session,后提交的整体覆盖 `metrics`,可能丢掉另一路刚写入的 `_crisis_correlations`/`_iqc_marginal`。read-modify-write 整个 JSONB 而非字段级更新。

### V-27.141 [Bug] BRAIN 不可达时本地强 self_corr 信号被丢弃,can_submit 返回 None 而非 False
**File**: `backend/can_submit.py:92-133`
`brain_alpha is None`(BRAIN 挂了)时 `:92` 直接 `return None,[],[]` —— 即使本地已测出 `local_self_corr=0.95`,这个明确的"应拒绝"信号被丢弃。本地强信号在 BRAIN 不可达时本应能独立 demote,现在被 BRAIN 可达性绑架。

### V-27.142 [Dead-code] `submit_alpha` 的 `skip_precheck` 参数无任何 caller 传入
**File**: `backend/services/alpha_service.py:352, 398`
router `submit_alpha_to_brain` 只传 `alpha_id`,批量脚本也没引用 —— 永远为 False 的死参数,留着会让人误以为有跳过 precheck 的途径。

### V-27.143 [Bug] portfolio 双因子匹配在 fields 集合双方都为空时仍可命中
**File**: `backend/agents/seed_pool/portfolio_skeletons.py:find_portfolio_match`
`_expr_fields_and_numerics` 解析失败时 `cand_fields` 可能是空 frozenset,`find_portfolio_match` 里 `fields == cand_fields` 两边都空就 True,再加 numerics 也匹配 → 命中、skip simulate。两个"fields 提取失败"的 alpha 被判彼此 near-duplicate。

### V-27.146 [Bug] `list_alphas` router 丢弃了 service 已计算的 `self_corr` 字段
**File**: `backend/routers/alphas.py:179-199`
`AlphaService._to_list_item` 已填充 `self_corr`/`self_corr_source`,`AlphaListItem` 模型也声明了,但 router 手工构造 `AlphaListItem(...)` 时**没传这俩字段** → `/alphas` 列表接口永远返回 `self_corr: null`。计算了、声明了、没接线。

### V-27.147 [Bug] submit 成功后 skeleton 缓存刷新失败被吞,mining 会继续生成已提交形状
**File**: `backend/services/alpha_service.py:438-444`
`refresh_portfolio_from_db` 抛异常时只 `logger.warning` 吞掉,`date_submitted` 已 commit。skeleton 缓存没更新 → mining loop 继续把刚提交的 shape 当候选,无重试/补偿机制,缓存可能长期陈旧。

### V-27.149 [Bug] `compute_portfolio_matrix` 的 `n_alphas` 包含整行 NaN 的 alpha
**File**: `backend/services/correlation_service.py:539-553`
`corr_df = returns.corr(min_periods=overlap_floor)` 对重叠 < floor 的 pair 产生 NaN(正确),但返回的 `n_alphas = corr_df.shape[0]` 是所有通过单列 dropna≥floor 筛选的列数。一个 alpha 可能单列有 30 个观测但和其他每个 alpha 重叠都 < floor,整行 NaN 仍计入 `n_alphas` → `_summarize` 的 `n` 虚高,median/mean 分母语义混乱。

---

## 🟢 改进

### V-27.18 [Tech-debt] `.cascade_phase_diag.log` 诊断脚手架仍整段留存,只是 env-gate
**File**: `backend/tasks/mining_tasks.py:786-802, 1016-1028`
**V-26.30 修复验证:有效但不彻底 —— 文件写出已 opt-in,但 `_phase_diag`/`_outer_diag` 闭包 + 十几处调用点整段留在热路径。** RCA 已结束,属该下架的临时脚手架。

### V-27.19 [Tech-debt] discrete 与 cascade 路径 `total_alphas` 统计语义与字段名 `alphas_mined` 有歧义
**File**: `backend/tasks/mining_tasks.py:282, 313`
两条路径 `alphas_mined` 都含 FAIL,与 `progress_current`(只数 success)语义不同,前端/审计按字面理解会高估成功数。

### V-27.20 [Tech-debt] `sync_datasets` 与 `sync_datasets_from_brain` 大段逻辑重复且行为分叉
**File**: `backend/tasks/sync_tasks.py:231-281` vs `284-368`
两个函数都做"拉 BRAIN datasets → upsert",但 beat 版只 insert、不设 universe、不触发 field sync。维护者改一处忘改另一处风险高(V-27.3 就是这个分叉的直接后果)。

### V-27.21 [Tech-debt] `_parse_to_beijing` 裸 `except:` 吞所有异常返回 None
**File**: `backend/tasks/sync_tasks.py:688-689`
无类型 `except:` 把 `KeyboardInterrupt`/`SystemExit` 也吞了,且解析失败静默返回 None,该行落库 `date_created=NULL` 无日志可查。

### V-27.22 [Tech-debt] `claim_iqc_audit_lock` Redis 故障时 fail-open,与 cascade 锁的 fail-closed 取向相反
**File**: `backend/tasks/redis_pool.py:197-207`(由 `refresh_tasks.py:507` 调用)
V-26.27 特意把 cascade 锁改成 fail-closed,但 `claim_iqc_audit_lock` 同类场景选 fail-open。同一子系统内两套相反的失败语义,是认知负担 + 未来踩坑点。

### V-27.37 [Dead-code] `state.py` 导入 `Annotated`/`operator.add` 但无任何字段使用 reducer
**File**: `backend/agents/graph/state.py:6, 9`
两个 import 是死代码;更危险的是:`trace_steps`/`generated_alphas`/`failures` 等累加字段全靠节点手动 `state.X + [...]` 全量替换,未来在 graph 引入并行分支时这些字段会因没有 reducer 而 **silent 互相覆盖**。

### V-27.43 [Tech-debt] `_select_exploration_fields` 死代码 + `random.sample` 无 seed
**File**: `backend/agents/graph/nodes/generation.py:700-715`
"Backward compatible helper" 全文件无调用点,且内部 `random.sample` 无 seed(与 V-26.53 同根)。

### V-27.44 [Bug] `node_code_gen` few-shot 抓取失败时 `merged_patterns` 静默退化,trace 无可观测信号
**File**: `backend/agents/graph/nodes/generation.py:778-798`
抓取失败只 warning,trace_update output 完全没记录 few-shot 是否命中/退化。W6 "rolling few-shot pool" 是否真在工作,审计时无法从 trace 区分"本来就没有 recent pass"和"抓取异常"。

### V-27.48 [Performance] `node_distill_context` 的 focused_fields 匹配是 O(fields × concepts) 双层 substring
**File**: `backend/agents/graph/nodes/generation.py:245-257`
对数百字段每个、每个 concept 做双向 `in` substring 匹配,字段集大时纯 CPU 浪费,且 substring 语义粗糙(concept "value" 命中 "valuation"/"undervalued")。与 V-26.37 同根。

### V-27.49 [Tech-debt] `node_validate` semantic_validator 每次调用重新实例化,加载 operator registry
**File**: `backend/agents/graph/nodes/validation.py:90-95`
`AlphaSemanticValidator(...)` 每次 `node_validate` 调用时新建(该 validator 启动时从 DB 加载 operator registry)。V-26.88 已修 persistence 侧同类,validation 侧遗漏。

### V-27.50 [Tech-debt] `_VALIDATOR` 是模块级单例,`semantic_validator` 不是 — 同函数内不一致
**File**: `backend/agents/graph/nodes/validation.py:32` vs `90`
同一个 `node_validate` 里 syntax validator 单例复用、semantic validator 每次新建,不一致没有理由。

### V-27.51 [Dead-code] `merge_state` 函数无调用点
**File**: `backend/agents/graph/state.py:286-291`
`merge_state(state, updates)` 函数体就是 `return updates`,grep 全仓无调用。

### V-27.52 [Tech-debt] `route_check_error` 在 edges.py 定义、workflow.py import 了但从未接线
**File**: `backend/agents/graph/edges.py:44-52`
`_build_graph` 全程没有 `add_conditional_edges(..., route_check_error, ...)`。`state.should_stop`/`state.error` 在 T1 主路径上没有任何边消费。import 了不用,等于声明了一个不存在的安全网。

### V-27.53 [Tech-debt] `node_hypothesis` 函数内反向 import `tasks/mining_tasks` 私有函数,违反分层
**File**: `backend/agents/graph/nodes/generation.py:434`
`from backend.tasks.mining_tasks import _get_dataset_fields` —— `agents/graph/nodes` 反向依赖 `tasks`,函数内 import 掩盖循环依赖风险,也让该函数无法独立测试。

### V-27.54 [Tech-debt] `node_code_gen` 的 `focus_hypotheses` 混合 strategy_dict 与 state.hypotheses,格式不统一
**File**: `backend/agents/graph/nodes/generation.py:844-847`
一半来自 `config.strategy.focus_hypotheses`,一半来自 `state.hypotheses`(后者用 `h.get("statement", h.get("idea", str(h)))` 三级 fallback),prompt 里出现混乱的混合表示。

### V-27.55 [Bug] `node_self_correct` 的 `knowledge_extracted`/`corrections_made` 仅进 trace,不持久化 — V-26.62 未修
**File**: `backend/agents/graph/nodes/validation.py:496-498, 503-507`
返回 dict 没把 `knowledge_extracted` 写进任何 state 持久字段,`MiningState` 也没有该字段。docstring "new corrections for future learning" 是 aspirational。**V-26.62 修复验证:未修。**

### V-27.56 [Tech-debt] `node_validate` 的 `type_warnings`/`semantic_errors` 收集后只用于日志,不影响路由
**File**: `backend/agents/graph/nodes/validation.py:71, 187-188`
收集这些聚合列表的 CPU/内存花了,价值只有日志。

### V-27.57 [Tech-debt] `_find_similar_errors` 仍只按 category 匹配,无 message similarity — V-26.56 未修
**File**: `backend/agents/graph/nodes/validation.py:263-279`
仍是 `if entry.get("error_category") == error_category` 取前 3 条。V-26.55 只修了上游 `_categorize_error` 的 word-boundary,未修下游检索粒度。**V-26.56 修复验证:未修。**

### V-27.58 [Tech-debt] `node_self_correct` retry 自检与 `route_after_validate` 边界语义重叠,需手动同步
**File**: `backend/agents/graph/nodes/validation.py:362-367` vs `edges.py:32`
node 内 `>= max_retries` 与 router 的 `< max_retries` 是两份需手动同步的真值表,未来有人改 edges 比较符就会不一致。**V-26.57 修复验证:加了自检功能正确,但引入双判断同步负担。**

### V-27.59 [Tech-debt] `MiningState.Config` 用 `validate_assignment = True`,每次字段赋值触发全量校验
**File**: `backend/agents/graph/state.py:277-279` + `validation.py:153-155`
`node_validate` 里每个 alpha 的 `model_copy()` 后三次赋值各触发一次 Pydantic 校验,batch 几十 alpha × 多 round 开销不可忽略。

### V-27.60 [Bug] `workflow.run` 对 `final_state` 同时做 `hasattr` 和 `isinstance(dict)` 双分支,掩盖返回类型不确定性
**File**: `backend/agents/graph/workflow.py:378-400`
双分支说明作者对 `app.ainvoke` 返回类型没把握。若 LangGraph 版本升级返回类型变化,这里会 silent 走 `else: ft = factor_tier` 丢失 state 里真实的 `factor_tier` 而非报错。

### V-27.83 [Tech-debt] `_merge_dedup_skels` 是 `node_simulate` 内闭包,每次调用重新绑定
**File**: `backend/agents/graph/nodes/evaluation.py:445-453`
**V-26.72 修复验证:LRU 语义已修对。** 仅结构上闭包捕获 `dedup_skel_buf`/`state`/`settings`,无法单测、每次重建,可提为模块函数。

### V-27.84 [Tech-debt] `summarise_round` 的 `pass_rate` 分母含 flip-retry alpha
**File**: `backend/agents/graph/early_stop.py:217, 241`
`total = max(1, len(pending_alphas))` 含 flip 产物,`pass_rate` 分母被稀释/抬高,`should_stop_early` 的 median pruner 吃这个失真值。

### V-27.85 [Dead-code] `node_save_results` 重算 pass/optimize/fail count,与 evaluation 已算的口径还不一致
**File**: `backend/agents/graph/nodes/persistence.py:654-659`
`node_evaluate` 已算过这些计数(进了 trace 但不在返回 state),`node_save_results` 又 `sum(...)` 重算一遍,且 optimize 口径不完全一致(evaluation 的含 V-16 downgrade,persistence 的不含)。

### V-27.86 [Tech-debt] `_process_hypothesis_feedback` 里 G-refine 整块(70 行)是有条件死代码
**File**: `backend/agents/graph/nodes/persistence.py:903-974`
整体包在 `if llm_service is not None` 下。结合 V-26.14 + V-27.69,即使 `llm_service` 注入了、`create_hypothesis` 写了 `parent_hypothesis_id`,`find_unused_refined` 仍不命中。建议明确标注 experimental / 下架。

### V-27.87 [Bug] `failure_feedback_queue` 的 `random.sample` 抽样在 attribution 过滤之前,有效记录率不稳定
**File**: `backend/agents/graph/nodes/evaluation.py:1613-1614, 1650`
先随机抽 3 条再 `should_record = attribution in ("hypothesis","both")` 过滤。若抽中的 3 条恰好都是 `unknown`/`implementation`,本轮 KB 一条都不记 —— 而队列里可能还有 hypothesis 归因的没被抽中。**V-26.40 修复验证:过滤逻辑修对,但与上游 random.sample 顺序耦合。**

### V-27.88 [Tech-debt] `random.sample`/`random.shuffle` 全程无 seed(与 V-26.53 同源)
**File**: `backend/agents/graph/nodes/evaluation.py:14, 1614`
mining 的 KB 学习样本选择不可复现,A/B 实验和 retrospective 难对齐。

### V-27.89 [Tech-debt] `node_save_results` 里 `max_iter` 解析吞掉所有异常静默回退 10
**File**: `backend/agents/graph/nodes/persistence.py:664-669`
`max_iterations` 配置错误(字符串/负数)被 silent 吞成 10,`should_stop_early` 拿着错的 cap。属于"Silent fail-open"主题。

### V-27.90 [Bug] `_incremental_save_alphas` 失败回退时 buffered 路径会对已落库 alpha 二次处理 + 重复 KB 写入
**File**: `backend/agents/graph/nodes/persistence.py:524-528, 539-592`
incremental 块 except 里 `use_incremental = False`。若 `_incremental_save_alphas` 是"部分 INSERT 成功后 outer commit 失败"回 `[]`,那部分 alpha 其实已落库,buffered 路径再当未持久化处理(靠 ON CONFLICT 兜),且 workflow 端会再触发一遍 `record_success_pattern` → 重复 KB 写入累积 `avg_*` 偏差。

### V-27.110 [Tech-debt] `_is_auth_error` 的 2KB body 嗅探阈值是 magic number,且 `len(response.content)` 对大响应触发整体读取
**File**: `backend/adapters/brain_adapter.py:1099`
`if response.status_code >= 400 or len(response.content) <= 2048` —— 2048 硬编码;成功大响应虽 status<400 但 `or` 右侧求值仍会强制读完整 body。

### V-27.111 [Dead-code] `_get_success_patterns`/`_get_failure_pitfalls` legacy 方法无调用方
**File**: `backend/agents/services/rag_service.py:654-683`
两个"Legacy method for backward compatibility"转发函数 grep 全仓无生产调用。

### V-27.112 [Tech-debt] `increment_pattern_usage` 改了 `entry.usage_count` 但从不 commit/flush,且无调用方
**File**: `backend/agents/services/rag_service.py:1036-1050`
`entry.usage_count += 1` 后直接 `return True`,依赖 caller session 生命周期,但 grep 无生产调用方,实际是悬空 API。

### V-27.113 [Tech-debt] `get_region_config` 失败时静默返回硬编码 USA 默认值
**File**: `backend/agents/services/rag_service.py:1389-1397`
任意 region 查不到就返回 `TOP3000/decay=4/SUBINDUSTRY`,CHN/EUR/ASI 用 USA 默认且无 warning。与 V-26.52 同主题的"非 USA region 静默退化"。

### V-27.114 [Tech-debt] `BrainAdapter.BASE_URL` 与文件头注释 / Origin header 的域名不一致
**File**: `backend/adapters/brain_adapter.py:53` vs `351-352`
`BASE_URL = "https://api.worldquantbrain.com"`,Origin/Referer 写 `https://platform.worldquantbrain.com`,CLAUDE.md 又称走 `platform.worldquantbrain.com`。三处域名表述不统一,排查时误导。

### V-27.115 [Tech-debt] `_wait_for_multisim`/`_wait_for_simulation` 的 `max_wait` 参数声明了但从不使用
**File**: `backend/adapters/brain_adapter.py:747, 852`
`max_wait: int = 900` 形参函数体内完全未引用,模拟卡死(BRAIN 一直回 Retry-After)时无墙钟上限,死循环风险。

### V-27.116 [Tech-debt] `record_success_pattern` 的 quality score 公式硬编码权重 0.6/0.3/0.1
**File**: `backend/agents/services/rag_service.py:1237`
这个 `score` 正是 retrieve 侧 `base_score` 来源,与 V-26.36 config 化方向相悖,调权重要改代码。

### V-27.117 [Bug] `query()` 与 `get_recent_pass_examples` 对 hypothesis 家族语义不一致(一软过滤一软加分)
**File**: `backend/agents/services/rag_service.py:508-514`
`get_recent_pass_examples` 对 hypothesis_id 是"matching 优先空则回退",`_get_success_patterns_enhanced` 只是 `score += RAG_SCORE_HYPOTHESIS_FAMILY_PATTERN`。caller 拿到的家族倾向强度取决于走哪个入口。**V-26.12 修复验证:语义不统一。**

### V-27.118 [Tech-debt] `_no_multisim` 系列 latch 跨进程不一致,与 `_SLOT_COUNTER_KEY` 走 Redis 的设计不对齐
**File**: `backend/adapters/brain_adapter.py:73-75`
同类"账户级 BRAIN 能力探测结果",sim slot 用 Redis 跨进程,multi-sim 权限 latch 却用 class 属性。架构不一致(V-27.94 是其后果)。

### V-27.119 [Performance] `refresh_all_stats` 串行逐个 `refresh_stats`,每个 2 条 SQL,N 个 hypothesis = 2N round-trip
**File**: `backend/services/hypothesis_service.py:410-429`
V-26.13 把 `refresh_stats` 从 1 query 拆成 2 query 后 batch refresh round-trip 翻倍,periodic reconcile 在 hypothesis 多时变慢,可用 GROUP BY 一次聚合。

### V-27.120 [Bug] `rounds_active` 的 60 秒桶估算未修(V-26.43 同问题),Phase 3 readiness 报告读到低估值
**File**: `backend/services/hypothesis_service.py:494-518`
`date_trunc("minute", Alpha.created_at)` 仍按分钟桶计 round 数,V-20.1 prefetch 同分钟双 round 被低估。`rounds_active` 被 `docs/phase3_readiness/` 分析脚本消费。**V-26.43 修复验证:未修,Phase 3 readiness 的 rounds_active 列系统性低估。**

### V-27.144 [Performance] `crisis_stress_test` 每次重算 5 region × 5 matrix,且 `compute_portfolio_matrix` 重复 load pickle
**File**: `backend/services/correlation_service.py:580, 626-630` + `sync_tasks.py:64-77`
一次 stress test = baseline + 4 windows 各调一次 `compute_portfolio_matrix`,每次都 `_load_cache` 重新 unpickle 整个 region 的 DataFrame。daily beat 对 5 region 跑 = 25 次完整 unpickle + 25 次 N×N corr。

### V-27.145 [Bug] `_series_to_returns` 对「有 PnL 记录却全 NaN」与「真没数据」归为同一 `empty`
**File**: `backend/services/correlation_service.py:108-114, 340-346`
一个有 PnL 记录但全 NaN 的 alpha 经 `ffill().shift(1)` 后 returns 全 NaN,`calc_self_corr` dropna 后为 0 → 返回 empty。这条还算安全,但运维无法区分数据质量问题。

### V-27.148 [Tech-debt] `CRISIS_WINDOWS` 日期硬编码,covid_2020 早于多数 OS alpha 历史成永远空转的窗口
**File**: `backend/services/correlation_service.py:62-71`
窗口是硬编码 module-level 常量,新危机事件需改代码 + 重部署。covid_2020 早于多数 OS alpha 的 PnL 历史,`calc_self_corr_by_window` 对几乎所有新 alpha 在该窗口恒返回 `insufficient_data`。

### V-27.150 [Bug] decay `_pick` 在 BRAIN metrics blob 字段为空串时 `float("")` 抛异常返回 None,decay 曲线出现空洞
**File**: `backend/services/decay_service.py:74-83`
fallback 分支 `metrics_blob.get(key)` 拿到 `""` 空字符串时 `float("")` 抛 ValueError 被捕获返回 None。同一指标在 flat 列有值时是数字、只在 blob 里且为空串时是 None,decay 曲线出现不连续空洞。

### V-27.151 [Tech-debt] `submit_alpha` 的 BrainAdapter 生命周期手工 `__aenter__/__aexit__`,异常路径脆弱
**File**: `backend/services/alpha_service.py:391-449`
`own_adapter` 分支手工调 dunder,`__aenter__` 自身抛异常时 `finally` 仍会 `__aexit__(None,None,None)` on 一个未完全初始化的 adapter。应用 `async with`。

### V-27.152 [Tech-debt] `_UNKNOWN_TYPES_SEEN` 进程级 set 永不清理,污染单测
**File**: `backend/can_submit.py:47, 112-113`
module-level set 只增不减,单测之间互相污染(一个测试触发的 warn 在另一个测试里不再触发),测试需手工 reset。

### V-27.153 [Tech-debt] CorrelationService router 为 cache-only 读也建真 BrainAdapter 并 `__aenter__`
**File**: `backend/routers/correlation.py:68-70, 87-89, 111`
`portfolio-matrix`/`crisis-summary` 是纯本地 pickle 读,注释也承认"read path never touches the network",但仍 `async with BrainAdapter()` 触发 BRAIN 登录认证。每个 cache-only 请求白付一次 auth 成本。

### V-27.154 [Performance] `list_alphas_by_tier` 的 submittable 过滤每行 JSONB cast,大表无索引
**File**: `backend/routers/factor_library.py:262-265, 518-519`
`Alpha.metrics["_self_corr"].astext.cast(Float)` 在 WHERE 里对每行做 JSONB 提取 + 双 cast,无表达式索引。`submittable` tab 和 `refresh-iqc` 都走这条,随表增长全表扫描。

### V-27.155 [Tech-debt] `refresh_iqc_batch` 用 `countdown=i*2` 错峰,但 enqueue 失败不影响 eta 计算
**File**: `backend/routers/factor_library.py:524-535`
`countdown` 按 `i` 递增(失败留空档),`eta = enqueued * 2` 用成功数 —— eta 与实际最后一个任务的 countdown 不一致,UI"约 Xs 内完成"偏小。

### V-27.156 [Bug] crisis snapshot `json.dump(..., default=str)` 掩盖类型错误
**File**: `backend/services/correlation_service.py:650`
`default=str` 让任何不可序列化对象(pd.Timestamp、numpy.float64、NaN-as-object)被静默 `str()` 化。某处遗漏时 `nan`(numpy)被写成字符串 `"nan"`,load 回来 UI 拿到字符串而非数字,无任何错误提示。

### V-27.157 [Bug] `_fetch_os_alpha_ids` 的 `a["id"]` 直接下标,BRAIN 返回缺 id 即 KeyError 中断整批
**File**: `backend/services/correlation_service.py:211`
其他字段都用 `.get()`,唯独 `id` 用下标。BRAIN 分页结果有一条缺 `id` 整个 `refresh_os_alpha_cache` 抛 KeyError 中断,已 fetch 的不保存。

### V-27.158 [Tech-debt] 三个相关方法三套「测不出」的词汇表(`empty`/`unknown`/`insufficient_data`...)
**File**: `backend/services/correlation_service.py:323, 384` + `routers/correlation.py`
`calc_self_corr` 返回 `{local,empty}`,`get_with_fallback` 返回 `{local,brain,unknown}`,`calc_self_corr_by_window` 又是 `{ok,insufficient_data,empty_pool,missing_window}`。caller 容易处理遗漏。

### V-27.159 [Tech-debt] `decay_curve` server_default `'[]'` 但读出 None 时靠防御代码掩盖迁移漏跑
**File**: `backend/models/alpha.py:99`
`nullable=False` + `server_default="[]"`,但 `maybe_append_decay_snapshot` 的 `list(alpha.decay_curve) if alpha.decay_curve else []` 在列还是 NULL 时不会崩 —— `nullable=False` 列读出 None 本身说明 schema 与迁移状态不一致,防御代码掩盖了迁移漏跑。

### V-27.160 [Half-done] crisis-window 评估节点写 `_crisis_correlations` 但无任何 gate / 消费链路
**File**: `backend/agents/graph/nodes/evaluation.py`(commit b4c5f0d,约 950-985 行)
evaluation 节点把 `crisis_by_window` 塞进 `alpha.metrics["_crisis_correlations"]`,spike 时只 `logger.info`。commit message 自称"advisory not gating"是有意为之,但结果是这块数据只进了 JSONB,既不影响 `quality_status`,也不在 factor_library 列表/submittable 过滤里被消费,前端只有 AlphaDetail 一张卡片读它。crisis stress test 跑了一整套 N×N 计算,唯一出口是详情页一个 pill —— 投入产出严重不匹配,功能处于"采集了但没用"的半成品状态。

---

## 跨阶段主题汇总

| 主题 | 涉及 V-27.X | 共同根因 |
|---|---|---|
| **V-26 修复「降级为观测」而非真修** | V-27.6, 8, 92, 105 | V-26.31/32/13/35 的 commit 实际只加了 log / counter,根因(无"真打过 BRAIN"标记、in-flight 不可取消、状态机不读 refresh_stats、counter 无导出)未动 |
| **V-26 修复覆盖不全(漏分支/漏路径/漏对称)** | V-27.4, 5, 10, 15, 31, 39, 40, 61, 62, 77, 91, 96, 97, 108, 109 | progress 写了不读、EARLY_STOPPED 漏判、prefetch adapter 漏 force_refresh、模块常量仍冻结;node_code_gen 修了 node_hypothesis 没修;PASS 路径修了 PROV 没修;success 侧修了 failure 侧没修 |
| **cascade 锁只保护 cascade,discrete 裸奔 + force-clear 引入双跑** | V-27.1, 2 | V-26.1-7 全是 cascade-only;watchdog force_clear 对"假死"worker 引入双 cascade 并发 |
| **G-refine 整条链确认死代码,V-26.14 完全未修** | V-27.46, 69, 86, 98 | `find_unused_refined`/`mark_superseded` 永不命中,但 hot path 仍每 round 执行无效查询,abandon 日志继续误导审计 |
| **Hypothesis 状态机与统计链路双轨** | V-27.71, 92, 100, 120 | `_process_hypothesis_feedback`(内存)与 `refresh_stats`(DB)从不交汇,排序键/Phase3 报告读 stale 列 |
| **node 自开 AsyncSessionLocal,事务隔离破裂** | V-27.38, 45, 70, 81 | V-26.23 未修,V-22.13/G-refine/B5 又新增多处,TOCTOU 窗口被独立 session 放大 |
| **State mutation 反模式 / scalar 传播补丁化** | V-27.47, 62, 63, 76 | 在 LangGraph 输入 state 上原地改;`current_hypothesis_id` 用 N 处 list[0] fallback 绕过传播 bug |
| **submit 轮询终止条件错误** | V-27.121, 122, 102 | "进行中"/"超时"/"201-202 accept" 都被误判,提交不可逆 → 烧 slot + 永久错标 |
| **非原子文件写 + 无锁** | V-27.130, 131, 156 | crisis JSON / OS PnL pickle 并发写损坏,被吞异常后静默降级 |
| **correlation 三态 None 的同类陷阱(V-26.81 升级版)** | V-27.126, 127, 129, 141, 145, 158 | "无法判定"在多个方法用不同词汇表,BRAIN 不可达时本地强信号被丢弃 |
| **crisis-window 核心计算失效** | V-27.124, 137, 138, 149 | corrwith 索引错位 + n_obs/n_alphas 含 NaN 行 → per-window corr 系统性偏低,告警测不出 |
| **跨进程状态不一致** | V-27.94, 118 | `_no_multisim` latch 用 class 属性,与 Redis slot counter 设计割裂 → 24h 惊群 |
| **Hardcode 配置缺 config 化(V-26 未清干净)** | V-27.108, 110, 116, 148 | failure 侧权重、2KB 阈值、quality score 权重、CRISIS_WINDOWS 日期 |
| **Silent fail-open** | V-27.21, 89, 101, 106, 113, 147 | 探测失败、配置缺失、缓存刷新失败均静默退化 |
| **死代码 / aspirational comment / 半成品入口** | V-27.37, 43, 51, 52, 86, 98, 111, 112, 142, 146, 160 | 删一半改一半;skip_precheck 死参数;self_corr 字段计算了没接线;crisis 数据采集了没消费 |
| **新代码数字提取不可靠** | V-27.132, 133, 143 | portfolio skeleton `_NUM_RE` 位置对齐非语义对齐、负号破坏配对、空集相等误命中 → 双因子预筛错杀真信号 |

---

## V-26 修复对抗性验证结论汇总

**真修对了**:V-26.4(Lua 原子释放)、V-26.7(Redis pool)、V-26.27(fail-closed)、V-26.28(cascade_phase normalize)、V-26.29(pipeline task 不泄漏)、V-26.46(dedup-before-trim)、V-26.55(word-boundary regex)、V-26.72(LRU move-to-end)、V-26.76(tier 参数)、V-26.77+#2(pyramid_multiplier NameError + self_corr=0.0 保留)、V-26.80(turnover band 对称)、V-26.86(audit 重试计数)、V-26.87(删冗余 SELECT)、V-26.88(persistence validator 单例)、V-26.94/95(阈值 config 化)、V-26.26(workflow 补 FAIL 路径 touched_hids)

**半修 / 治标 / 覆盖不全**:V-26.2(prefetch 路径漏)、V-26.3(写了不读)、V-26.5(引入双跑 race)、V-26.8+V-26.12(cap 是 id 序架空 family boost)、V-26.9(failure 侧不对称)、V-26.11(只隔离最低风险一项)、V-26.13(只修 denormalized 列)、V-26.17(fallback 重引入分歧)、V-26.19(scoring 链仍编造)、V-26.21(checks 缺失时 score-only 旁路复活)、V-26.24(只覆盖 simulate/poll)、V-26.25(引入跨进程惊群)、V-26.31(反向高估)、V-26.33(多跳 revive 不可追溯)、V-26.35(counter 无导出)、V-26.36(failure 侧没动)、V-26.49/V-26.48/V-26.50(node_hypothesis 漏)、V-26.57(双判断同步负担)、V-26.61(import-time 求值 + silent fallback)、V-26.79(PROV 路径漏)、V-26.83(模块别名仍冻结)、V-26.84(enqueue 失败锁泄漏)、V-26.89(旧 UPDATE 变纯冗余写)、V-26.90(INCR+EXPIRE 非原子)、V-26.91(dateModified 语义错配)、V-26.92(KB 侧未堵)、V-26.93(依赖无人保证的 key)

**完全未修(根因仍在,且部分已 backlog 化)**:V-26.14(G-refine 死代码)、V-26.15(abandon 只看 pass_count)、V-26.16(aspirational 日志)、V-26.18(fix 重验证前入 KB)、V-26.22(`locals().get` 反模式)、V-26.23/V-26.64(node 自开 session + dedup race)、V-26.32(PAUSE 不取消 in-flight)、V-26.43(60s 桶低估)、V-26.56(category-only 匹配)、V-26.58(is_valid 三态,已 backlog)、V-26.60(`fields[:50]` 截断)、V-26.62(knowledge_extracted 不持久化)、V-26.75(retryable caller 没接)、V-26.1/6(SIGKILL,已如实 backlog)

---

## 优先级建议

**首轮修复(影响生产数据正确性 / 不可逆操作)**:
- V-27.121 / V-27.122 / V-27.123 — submit 轮询误判 + 无并发保护(提交不可逆,烧 BRAIN slot)
- V-27.1 / V-27.2 — watchdog 双跑(cascade force-clear race + discrete 裸奔)
- V-27.124 — crisis-window corrwith 索引错位(核心功能失效)
- V-27.93 — KB 事务夹带 commit 主路径仍在(alpha rollback 后 KB 漂移)
- V-27.63 — node_evaluate 写穿输入 state
- V-27.72 — rate-limit 永久失效模式
- V-27.91 — submit/data-fetch 路径 poison cache 未自愈

**次轮(影响 mining 质量 / KB 累积 / 状态机)**:
- V-27.92 / V-27.68 — Hypothesis 状态机根因未真修 + abandon 误判
- V-27.61 — retryable 协议断链(429 alpha 永久 FAIL)
- V-27.73 / V-27.97 — 无 alpha_id alpha + hypothesis_id=None 污染 KB
- V-27.95 — RAG retrieve 自锁 7 天窗口
- V-27.62 / V-27.77 / V-27.78 — V-16 / V-12 / BRAIN-aware downgrade 的旁路与编造数据
- V-27.3 / V-27.14 — sync 维度错配(universe 漏写 / created vs submitted)
- V-27.31 — node_hypothesis 无 try/except

**第三轮(性能 + 可维护 + 新功能闭环)**:
- V-27.130 / V-27.131 — 非原子文件写
- V-27.94 / V-27.118 — 跨进程 latch 不一致
- V-27.146 / V-27.160 — self_corr 字段没接线 / crisis 数据没消费(新功能半成品)
- V-27.132 / V-27.133 / V-27.143 — portfolio 双因子预筛数字提取不可靠
- 其余 🟡 / 🟢

**长期(架构清理)**:
- G-refine 整条链下架(V-27.46/69/86/98 + V-26.14)
- node 自开 session 统一收敛(V-27.38/45/70/81)
- correlation「测不出」三态词汇表统一(V-27.158)
- Hypothesis 状态机与 refresh_stats 单一数据源(V-27.92/100/120)
- 死代码下架(V-27.37/43/51/52/111/112/142)、config 化批次(V-27.108/116/148)

---

## 闭环动作模板

每项 V-27.X 需要落到下列之一:
1. **commit**(`fix/feat/docs: V-27.X — <一句话>`)
2. **backlog stub script**(scripts/v27_x_*.py)
3. **RCA doc**(docs/rca_2026-05-14_v27_<topic>.md)
4. **plan 修订**(本文件加 mitigation 段)

> **审查方法说明**:本轮由 5 路并行 agent 分子系统深啃,每路均 `git show` 对应 V-26 修复 commit + 逐文件打开核对行号。V-26 修复验证结论基于「读修复 commit diff → 读当前代码 → 判断根因是否消除」三步。新增三功能(submit/crisis-window/decay)为 V-26 未覆盖,全量 `git show` + 逐文件审查。
