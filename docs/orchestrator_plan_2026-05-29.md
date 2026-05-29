# Mining Orchestrator plan (占位)

- 日期: 2026-05-29
- 状态: **stub / 未开工**,待 [serial→pipeline 迁移](./serial_to_pipeline_migration_plan_2026-05-29.md) Phase C 完成后启动
- 起源: serial→pipeline 迁移 v3 的 R14 决策(§2.5)发现 — R14 + 无 orchestrator = 反目标(配额空转死结)。R14 真正价值依赖 orchestrator,所以打包到本 plan

---

## 1. 目标

让 mining 全链路在没有人手动启动 / 监控 / 重开 task 的前提下持续产出 PASS alpha 并自动 submit 候选。

当前自动化分布:

| 阶段 | 状态 |
|---|---|
| **下游 — submit** | ✅ 已有:积压抽干页(`/ops/submit-backlog` 2026-05-28)、IQC 边际推荐、self-corr 撞门标记 |
| **中游 — task 续命** | ✅ 已有:`watchdog_revive_dead_sessions`(5min revive RUNNING-stale)、heartbeat-abort(`PipelineHeartbeatExpired` → PAUSED → 重 dispatch fresh pool)、`quota_guard_pause_at_threshold`(10min,900 sim/天) |
| **上游 — task 启动 / 选择** | ❌ **完全手动** — `start_flat_session` 0 自动 caller |

orchestrator 补上 **上游 task 自动启动 / 让位 / 替换** 的空白。

---

## 2. 期望能力(草稿)

### 2.1 Auto-launch — 决定"什么时候开新 task,什么参数"

输入:
- 当前 RUNNING / PAUSED task 池
- 当日剩余配额(`BRAIN_DAILY_SIMULATE_LIMIT - today_count`)
- 历史 PASS rate per (region, dataset, hypothesis)
- 当前 submit 积压 (per region)
- 时区 / BRAIN 维护窗口

输出:
- 0 或 N 个 `start_flat_session(region, universe, datasets, delay, enable_pipeline=True)` 调用
- 决策日志(为什么开 / 为什么不开)

### 2.2 让位策略 — 决定"PAUSED/COMPLETED task 怎么接续"

触发源:
- task COMPLETED(自然 max_iters / daily_goal 达成)
- task PAUSED(R14 触发 / quota_guard 触发 / 手动)
- task STOPPED(用户终止)

决策:
- 同 (region, dataset) 重开?(若 PAUSED 是 transient 原因)
- 换 dataset 同 region?(dataset 走完了)
- 换 region?(region 配额耗尽 / 全 dataset 走完)
- 换 hypothesis variant?(当前 hypothesis 池跑烂)
- 不开新 task,等明天配额 reset?

### 2.3 R14 子模块 — "判定当前 task 应该让位"

承接 serial→pipeline 迁移 v3 §2.5。**R14 在 orchestrator 内才有意义** — PAUSE 事件由 orchestrator 接住开新 task。

R14 触发节奏选项(从 serial→pipeline plan v3 搬过来):
- A. per-dataset:`next_round_inputs` 切 dataset 时算上一个 dataset 的 PASS/total(推荐,与串行 R14 设计意图最近)
- B. per-N-candidates:persister 每 N 个 candidate 调一次
- C. per-time-window:每 T 秒一次 batch

实现注入点:重命名后的 `_run_flat_iteration`(参见 [serial→pipeline plan v3 Phase C](./serial_to_pipeline_migration_plan_2026-05-29.md))内 producer 的 dataset 切换边界(`next_round_inputs` 闭包)。**orchestrator 开工时 `_run_flat_iteration_pipeline` 已被重命名,本 plan 不写具体行号避免 stale**。

EMA 状态 `task.config[stop_loss_state]` 复用串行同 key(`task_stop_loss_service.py:119` `check_should_pause` / `:220` `apply_stop_loss_decision`)。

### 2.4 配额调度

- **sim slot 上限是账号角色的函数**:USER 3 slot / CONSULTANT 80 slot(`BrainAdapter._current_sim_slot_limit()` 实时返回),不是硬数字。`ENABLE_BRAIN_CONSULTANT_MODE` flip 会改变实际可用并发,orchestrator 必须读 effective 值不能缓存
- 给"高潜力" task(high recent PASS rate)优先 sim slot
- 给"探索新维度" task(新 dataset / 新 region)留 X% 配额(避免被高产 task 饿死)
- 跨日 reset 策略(`quota_guard` 当天兜底,次日 UTC 0:00 后自然恢复)
- 多账号(USER + CONSULTANT 升级窗口期)slot 池协调:见 Q4

### 2.5 错误回放 / dispatch backoff (防 orchestrator 自烧)

- **launch 失败 N 次同参数 task → backoff**(否则配置错的 task 会被 orchestrator 循环重 launch,烧光 BRAIN auth 配额 / DB 连接)
- **launch 成功后 task 在 ≤ 5min 内挂**(配置错 / dataset 不存在 / BRAIN 拒) → 与正常长跑 task 分桶记录,**不算"已让位"** — 避免短命 task 被误判为已完成的让位事件
- 单日 launch 上限(防 orchestrator bug 烧光配额) — 见 Q5
- 与 `watchdog_revive_dead_sessions` 协作:revive 路径不经 orchestrator,RUNNING-stale 复活后 task.config 已稳,orchestrator 不重复 launch

