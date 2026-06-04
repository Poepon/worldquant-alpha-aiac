# Alpha Mining Task 全流程质量审查 — V-26 系列

**日期**: 2026-05-13
**审查者**: Claude(对抗性审查)
**审查范围**: 从 Celery `run_mining_task` 入口 → LangGraph workflow → BRAIN simulate → evaluate → persistence → KB 写回 + Hypothesis lifecycle + watchdog
**严重度**: 🔴 阻断 / 🟡 中 / 🟢 改进
**编号约定**: `V-26.X` 顺接 V-25 系列;X 用 1-3 位数字按发现顺序

> 本审查不规定修法,只暴露问题。**下一步**:用户对发现挑选优先级 → 拆 plan → 进入修复 commit。
> 每一项编号注明 file:line 锚点 + 类型(Bug / Race / Dead-code / Performance / Tech-debt / Half-done)。

---

## 🔴 阻断级(Bug + 已观测到现网影响)

### V-26.1 [Bug] 锁泄漏导致 cascade task 3h 内无法重启
**File**: `backend/tasks/mining_tasks.py:71-134`
Redis cascade_lock 在 line 79 acquire 后,line 126-134 之间(`db.execute(update RUNNING)` + `_get_or_create_run`)若抛错,**`_release_lock` 永不调用**;锁 TTL=10800s,导致 watchdog 5min 探测到 dead session 后重发 celery 任务连撞 3 小时 `duplicate_active_run`。
**影响**: V-19.7 watchdog "revive dead session" 对硬 crash **实际不工作**。

### V-26.2 [Bug] Cascade T1→T2 同迭代内不刷新 BRAIN session
**File**: `backend/tasks/mining_tasks.py:998-1006`
`brain.ensure_session()` 只在 `while True` 顶部调,**T1→T2 phase 边界不重刷**。token TTL 默 4h,T1+T2 合计 >4h 时 T2 直接 auth fail 全军覆没,与 V-22.7 body-marker detect 形成双层补救但根因未消除。

### V-26.3 [Bug] Cascade 路径**从不更新 `progress_current`**
**File**: `backend/tasks/mining_tasks.py:284` vs `956-1132`
discrete 路径 line 284 增量 `task.progress_current`;cascade 路径整段无此写入 → 前端进度条永远 0,`daily_goal` 判停失效。

### V-26.4 [Race] `_release_lock` GET+DEL 非原子
**File**: `backend/tasks/mining_tasks.py:112-123`
注释自称 "Lua-style check-and-delete" 但代码是 Python GET 后 DEL 两步;TTL 边界处可误删他人锁。**应换 Lua EVAL**。

### V-26.5 [Bug] Watchdog 不强制清锁
**File**: `backend/tasks/session_watchdog.py:134-167`
revive dead session 时只新 dispatch celery,不 DELETE cascade_lock_key → 与 V-26.1 联合导致 revive 实际失效 3h。

### V-26.6 [Bug] `progress_current` / `task_time_limit=3600` 与多小时 cascade 设计冲突
**File**: `backend/celery_app.py:28`
celery `task_time_limit=3600` 1h 后 SIGKILL,而 cascade 是 `while True`;SIGKILL 触发 V-26.1 锁泄漏。

### V-26.7 [Race] `_redis_cli` 永不 close
**File**: `backend/tasks/mining_tasks.py:75-85`
每次 task 创建新 redis 客户端,无 `close()` / 不入 pool;task 量大时连接耗尽。

### V-26.8 [Bug] RAG retrieve 全表 SELECT,无 SQL LIMIT
**File**: `backend/agents/services/rag_service.py:370-376, 475-481`
`_get_success_patterns_enhanced` / `_get_failure_pitfalls_enhanced` 都是 `SELECT * WHERE is_active=true`,Python 端排序后切 5/10。KB 行数增长后 **内存 + CPU 不收敛**;每轮 mining 都拉全表。

### V-26.9 [Half-done] `record_success_pattern` 重复 hit 只更新 `avg_sharpe`
**File**: `backend/agents/services/rag_service.py:1077-1093`
`avg_fitness` / `avg_turnover` / `expected_sharpe` 永远停留在首次写入值;running-average 只有 sharpe 一项。RAG score 部分受影响。

