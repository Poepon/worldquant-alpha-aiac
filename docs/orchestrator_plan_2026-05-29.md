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

## 5. 开工前必决问题(占位)

- Q1: orchestrator 跑在哪里 — celery beat / 独立 daemon / FastAPI 后台 task?
- Q2: 决策频率 — 事件驱动(task 终态变化触发)还是 cron 定时(每 N 分钟扫一次)?
- Q3: 是否引入 RL / bandit 选 task 参数(region/dataset/hypothesis)还是规则驱动?(参考已有 `dataset_value_bandit`)
- Q4: 多账号(USER + CONSULTANT)配额池怎么协调?
- Q5: 安全阈值 — orchestrator 最多同时开几个 task / 每日 launch 上限是多少?
- Q6: 用户 override — 手动开的 task 是否豁免 orchestrator 决策?
- Q7: 与现有 `watchdog_revive_dead_sessions` / `quota_guard_pause_at_threshold` 怎么协作?(谁先 fire)

---

## 6. 参考

- serial→pipeline 迁移 v3 §2.5(R14 推迟决策)
- 现有自动化清单:`backend/celery_app.py:celery_beat_schedule` (170-269)
- 现有 task 启动:`backend/services/task_service.py:start_flat_session` + `backend/routers/ops.py:/ops/start-flat-session`
- 现有 PAUSE 路径:`backend/tasks/session_watchdog.py:_quota_guard_async` + `services/task_stop_loss_service.py`
- 实证依据:本对话 2026-05-29 grep + Read