### 2.6 与 submit 端衔接

- mining 高 PASS 高 margin → 触发 submit 端积压抽干(已有,`/ops/submit-backlog`)
- submit 积压过载(> N 条 SUBMIT 候选未处理) → 暂停 launch 新 mining task,等抽干
- submit pass-rate 反馈 → 调整 launch 策略(哪个 region/dataset 历史 submit 成功率高)

---

## 3. 不在本 plan 范围

- 流水线内部设计(已 ship)
- 串行→流水线迁移(独立 plan)
- BRAIN 角色切换(USER↔CONSULTANT,已有 P3-Brain 机制)
- 优化闭环(独立 plan `optimization_closure_plan_v1_2026-05-28.md`)

---

## 4. 依赖 / 前置

- [ ] serial→pipeline 迁移 Phase C 完成(流水线作为唯一执行路径)
- [ ] 流水线 ≥48h soak 验证(B.3 通过) — orchestrator 启动的 task 必须能跑得稳
- [ ] heartbeat-abort 实战验证 ≥1 周无 false-positive
- [ ] task 终态语义清晰化:COMPLETED / PAUSED(R14 vs quota_guard vs manual) / STOPPED / EARLY_STOPPED 各对应什么 orchestrator 行为

---

## 5. 开工前必决问题 + 决策(2026-05-29 晚)

> **状态:Q1-Q7 全部 DECIDED 2026-05-29 晚** — Q1/Q3/Q4/Q5/Q6/Q7 采纳推荐,**Q2 偏离推荐改事件驱动**(post-finalize hook + cron fallback)。

### Q1: orchestrator 跑在哪里 — celery beat / 独立 daemon / FastAPI 后台 task?

**[DECIDED] celery beat**(轻量 cron + 接事件 task,与现有 maintenance task 同栈)
- ✅ 已是项目 scheduler 入口,`watchdog_revive_dead_sessions` / `quota_guard_pause_at_threshold` 在此 — 同样的"扫描决策"性质
- ✅ 不增运维负担(无新进程/端口)
- ✅ Windows `--pool=solo` 不阻碍(orchestrator 决策 <30s,不抢 mining-worker)
- ❌ celery beat 单线程 — 但决策本身轻量,不构成瓶颈

**备选**:独立 daemon(更隔离但增加运维) / FastAPI 后台 task(生命周期绑服务不稳)

### Q2: 决策频率 — 事件驱动 vs cron 定时?

**[DECIDED] 事件驱动 + cron 1h fallback**(用户偏离推荐 cron)
- ✅ 精度高:task 终态变化立即评估,不等 10min tick
- ✅ post-finalize hook 接在 `_run_flat_iteration` finalize 末尾,投 celery task `orchestrator_evaluate_after_finalize.delay(task_id)`
- ✅ idempotency:消费端检查 `task.config["launched_by"]` 防重复触发
- ✅ cron 1h fallback 兜底防丢事件(投递失败/worker 重启吞事件)
- ⚠️ 实现成本 +0.5-1d(hook 接线 + 事件投递 + 防重)

**未采纳**(原推荐 cron 10min :05 偏移):工程量小但延迟 10min,被用户判定为产能瓶颈

### Q3: RL/bandit 选参数还是规则驱动?

**[DECIDED] Phase 1 规则驱动**(经验阈值 + 历史 PASS rate 加权),**Phase 2 升 bandit**
- ✅ RL/bandit 冷启动慢(需累积 mining → submit pass-rate 全链路反馈)
- ✅ 规则可立即落地,可观察
- ✅ 参考已有 `dataset_value_bandit`(Beta-Bernoulli),Phase 2 复用 sampling 框架
- Phase 1 规则示例:历史 PASS rate top-3 region/dataset 加权采样,每周 EMA 半衰期

**备选**:直接上 bandit(冷启慢,3-7 天产能损失)

### Q4: 多账号配额池?

**[DECIDED] 不管多账号,读 `BrainAdapter._current_sim_slot_limit()` 即可**
- ✅ 项目当前不是真多账号 — USER (3 slot) vs CONSULTANT (80 slot) 是同账号 role 切换,通过 `ENABLE_BRAIN_CONSULTANT_MODE` flag
- ✅ `_current_sim_slot_limit()` 实时返回 effective 值(不可缓存,见 §2.4)
- ✅ Consultant 升级后自动扩容,不需 orchestrator 改逻辑
- 真多账号需求来时(Phase 3?)再设计 account-aware 池

**备选**:预先建多账号池(过早工程化,YAGNI)

### Q5: 安全阈值(防 orchestrator 自烧)