### V-26.10 [Bug] `_find_similar_success/pitfall` 跨 region 合并行
**File**: `backend/agents/services/rag_service.py:1163-1178`
`region` 参数标注"defer"实际 `pass`;同 skeleton 跨 USA/CHN/EUR 折叠到一行,regional avg 失真。

### V-26.11 [Race] `record_success_pattern` / `_track_retrieval_hit` 内嵌 `db.commit`
**File**: `backend/agents/services/rag_service.py:282, 1148`
KB 写在 RAG retrieve 与 persistence 之间多次 commit,**caller 未提交的事务被夹带 commit**;之后 alpha 写若 rollback,KB 已落盘 → 数据漂移。

### V-26.12 [Half-done] Phase 2 B8 半实现 — RAG retrieve 端不接 `hypothesis_id`
**File**: `backend/agents/services/rag_service.py:286-292` (retrieve) vs `1086-1090, 1130-1131` (write)
写入端把 `hypothesis_id` 注入 KB 行 `meta_data.hypothesis_ids`;retrieve 端 `query()` **没有 `hypothesis_id` 参数**。"按 hypothesis 家族 RAG"承诺没兑现。

### V-26.13 [Bug] Hypothesis lifecycle counter 不看 alpha_failures
**File**: `backend/services/hypothesis_service.py:304-313, 467-472`
`refresh_stats` 只 COUNT `Alpha` 表;V-25.B 已加 `alpha_failures.hypothesis_id` 列,但 lifecycle counter / `auto_activate_if_eligible` 仍走旧逻辑 → **hypothesis 跑了 50 个 FAIL alpha 仍 alpha_count=0**,卡 PROPOSED → V-25 RCA layer 2 "275 ACTIVE orphaned" 真根因。

### V-26.14 [Dead-code] G-refine 上下游全断
**File**: `backend/services/hypothesis_service.py:248-252, 365-422`
`find_unused_refined` JOIN `parent.status=SUPERSEDED`;V-25 RCA SQL 证实 **0/673 rows 有 `parent_hypothesis_id`** → 永不命中。`mark_superseded` 验证逻辑健全,但**全代码库无任何路径写 child.parent_hypothesis_id** → mark_superseded 永远 ValueError。整条 G-refine 链路是死代码。

### V-26.15 [Bug] `should_abandon_hypothesis` 只看 pass_count,不看 alpha_count
**File**: `backend/agents/graph/early_stop.py:175`
"3 round 各 0 alpha"和"3 round 各 50 alpha 全 FAIL"等同处理 — 前者根本无证据。

### V-26.16 [Bug] abandon trigger 日志写 "convert to SUPERSEDED via G-refine"
**File**: `backend/agents/graph/early_stop.py:196-199`
Aspirational 日志,实际 G-refine 不工作(V-26.14),误导审计。

### V-26.17 [Bug] `_ERROR_KNOWLEDGE_BASE` 进程内全局 list
**File**: `backend/agents/graph/nodes/validation.py:214, 273-274`
worker 重启 / watchdog revive 后丢失;跨 worker 不共享;"learn from past corrections" 实际只在单进程一次性,且超 100 条砍后 50 条 FIFO(非 quality-based)。

### V-26.18 [Bug] `_record_correction` 在 fix 重验证前就入 KB
**File**: `backend/agents/graph/nodes/validation.py:391-397`
docstring 说"成功 correction"实际记录所有尝试性 fix(本身可能仍 invalid)→ KB 噪声累积。

### V-26.19 [Bug] V-12 IS/OS 一致性 gate 在 test_sharpe 缺失时**失效**
**File**: `backend/agents/graph/nodes/evaluation.py:707`
`test_sharpe` 缺失时编造为 `sharpe * 0.8`;V-12 ratio threshold 0.3-0.4 → 0.8 fake **总过**。V-12 本意 "避免 IS-only overfit",在 BRAIN 不返 os_sharpe 时反被绕过。

### V-26.20 [Bug] `near_pass` PROVISIONAL 路径不跑 V-16 / brain_actionable_fails
**File**: `backend/agents/graph/nodes/evaluation.py:903-946` vs `947-949`
PASS 路径 V-16 suspicion + BRAIN 下调齐全,PROVISIONAL 旁路全部跳过 → high-sharpe PROVISIONAL 进 KB 学习池 unfiltered。

