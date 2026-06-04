# RCA — V-27.1 watchdog force-clear 锁 + 旧 worker 仍存活 → 双 cascade 并发

**日期**: 2026-05-14
**来源**: `docs/quality_review_mining_task_2026-05-14.md` V-27.1(阻断级)
**状态**: 根因确认,临时缓解已上线,根治方案待 plan 排期

---

## 现象

`session_watchdog._redispatch_task` 在 revive 一个 dead-but-RUNNING 的
`CONTINUOUS_CASCADE` task 前,无条件调用
`force_clear_cascade_lock(f"cascade_lock:task:{task.id}")` 删掉 Redis 锁,
然后派发新 worker。新 worker 成功 `acquire` → **两个 worker 同跑同一
cascade task**,正是 V-26.1 锁机制要防的根因。

旧 worker 后续 `finally: _release_lock` 因 Lua-CAS token 不匹配是 no-op,
无法自我纠正 —— 它会继续跑到自己的循环结束,期间和新 worker 并发烧
BRAIN slot、并发写同一 task 的 alpha / trace_step。

## 根因

判定链有一个**不可消除的 heuristic 缺口**:

1. watchdog 的死活判据是 `last_alpha_persisted_at < NOW() - DEAD_THRESHOLD_MIN`
   (默认 15 min)。
2. 但一个**活着的** worker 完全可能 15 min 不写 heartbeat:
   - 卡在长 BRAIN simulate(multi-sim 可达 5-10 min,V-20.1 pipeline 下
     2 round 串行排队更久);
   - 慢 LLM 调用(code_gen / hypothesis,偶发 30s-2min);
   - 一个 cascade phase 内连续多轮都没产出 PASS alpha →
     `last_alpha_persisted_at` 长时间不更新(V-26.3 之后 cascade 在
     round 边界更新 heartbeat,但 round 本身可能很长)。
3. watchdog 把"15 min 没 heartbeat"误判成"worker 死了",`force_clear`
   是**无条件 DELETE** —— 它不验证旧 worker 真的退出了,也无法验证
   (Celery `--pool=solo` 下没有可靠的 cross-process liveness 探针)。

V-26.5 的 `force_clear` 解决了"SIGKILL 死 worker 留下锁残留 10800s TTL
阻塞新 worker"这个真问题,但代价是引入了"误判活 worker → 删锁 → 双跑"
这个新回归。两个问题是**对称的两难**:
- 不 force_clear → SIGKILL 死 worker 的锁残留 3 小时,task 卡死;
- force_clear → 误判活 worker,双跑。

## 为什么不能在 bug-fix 轮里简单修

`_release_lock` 的 Lua-CAS(token 匹配才释放)是对的 —— 它防止 worker A
释放 worker B 的锁。问题不在锁原语,在**"何时可以安全删别人的锁"**这个
判定本身无解:watchdog 没有任何手段 100% 确认旧 worker 已死。

任何"改判据"(拉长 DEAD_THRESHOLD、加更多 heartbeat 点)只是把误判概率
往下压,不消除 race。真正的修复需要改**锁的所有权语义**,这是架构改动,
不属于 bug-fix 轮的范围。

## 候选根治方案

### 方案 A — 锁 takeover + 主循环所有权自检(推荐)

1. 锁 value 从单纯的 CAS token 扩展为 `{token, run_id, worker_pid,
   acquired_at}`。
2. watchdog revive 时不再 `force_clear` + 新 worker 重新 `acquire`,而是
   **原子 takeover**:Lua 脚本把锁 value 直接改写成新 worker 的 token
   (无论旧 token 是什么)。
3. cascade 主循环(`mining_tasks._run_cascade_phase`)在**每个 round 边界**
   增加一次 `_verify_lock_ownership()`:读锁 value,若 token 不再是自己的
   → 说明被 takeover 了 → 立即 `return`(优雅退出,不再调度下一 round)。
4. 旧 worker 卡在 BRAIN sim 里时,sim 是同步 await,无法立刻响应 —— 但
   sim 返回后的第一个 round 边界就会检测到失去所有权并退出。最坏情况:
   旧 worker 多跑**一个 round**(≤ 1 个 cascade round 的 BRAIN 配额),
   不再是"多跑到自己循环自然结束"。

**工作量**: ~1.5-2 day(redis_pool 锁原语扩展 + Lua takeover 脚本 +
mining_tasks 主循环 4 个 round 边界插 ownership check + 单测覆盖
takeover / 自检退出 / 旧 worker sim-in-flight 场景)。

### 方案 B — 不 force_clear,改 TTL 续租(weaker)

活 worker 每个 round 边界 `EXPIRE` 续租锁(比如 TTL=30min,round 间隔
远小于此)。死 worker 不续租,锁自然过期,watchdog 不需要 force_clear。

**缺点**: 仍有"锁过期窗口"内的 race;且续租失败(Redis 抖动)会让活
worker 锁意外过期。比方案 A 弱,但工作量小(~0.5 day)。

### 方案 C — 接受现状,仅靠概率缓解(最小)

保持 force_clear,仅靠 V-27.2 的 `_recently_revived` 把 watchdog **重复**
revive 的频率压到 dead-threshold 一次。**不修第一次误判**。

## 当前临时缓解(已上线,commit cd83d0f)

V-27.2 的 `_recently_revived`:watchdog 跳过"dead-threshold 窗口内已
revive 过"的 task。这把 watchdog 自身重复 revive 的频率从"每 5min tick"
降到"每 dead-threshold(15min)一次",**减少**了双跑发生的次数,但
**不能阻止第一次误判 force_clear** —— 因为第一次 revive 时没有"最近 revive
记录"。V-27.1 的核心 race 仍在。

## 建议

排期 **方案 A**。在落地前,V-27.2 缓解 + 把 `CASCADE_WATCHDOG_DEAD_MIN`
调大(15 → 25-30 min,留足长 sim / 慢 LLM 的余量)可作为低成本的进一步
概率缓解 —— 但必须明确:这是降低误判率,不是消除 race。