**[DECIDED] 保守起步**:
- `max RUNNING task` = **3**(慢起,Phase B 观察 PASS rate 后调)
- 每日 launch 上限 = **10**(单 task 400 sim 上限,10 × 400 = 4000 sim,远超 quota_guard 阈值 900 → quota_guard 兜底)
- 单参数 launch 连续失败 **N=3** → backoff 2h
- launch 后 task 在 **≤5min** 内 COMPLETED + total_alphas=0 → 标"短命"不算让位(见 §2.5)

**备选**:激进阈值(max 8 / daily 20)— Phase 1 不推荐,无 observ 数据

### Q6: 用户 override?

**[DECIDED] manual_override 标记 + orchestrator 跳过非自己启的 task**
- ✅ 用户手动 `POST /ops/start-flat-session` 写 `task.config["launched_by"]="manual"`
- ✅ orchestrator 启的 task 写 `task.config["launched_by"]="orchestrator"`
- ✅ orchestrator 让位决策只对自己启的 task 生效(自己不动 user 的)
- ✅ user 仍可手动 PAUSE/STOP/RESUME 任何 task
- 缺省值 "manual"(向后兼容历史 task)

**备选**:orchestrator 接管全部 task(过度自动化,违反用户主权)

### Q7: 与现有 watchdog/quota_guard 协作?

**[DECIDED] 事件驱动 + cron 1h fallback**(配合 Q2 决策)
- **主路径(事件)**:`_run_flat_iteration` finalize 末尾 → celery `orchestrator_evaluate_after_finalize.delay(task_id)` → orchestrator task 内读当前 RUNNING/PAUSED pool + 配额状态 + 历史 PASS rate → 决定是否 launch 下一个
- **fallback cron**:每 1h 全状态扫描 schedule(防丢事件 / worker 重启吞事件 / 边界 case)
- 已有 cron 保持不变:
  - `quota_guard_pause_at_threshold` @ :00/:10/...(不动)
  - `watchdog_revive_dead_sessions` @ :00/:05/...(不动)
- 协作语义:
  - `quota_guard` PAUSE → 事件路径读 quota_guard 当日累计,不重 launch
  - `watchdog_revive` 复活 RUNNING-stale → 触发不了 finalize event,不与 orchestrator 冲突
  - orchestrator launch task 5min 内挂 → 短命标记,不让位

**未采纳**:cron-only 错峰(用户偏离 Q2 推荐,选事件驱动)

---

## 6. 决策汇总表(2026-05-29 晚 DECIDED)

| Q | 决策 | 实施工作量 |
|---|---|---|
| Q1 | celery beat | 0.2d(`backend/tasks/orchestrator.py` + beat fallback schedule) |
| Q2 | **事件驱动 + cron 1h fallback**(偏离 cron 推荐) | **+1d**(`_run_flat_iteration` finalize post-hook + `orchestrator_evaluate_after_finalize` celery task + 防重 idempotency) |
| Q3 | Phase 1 规则,Phase 2 bandit | 0.5d 规则 + EMA;Phase 2 单独 plan |
| Q4 | 读 `_current_sim_slot_limit()` | 0d(已有 API) |
| Q5 | max 3 / daily 10 / backoff 2h / 短命 5min | 0.3d 实现 + 配置 |
| Q6 | `task.config["launched_by"]` | 0.2d(新 field + start_flat_session 默认 manual) |
| Q7 | 事件驱动 + cron 1h fallback | 包含在 Q2 |

**Phase 1 总工作量**:**~2.2d**(原 cron 估算 1.2d,Q2 改事件驱动 +1d),前置依赖见 §4。

---

## 7. Phase 1 实施 sub-phase(依据 Q1-Q7 决策)

1. **Sub-phase 1**(0.4d)✅ **SHIPPED 2026-05-29** — `backend/tasks/orchestrator.py`(2 个 stub celery task,flag OFF short-circuit)+ `task.config["launched_by"]` schema field + `start_flat_session(launched_by="manual")` 默认参数 + `ENABLE_AUTO_ORCHESTRATOR` flag default OFF + beat schedule `orchestrator-periodic-scan` 1h + 8 单测全 PASS
2. **Sub-phase 2**(1d):事件路径 — `_run_flat_iteration` finalize 末尾 schedule `orchestrator_evaluate_after_finalize.delay(task_id)` + 消费端读 task pool + 配额 + 决策规则
3. **Sub-phase 3**(0.5d):规则引擎 — 历史 PASS rate EMA(7d window)+ region/dataset 加权采样 + Q5 阈值
4. **Sub-phase 4**(0.2d):cron 1h fallback + 安全阈值 + 监控 endpoint `/ops/orchestrator/status`
5. **Sub-phase 5**(0.1d):测试 + plan v3 同步

---

## 8. 参考

- serial→pipeline 迁移 v3 §2.5(R14 推迟决策)
- 现有自动化清单:`backend/celery_app.py:celery_beat_schedule` (170-269)
- 现有 task 启动:`backend/services/task_service.py:start_flat_session` + `backend/routers/ops.py:/ops/start-flat-session`
- 现有 PAUSE 路径:`backend/tasks/session_watchdog.py:_quota_guard_async` + `services/task_stop_loss_service.py`
- 实证依据:本对话 2026-05-29 grep + Read