### V-26.21 [Bug] PASS gate 是 `hard_gate_pass AND (meets_thresholds OR score >= threshold)`
**File**: `backend/agents/graph/nodes/evaluation.py:903`
score-only 旁路;`brain_actionable_fails` 仅 fitness/sharpe/concentrated 下调,**LOW_TURNOVER / MATCHES_PYRAMID / HIGH_CORRELATION 等 BRAIN FAIL 类型不触发下调**。

### V-26.22 [Tech-debt] `self_corr_source = locals().get(...)` 反模式
**File**: `backend/agents/graph/nodes/evaluation.py:847`
refactor 易炸,变量名重命名会 silent 改语义。

### V-26.23 [Race] node_simulate / node_code_gen 自开 AsyncSessionLocal
**File**: `backend/agents/graph/nodes/generation.py:754`, `backend/agents/graph/nodes/evaluation.py:296`
绕过 graph 注入的 `db`,事务隔离破裂、连接池压力。

### V-26.24 [Race] BRAIN session Redis cache "trust" 无 first-401 → flush 自愈
**File**: `backend/adapters/brain_adapter.py:419-422`
注释明确 "trust Redis";server 端 invalidate 后 cache TTL 剩余时间内所有 sim fail。

### V-26.25 [Bug] `_no_multisim` latch 单向不可恢复
**File**: `backend/adapters/brain_adapter.py:570-572`
账户升级 Consultant 后需 worker 重启才能再走多 sim;无定期 re-probe。

### V-26.26 [Bug] AlphaFailure 写入不触发 Hypothesis lifecycle 刷新
**File**: `backend/agents/graph/workflow.py:601-612` + `backend/services/hypothesis_service.py`
V-25.B 已加 hypothesis_id 列,但 workflow 写完 AlphaFailure 不调 `auto_activate_if_eligible` / `refresh_stats` → 与 V-26.13 联合,FAIL alpha 完全无法推动 hypothesis 状态机。

---

## 🟡 中等(Tech-debt + 边界 + Performance)

