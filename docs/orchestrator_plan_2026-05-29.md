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

## 5. 开工前必决问题 + v0 推荐(2026-05-29 晚)

> 状态:草稿推荐 / 等用户审批。每个 Q 给推荐 + 理由 + 备选。

### Q1: orchestrator 跑在哪里 — celery beat / 独立 daemon / FastAPI 后台 task?

**推荐:celery beat**(轻量 cron 任务,与现有 maintenance task 同栈)
- ✅ 已是项目 scheduler 入口,`watchdog_revive_dead_sessions` / `quota_guard_pause_at_threshold` 在此 — 同样的"扫描决策"性质
- ✅ 不增运维负担(无新进程/端口)
- ✅ Windows `--pool=solo` 不阻碍(orchestrator 决策 <30s,不抢 mining-worker)
- ❌ celery beat 单线程 — 但决策本身轻量,不构成瓶颈

**备选**:独立 daemon(更隔离但增加运维) / FastAPI 后台 task(生命周期绑服务不稳)

### Q2: 决策频率 — 事件驱动 vs cron 定时?

**推荐:cron 每 10 分钟**(与 `quota_guard` 同步,但偏移 5 分钟)
- ✅ mining task 自然时长是小时级,10min 决策延迟可忽略
- ✅ 事件驱动需要 post-finalize hook + 事件 bus,实现复杂
- ✅ cron 容易观察(每 tick 一条 log)
- 偏移 5 min:`quota_guard` 在 :00/:10/:20...,orchestrator 在 :05/:15/:25...,读 quota_guard 最新状态

**备选**:事件驱动(精度高但工程量大,推迟到 Phase 2)

### Q3: RL/bandit 选参数还是规则驱动?

**推荐:Phase 1 规则驱动**(经验阈值 + 历史 PASS rate 加权),**Phase 2 升 bandit**
- ✅ RL/bandit 冷启动慢(需累积 mining → submit pass-rate 全链路反馈)
- ✅ 规则可立即落地,可观察
- ✅ 参考已有 `dataset_value_bandit`(Beta-Bernoulli),Phase 2 复用 sampling 框架
- Phase 1 规则示例:历史 PASS rate top-3 region/dataset 加权采样,每周 EMA 半衰期

**备选**:直接上 bandit(冷启慢,3-7 天产能损失)

### Q4: 多账号配额池?

**推荐:不管多账号,读 `BrainAdapter._current_sim_slot_limit()` 即可**
- ✅ 项目当前不是真多账号 — USER (3 slot) vs CONSULTANT (80 slot) 是同账号 role 切换,通过 `ENABLE_BRAIN_CONSULTANT_MODE` flag
- ✅ `_current_sim_slot_limit()` 实时返回 effective 值(不可缓存,见 §2.4)
- ✅ Consultant 升级后自动扩容,不需 orchestrator 改逻辑
- 真多账号需求来时(Phase 3?)再设计 account-aware 池

**备选**:预先建多账号池(过早工程化,YAGNI)

### Q5: 安全阈值(防 orchestrator 自烧)

**推荐保守起步**:
- `max RUNNING task` = **3**(慢起,Phase B 观察 PASS rate 后调)
- 每日 launch 上限 = **10**(单 task 400 sim 上限,10 × 400 = 4000 sim,远超 quota_guard 阈值 900 → quota_guard 兜底)
- 单参数 launch 连续失败 **N=3** → backoff 2h
- launch 后 task 在 **≤5min** 内 COMPLETED + total_alphas=0 → 标"短命"不算让位(见 §2.5)

**备选**:激进阈值(max 8 / daily 20)— Phase 1 不推荐,无 observ 数据

### Q6: 用户 override?

**推荐:manual_override 标记 + orchestrator 跳过非自己启的 task**
- ✅ 用户手动 `POST /ops/start-flat-session` 写 `task.config["launched_by"]="manual"`
- ✅ orchestrator 启的 task 写 `task.config["launched_by"]="orchestrator"`
- ✅ orchestrator 让位决策只对自己启的 task 生效(自己不动 user 的)
- ✅ user 仍可手动 PAUSE/STOP/RESUME 任何 task
- 缺省值 "manual"(向后兼容历史 task)

**备选**:orchestrator 接管全部 task(过度自动化,违反用户主权)

### Q7: 与现有 watchdog/quota_guard 协作?

**推荐 cron schedule 错峰**:
- `quota_guard_pause_at_threshold` @ :00/:10/:20...(已存在,不改)
- `watchdog_revive_dead_sessions` @ :00/:05/:10...(已存在,5min 间隔)
- **orchestrator @ :05/:15/:25...**(新增,10min 间隔,偏移 5min 让 quota_guard 先跑)
- 协作语义:
  - `quota_guard` PAUSE 配额超阈值的 task → orchestrator 不会立即重新 launch(读 quota_guard 当日累计)
  - `watchdog_revive` 复活 RUNNING-stale → orchestrator 不冲突(它启新 task,不动 stale)
  - orchestrator launch task 后 5min 内挂(配置错)→ 标短命,不让位

**备选**:同时 fire(无意义争锁)/ orchestrator 早于 quota_guard(读不到当日最新累计)

---

## 6. 决策汇总表(供审批)

| Q | 推荐 | 实施工作量 |
|---|---|---|
| Q1 | celery beat | 0.2d(新增 beat schedule + task module) |
| Q2 | cron 10min(:05 偏移) | 包含在 Q1 |
| Q3 | Phase 1 规则,Phase 2 bandit | Phase 1 = 0.5d 规则 + EMA;Phase 2 单独 plan |
| Q4 | 读 `_current_sim_slot_limit()` 即可 | 0d(已有 API) |
| Q5 | max 3 / daily 10 / backoff 2h / 短命 5min | 0.3d 实现 + 配置 |
| Q6 | `task.config["launched_by"]` | 0.2d(新 field + start_flat_session 默认 manual) |
| Q7 | :05 偏移 cron + 状态读 | 包含在 Q1 |

**Phase 1 总工作量**:~1.2d(规则驱动 + Q4-Q7 工程接线),前置依赖见 §4。

---

## 6. 参考

- serial→pipeline 迁移 v3 §2.5(R14 推迟决策)
- 现有自动化清单:`backend/celery_app.py:celery_beat_schedule` (170-269)
- 现有 task 启动:`backend/services/task_service.py:start_flat_session` + `backend/routers/ops.py:/ops/start-flat-session`
- 现有 PAUSE 路径:`backend/tasks/session_watchdog.py:_quota_guard_async` + `services/task_stop_loss_service.py`
- 实证依据:本对话 2026-05-29 grep + Read
