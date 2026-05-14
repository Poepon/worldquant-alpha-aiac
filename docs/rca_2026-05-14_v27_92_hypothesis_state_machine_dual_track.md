# RCA — V-27.92 Hypothesis 状态机与统计链路双轨(内存态 vs DB 真值)

**日期**: 2026-05-14
**来源**: `docs/quality_review_mining_task_2026-05-14.md` V-27.92(中等)+ 同根因簇 V-27.71 / V-27.100 / V-27.120
**状态**: 根因确认,根治需状态机重构,排期 plan

---

## 现象

Hypothesis 的生命周期状态转换(`mark_active` / `mark_promoted` /
`mark_abandoned`)由 `persistence.py:_process_hypothesis_feedback` 驱动,
而它的判定输入**全部是本轮内存量**:

- `alpha_count = len(pending_alphas)` —— 内存计数(persistence.py:803)
- `pass_count = sum(... for a in pending_alphas)` —— 内存计数(:804)
- `entry = {round_index, alpha_count, pass_count, attribution, ...}` —— 内存 entry(:854)
- `history_out[hid] = history_so_far[hid] + [entry]` —— 内存 history 累积(:868-870)
- `should_abandon_hypothesis(history_out.get(primary_hid, []), ...)` —— **读内存 history**(:915)

与此**平行**的是 `hypothesis_service.refresh_stats` —— 它正确地从 DB
合并 `alpha_failures` 计数(V-26.13 真修),`workflow` 也补了 FAIL 路径的
`touched_hids`(V-26.26 真修)。但 `refresh_stats` 的产出**从不被状态机
读取** —— 它只刷新前端展示用的 denormalized 列。

两条链路从不交汇:
- **状态机链路**(内存):`pending_alphas` → `history_out` → `should_abandon` → `mark_*`
- **统计链路**(DB):`alpha_failures` → `refresh_stats` → denormalized 列

## 根因

worker 重启 / Celery task 边界一切换,`_process_hypothesis_feedback` 接收的
`history_so_far`(来自 `state.round_history` 内存累积)就丢失或不完整。
后果:

1. **FAIL alpha 推不动状态机** —— `should_abandon_hypothesis` 拿到的
   history 窗口不足 `n_rounds`,直接 `return False`(early_stop.py:168)。
   一个本该被 abandon 的 hypothesis 永远卡在 ACTIVE / PROPOSED。
2. **跨 worker 不一致** —— V-20.1 pipeline 的 prefetch round 在独立 session
   跑,内存 history 不共享;两个 round 各自累积各自的 `history_out`。
3. **DB 已有真值却不用** —— `alpha_failures` 表里有完整的 per-hypothesis
   失败记录,`refresh_stats` 已经会算,但 `should_abandon` 偏偏读内存。

V-26.13 / V-26.26 是"真修",但它们修的是**统计链路**;V-26 文档点名的
"卡 PROPOSED 真根因"是**状态机链路**,状态机至今不读 `refresh_stats`。

## 同根因簇

这不是孤立 bug,是一组同源问题:

| 条目 | 表现 | 同源点 |
|---|---|---|
| V-27.92 | `should_abandon` 读内存 history,worker 重启失效 | 状态机不读 DB |
| V-27.71 | `alpha_count = len(pending_alphas)` 含 flip-retry 追加的 alpha,虚高 | 内存计数不准 |
| V-27.100 | V-26.45 的 `untouched_first` 排序键基于 stale 的 `alpha_count` denormalized 列 | denormalized 列 vs 真值 |
| V-27.120 | `rounds_active` 60 秒桶估算,Phase 3 readiness 报告读到低估值 | 内存/估算 vs 真值 |

审查"长期(架构清理)"建议明确归类:**"Hypothesis 状态机与 refresh_stats
单一数据源(V-27.92/100/120)"**。

## 为什么不能在 bug-fix 轮修

`_process_hypothesis_feedback` 的**整个设计**是"用本轮内存 `pending_alphas`
+ 累积内存 `history` 驱动状态机"。要真修 = 让状态机改读 DB 真值,这牵动:

- `should_abandon_hypothesis(history_for_hid, hypothesis_id)` 的签名 —— 从
  "传内存 history" 改成 "读 DB / 接受 refresh_stats 产出"
- `refresh_stats` 要在 `should_abandon` 之前被调用并落库
- `state.round_history` / `history_so_far` 内存累积的存在意义要重新评估
  (保留作 cache?还是删掉?)
- attribution(`classify_attribution_llm`)的输入也部分来自内存计数

这是状态机的数据源重构,bug-fix 轮贸然改会让 hypothesis 生命周期(本就是
系统里最绕的子系统之一)更不可预测。

## 候选根治方案

### 方案 A — `should_abandon` 改读 DB(推荐)

1. `refresh_stats` 在每轮 `_process_hypothesis_feedback` 末尾先跑一次,把
   per-hypothesis 的 `alpha_count` / `pass_count` / per-round 明细落库
   (新增 `hypothesis_round_stats` 表,或扩展现有 hypothesis 行的 JSONB)。
2. `should_abandon_hypothesis` 改签名:接受 `hypothesis_id` + DB session,
   内部 `SELECT` 最近 N 轮的 DB 明细,不再吃内存 `history_for_hid`。
3. `state.round_history` 内存累积降级为纯展示 cache,不再是状态机的
   authoritative 输入。
4. V-27.71(flip-retry 虚高)随之解决 —— DB 明细按 alpha 真实归属计数,
   flip 产物 attribution 单独标。

**工作量**: ~3-4 day(migration + refresh_stats 扩展 + should_abandon
重构 + `_process_hypothesis_feedback` 改造 + 单测:worker-重启场景 /
跨-prefetch-round 场景 / abandon 触发场景)。

### 方案 B — 内存 history 持久化到 state 并跨 worker 传递(weaker)

把 `round_history` 做成 LangGraph 的 checkpointed 字段,worker 重启从
checkpoint 恢复。**缺点**:V-20.1 prefetch round 的独立 session 仍不共享;
治标不治本,只覆盖单 worker 重启场景。

## 当前临时缓解(已上线)

- **V-27.68**(commit `85fe5c6`):`should_abandon_hypothesis` 加 `alpha_count`
  显式守卫 —— 0-alpha round 不算失败 round。这减少了**误杀**(把没真正
  测试的 hypothesis abandon 掉),但**不解决**"内存 history 丢失 → 该
  abandon 的不 abandon"这个主方向。
- V-27.92 的"FAIL alpha 推不动状态机"主问题仍在。

## 建议

排期 **方案 A**,与同簇的 V-27.71 / V-27.100 / V-27.120 一起作为
"Hypothesis 状态机单一数据源" plan 项处理 —— 它们共用同一个修复
(状态机改读 DB 真值)。在落地前,V-27.68 的误杀缓解 + 把
`CASCADE_WATCHDOG` 之外的 worker 重启频率压低(稳定 worker)是低成本的
进一步缓解。