### V-26.27 [Race] Redis-down 时 cascade lock fail-open
**File**: `backend/tasks/mining_tasks.py:85-89` — `cascade_lock_acquired = True`,退化回 cascade-stuck-T2 原始 bug。
### V-26.28 [Bug] Invalid `cascade_phase` → CPU 空转 — 三个 if 全 skip 无 fallback。
**File**: `backend/tasks/mining_tasks.py:1015-1101`
### V-26.29 [Race] Pipeline 内层异常 leak background asyncio.Task
**File**: `backend/tasks/mining_tasks.py:914-925, 940-948`
### V-26.30 [Tech-debt] `.cascade_phase_diag.log` file-based 调试输出未清理
**File**: `backend/tasks/mining_tasks.py:974-981, 793-800`
### V-26.31 [Bug] Quota guard 只计 `Alpha`,**忽略 `alpha_failures`**
**File**: `backend/tasks/session_watchdog.py:201-206` — BRAIN sim 调用数被低估。
### V-26.32 [Bug] Quota guard 触发 PAUSE 不取消 in-flight sim
**File**: `backend/tasks/session_watchdog.py:225-244`
### V-26.33 [Half-done] Watchdog revive 创建新 ExperimentRun 丢原 config_snapshot
**File**: `backend/tasks/session_watchdog.py:137-149`
### V-26.34 [Performance] `_filter_hallucinated` 每条 entry 调 regex
**File**: `backend/agents/services/rag_service.py:232-244`
### V-26.35 [Race] V-24.C `valid_ops` 加载失败 fail-open(`return entries`)
**File**: `backend/agents/services/rag_service.py:230-231`
### V-26.36 [Tech-debt] RAG scoring weights 硬编码(100/50/30/20/10)
**File**: `backend/agents/services/rag_service.py:419-425`
### V-26.37 [Tech-debt] `_classify_pattern_family` substring 匹配
**File**: `backend/agents/services/rag_service.py:74-89`
### V-26.38 [Half-done] `KnowledgeType.FIELD_INSIGHT` enum 仍存在但无读路径
**File**: `backend/config.py:275-280`、`backend/tests/unit/test_core_knowledge.py:27`
### V-26.39 [Bug] V-24.E backlog "enable retrieve path" 无 owner / 无截止
**File**: `backend/config.py:278`
### V-26.40 [Tech-debt] Failure 写回把 "unknown" attribution 当 hypothesis 处理
**File**: `backend/agents/graph/nodes/evaluation.py:1353`
### V-26.41 [Bug] `mark_abandoned` 允许重写 reason,丢原始诊断
**File**: `backend/services/hypothesis_service.py:222-228`
### V-26.42 [Tech-debt] `set_active_flag` 把 regime-freeze tag 拼到 `abandon_reason`
**File**: `backend/services/hypothesis_service.py:272-275` — 字段语义被污染。
### V-26.43 [Bug] `rounds_active` 用 60s 桶估算,V-20.1 prefetch 同分钟双 round 被低估
**File**: `backend/services/hypothesis_service.py:444`
### V-26.44 [Tech-debt] 所有 lifecycle 方法不 commit,依赖 caller
**File**: `backend/services/hypothesis_service.py:174-289`
### V-26.45 [Performance] `list_active` ORDER BY created_at desc limit 50 → 老 hypothesis 被 starve
**File**: `backend/services/hypothesis_service.py:150-159`
### V-26.46 [Performance] `code_gen_fields[:60]` 截断未去重
**File**: `backend/agents/graph/nodes/generation.py:778-780`
### V-26.47 [Tech-debt] `state.operators[:50]` 截前 50 无相关性排序
**File**: `backend/agents/graph/nodes/generation.py:796`
### V-26.48 [Bug] LLM `response.parsed.alphas` 类型不验证
**File**: `backend/agents/graph/nodes/generation.py:838-868`
### V-26.49 [Tech-debt] LLM 异常用 `type('obj', ..., {...})()` mock 对象
**File**: `backend/agents/graph/nodes/generation.py:825-827`
### V-26.50 [Bug] `expected_sharpe` 从 LLM 输出直读 — 注入向量
**File**: `backend/agents/graph/nodes/generation.py:857`
### V-26.51 [Bug] `composite_fields._LOADED` 缓存无 mtime 检查
**File**: `backend/agents/seed_pool/composite_fields.py:89-91`
### V-26.52 [Bug] `REGION_BLOCKED_FIELDS` 只填了 USA,其他 region 空集
**File**: `backend/agents/seed_pool/composite_fields.py:59-67` — CHN/EUR/ASI/GLB 字段缺失 silent 烧配额。
### V-26.53 [Tech-debt] `random.shuffle` 无 seed → mining 不可复现
**File**: `backend/agents/seed_pool/composite_fields.py:266`
### V-26.54 [Bug] YAML 加载只 filter 真值 `name`,不查重 → 重名 composite bucket 冲撞
**File**: `backend/agents/seed_pool/composite_fields.py:101`
### V-26.55 [Bug] `_categorize_error` substring 匹配 "matrix" 误归 type_error
**File**: `backend/agents/graph/nodes/validation.py:221-232`
### V-26.56 [Tech-debt] `_find_similar_errors` 只按 category 匹配,无 message similarity
**File**: `backend/agents/graph/nodes/validation.py:235-251`
### V-26.57 [Bug] `node_self_correct` 不自检 retry_count 上限,完全靠 router
**File**: `backend/agents/graph/nodes/validation.py:312`
### V-26.58 [Bug] `is_valid=None` 三态,下游 `if alpha.is_valid:` 行为不一致
**File**: `backend/agents/graph/nodes/validation.py:386`
### V-26.59 [Tech-debt] SELF_CORRECT `temperature=0.3` / dedup `similarity_threshold=0.90` 硬编码
**File**: `backend/agents/graph/nodes/validation.py:60, 354`
### V-26.60 [Bug] `state.fields[:50]` 给 SELF_CORRECT — 原表达式用 51+ 字段会被 "fix" 误删
**File**: `backend/agents/graph/nodes/validation.py:318`
### V-26.61 [Bug] SELF_CORRECT 不走 prompts.yaml registry
**File**: `backend/agents/graph/nodes/validation.py:22`
### V-26.62 [Half-done] `corrections_made` / `knowledge_extracted` 仅 trace_step 输出,不持久化
**File**: `backend/agents/graph/nodes/validation.py:413-417`
### V-26.63 [Race] 直接 mutate `state.pending_alphas[idx]`(LangGraph 输入态)
**File**: `backend/agents/graph/nodes/evaluation.py:308-309, 366-370` — replay/debug 不一致。
### V-26.64 [Race] DB dedup 与 V-20.1 prefetch race window — ON CONFLICT 兜数据但 sim 浪费配额
**File**: `backend/agents/graph/nodes/evaluation.py:294-299`
### V-26.65 [Tech-debt] 默认 sim 设置硬编码 `delay=1, decay=4, neutralization=SUBINDUSTRY`
**File**: `backend/agents/graph/nodes/evaluation.py:504-506`
### V-26.66 [Bug] `brain.simulate_batch` 单次 except 让整批失败,无单 alpha 重试
**File**: `backend/agents/graph/nodes/evaluation.py:508-510`
### V-26.67 [Tech-debt] V-12 把 `os_sharpe` / `test_sharpe` 等价取,语义不同
**File**: `backend/agents/graph/nodes/evaluation.py:63`
### V-26.68 [Tech-debt] V-16 阈值 3.0 / standard_windows / risky_denoms 全硬编码
**File**: `backend/agents/graph/nodes/evaluation.py:85, 109, 88-96`
### V-26.69 [Bug] V-16 divide-by-zero regex 只识别浅层 `divide(_, var)`,嵌套漏检
**File**: `backend/agents/graph/nodes/evaluation.py:111-123`
### V-26.70 [Bug] V-16 lookahead 用 string idx 判定 ts_delay 包裹 — 误判 sibling 也是安全
**File**: `backend/agents/graph/nodes/evaluation.py:131-140`
### V-26.71 [Bug] `bucket_results[j]` 短返回 silent 兜 missing,无 alert
**File**: `backend/agents/graph/nodes/evaluation.py:497`
### V-26.72 [Bug] `_merge_dedup_skels` `dict.fromkeys` 保留首次位置 — LRU/FIFO 倒置
**File**: `backend/agents/graph/nodes/evaluation.py:331-334`
### V-26.73 [Bug] `_acquire_sim_slot` timeout 直接失败,无 re-queue
**File**: `backend/adapters/brain_adapter.py:505-507`
### V-26.74 [Tech-debt] `authenticate` retry 无 jitter,多 worker 同步过期 → 雷击 BRAIN
**File**: `backend/adapters/brain_adapter.py:452`
### V-26.75 [Bug] simulate 收到 429 → 该 alpha 永久 failed(caller 不重试)
**File**: `backend/adapters/brain_adapter.py:518-519`
### V-26.76 [Tech-debt] V-12 IS/OS check 无 tier 参数,T3 用同一 0.3/0.4 ratio
**File**: `backend/agents/graph/nodes/evaluation.py:39`
### V-26.77 [Dead-code] `pyramid_multiplier` 提取后未使用
**File**: `backend/agents/graph/nodes/evaluation.py:730`
### V-26.78 [Tech-debt] `corr_check_threshold=0.5` 硬编码默认
**File**: `backend/agents/graph/nodes/evaluation.py:672`
### V-26.79 [Race] `alpha.metrics["_v16_suspicion_flags"]` 直接 mutate 原 alpha 非 updated copy
**File**: `backend/agents/graph/nodes/evaluation.py:914`
### V-26.80 [Bug] near_pass turnover 区间下限用 regular,上限用 prov — 不对称
**File**: `backend/agents/graph/nodes/evaluation.py:893-900`
### V-26.81 [Bug] `compute_can_submit` 三态 None,caller 用 `if not can_submit` 会把 None 当 False
**File**: `backend/can_submit.py:39-44, 57`
### V-26.82 [Bug] `compute_can_submit` 只识别 FAIL/PENDING,未来 WARNING/ERROR 类默 PASS
**File**: `backend/can_submit.py:52-56`
### V-26.83 [Tech-debt] `IQC_AUDIT_BACKFILL_LIMIT=50` / `countdown=2` 硬编码
**File**: `backend/tasks/refresh_tasks.py:382, 448`
### V-26.84 [Race] Sweep 不检测 "已入队未完成" alpha,周期间可重复入队
**File**: `backend/tasks/refresh_tasks.py:444-454`
### V-26.85 [Bug] Sweep ORDER BY `updated_at DESC` 不代表"最 stale" — sync 写入也刷
**File**: `backend/tasks/refresh_tasks.py:437`
### V-26.86 [Bug] 失败 audit 无重试计数,周期性永久重试 BRAIN 500 alpha
**File**: `backend/tasks/refresh_tasks.py:445-454`
### V-26.87 [Performance] SELECT pre-check + ON CONFLICT 双重保护冗余
**File**: `backend/agents/graph/nodes/persistence.py:108-124, 184-200`
### V-26.88 [Performance] `_extract_used_fields` 每 alpha 实例化 `AlphaSemanticValidator`
**File**: `backend/agents/graph/nodes/persistence.py:87-97`
### V-26.89 [Bug] `fields_used` 在 outer commit 之后才写,crash 间隙留 NULL alpha 无 backfill 路径
**File**: `backend/agents/graph/nodes/persistence.py:281-307`
### V-26.90 [Performance] `enqueue_can_submit_refresh(countdown=30)` 无频率限制,PASS 突发洪流
**File**: `backend/agents/graph/nodes/persistence.py:340`
### V-26.91 [Tech-debt] `metrics_snapshot_at` 全批用一个 wall-clock,sim 时间差被抹
**File**: `backend/agents/graph/nodes/persistence.py:136, 179`
### V-26.92 [Bug] 无 alpha_id 时 silent 丢失(`landed=False`,不入 db)
**File**: `backend/agents/graph/nodes/persistence.py:316`
### V-26.93 [Half-done] V-22.1 record_success_pattern 可能 hypothesis_id=None — KB 行 NULL/非 NULL 混合
**File**: `backend/agents/graph/nodes/persistence.py:446`
### V-26.94 [Tech-debt] `classify_attribution` 75% 阈值硬编码
**File**: `backend/agents/graph/early_stop.py:118-121`
### V-26.95 [Tech-debt] `WARMUP_ROUNDS=5` / `PASS_RATE_DROP_RATIO=0.5` 硬编码
**File**: `backend/agents/graph/early_stop.py:25-26`
### V-26.96 [Bug] stagnation max(last 2) vs max(first 3) 不随 max_iterations 缩放
**File**: `backend/agents/graph/early_stop.py:72-79`
### V-26.97 [Tech-debt] round-level attribution 75% 桶压扁 round 内 alpha 间归因
**File**: `backend/agents/graph/early_stop.py:115-122`

---

## 跨阶段主题汇总

| 主题 | 涉及 V-26.X | 共同根因 |
|---|---|---|
| **B5/B6/B8 半实现** | V-26.12, 13, 14, 16, 26, 93 | Phase 2 hypothesis lifecycle 写入和读取/状态机不同步 |
| **Cascade 锁与 watchdog 协作断裂** | V-26.1, 4, 5, 6, 27, 33 | Redis 锁 TTL=3h、watchdog 5min、celery 1h 三个时间窗口未对齐 |
| **In-memory state 缺失持久化** | V-26.9, 17, 22, 29 | round_history / _ERROR_KB / asyncio.Task 跨 worker 不可恢复 |
| **Hardcode 配置缺 config 化** | V-26.36, 53, 59, 65, 67, 68, 78, 83, 94, 95 | 调优需改代码 |
| **N+1 / 全表 SELECT** | V-26.8, 34, 87, 88 | KB / persistence 缺索引或缓存 |
| **State mutation 反模式** | V-26.22, 63, 79 | 在 LangGraph 输入 state 上原地改 |
| **Silent fail-open** | V-26.27, 35, 48, 81, 82, 92 | Defensive but 隐藏问题 |
| **死代码 / aspirational comment** | V-26.14, 16, 38, 39, 77 | 删一半改一半 |

---

## 优先级建议

**首轮修复(影响生产数据正确性)**:
V-26.1, V-26.5, V-26.13, V-26.19, V-26.21, V-26.26, V-26.52

**次轮(影响 mining 质量 / KB 累积)**:
V-26.2, V-26.3, V-26.12, V-26.14, V-26.17, V-26.20, V-26.31

**第三轮(性能 + 可维护)**:
其余 🟡

**长期(架构清理)**:
死代码下架(V-26.14, 38, 77)、prompts.yaml 统一(V-26.61)、config 化(V-26.36 等批次)

---

## 闭环动作模板

每项 V-26.X 需要落到下列之一:
1. **commit**(`fix/feat/docs: V-26.X — <一句话>`)
2. **backlog stub script**(scripts/v26_x_*.py)
3. **RCA doc**(docs/rca_2026-05-13_v26_<topic>.md)
4. **plan 修订**(本文件加 mitigation 段)
