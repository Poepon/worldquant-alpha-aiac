# 自动提交 alpha 到 BRAIN — 设计与实现(as-built, 2026-06-04)

> 状态:已实现并通过单测/回归;**默认全关 + 默认 shadow**。flip 到 live 前按 §5 走影子观察期。
> 关联:`docs/DEVELOPMENT_PLAN.md` N1 · 战略基调 execution-limited(抽干提交积压)。

## 0. 定位

系统是 **execution-limited**:67+ clean alpha 积压、史上仅 ~12 提交。本功能把"人工逐个点 `GET /ops/submit-backlog/drain-order` → 逐个 `POST /alphas/{id}/submit`"自动化。提交**不可逆 + 烧 BRAIN 配额**,故唯一可接受的设计 = **fail-closed 守门栈 + 影子先行 + 极保守日上限**。最终不可逆动作仍交 `AlphaService.submit_alpha`(它自带 can_submit / live self_corr<0.7 / Redis 锁 / 锁内重查),本层是**更严的预筛,不是绕过**。

## 1. 守门栈(全过才提交,任一缺值/stale/异常 → 不提交)

| 序 | 门 | 判据 | 实现 |
|---|---|---|---|
| G0 | 主开关 | `ENABLE_AUTO_SUBMIT=True` 且 `AUTO_SUBMIT_MODE!='off'` | `auto_submit_tasks._run` |
| G1 | recon kill-switch(region 级) | 实时 `sign_agreement_stats` verdict 满足 `AUTO_SUBMIT_REQUIRE_RECON_VERDICT`(Stage1='supported');否则整 region 停手 | `_run_region` + `marginal_recon.route_on_sign_verdict` |
| G2 | 候选 SQL(收紧) | can_submit IS TRUE / date_submitted IS NULL / region / **is_margin NOT NULL 且 ≥floor** / **self_corr NOT NULL 且 <thr**(无 NULL 放行分支) | `compute_auto_submit_candidates` |
| G3 | BRAIN 形式合规 | can_submit IS True(SQL 已保证) | 同上 |
| G4 | can_submit 新鲜度 | `metrics_snapshot_at` 距今 ≤ `AUTO_SUBMIT_CANSUBMIT_MAX_AGE_H`(12h);未知/超期 → fail(`AUTO_SUBMIT_REQUIRE_FRESH_CANSUBMIT`) | `evaluate_guard_stack` |
| G5 | BRAIN 红线 | is_sharpe/is_fitness/is_turnover 满足 `eval_thresholds(delay)`(delay-0 用严档);任一 NULL → fail | 同上 |
| G6 | 经济门 | is_margin ≥ `AUTO_SUBMIT_MARGIN_BPS_MIN`/10000(5bps);<0 硬拒 | 同上 |
| G7 | 边际推荐 | `_iqc_marginal.recommendation=='SUBMIT'` 且 composite>0 | 同上 |
| G8 | sign value tier | `sign_routing_ok` 且 value_tier==0(additive) | 同上 |
| G9 | 正交排序 | 落 `greedy_orthogonal_order` 的 ordered(非 blocked)且 max_corr_to_selected<thr | 同上 |
| G10 | 不可逆门兜底 | `submit_alpha` 自带:alpha_id / date_submitted 锁内重查 / can_submit / region / live self_corr<0.7 / (Consultant)PROD-corr | `alpha_service.submit_alpha` |

## 2. 安全机制

- **主 kill-switch 默认 OFF**(`ENABLE_AUTO_SUBMIT=False`),`ENABLE_` 前缀可运行时热刷。
- **影子模式**(`AUTO_SUBMIT_MODE='shadow'`,默认):跑完整守门栈 + 把 would_submit/skipped 全量落审计表 `auto_submit_audit`,**绝不调 submit_alpha**。
- **保守日上限**(`AUTO_SUBMIT_DAILY_CAP=4`,每 UTC 日)+ **per-run cap**(`=2`)。
- **原子 incr-先占位**(审查修复 H1/H2):live 提交前先 `redis.incr` 占位,占位在不可逆动作之前且原子 → 并发 beat 不会双双过闸、提交后 incr 不会丢;未提交(超额/rejected/error)则 `decr` 回退,rejection 不吃配额。
- **beat 单飞锁**:NX 锁值=beat_run_id,**CAS 释放**(Lua check-and-del,审查修复 M1),TTL 30min。
- **Redis 不可用**:live 自动降级 shadow(无法计数则绝不超额提交)。
- **全程 fail-closed**:任一信号缺值/异常 → 跳过该 alpha + 记审计,绝不"默认放行"。
- **全审计落库**:每候选(would_submit/submitted/rejected/skipped/error)记 gate_results(各信号原值 + 逐 gate pass/fail)。

## 3. 复核入口

`GET /ops/auto-submit/audit?outcome=would_submit` —— 影子期看"本来会提交"的名单 + 每条的信号值/gate 结果 + 近 24h outcome 统计。

## 4. 文件

- `backend/config.py` — AUTO_SUBMIT_* 旋钮
- `backend/auto_submit_selector.py` — 候选选择 + 守门栈(纯函数,单测覆盖)
- `backend/tasks/auto_submit_tasks.py` — beat
- `backend/routers/ops.py` — `GET /ops/auto-submit/audit`
- `backend/models/alpha.py` `AutoSubmitAudit` + 迁移 `m4a9c7e2b1f8_auto_submit_audit.py`
- `backend/celery_app.py` — beat 注册(每 6h :35)
- `backend/tests/unit/test_auto_submit.py` — 35 测

## 5. 分阶段上线

- **Stage 0 影子(≥7天)**:`ENABLE_AUTO_SUBMIT=True`(mode 仍 shadow)。看名单确认 0 垃圾。⚠️ 先确认 `metrics_snapshot_at` 在刷新(否则 G4 挡光 → would_submit 恒 0)。
- **Stage 1 live(≥14天)**:`AUTO_SUBMIT_MODE='live'`,daily_cap=4 / per_run_cap=2,G1 只 supported / G8 只 additive。盯 submitted/rejected,要求 BRAIN 接受率 100% + 0 起垃圾事故。
- **Stage 2**:放宽到 value_tier=neutral、G1 纳 weak、多 region(逐 region 跑)。**永不放宽**:BRAIN 红线 / margin≥5bps / 相关性 blocked 排除 / dilutive 绝不提 / FALSIFIED 整 region 停手。

## 6. 残余风险(无新门可堵,靠影子期 + 保守 cap + 人工抽查缓释)

(a) BRAIN before-and-after 对已提交返 400 → composite/ΔSharpe 是回测合并估计非活体;(b) 真 OOS 需数月 post-submit PnL;(c) Redis 全宕的无锁双发(BRAIN 400 兜底);(d) 非 sweep 来源、红线达标但过拟合的 IS alpha。

## 7. 运维上线 Runbook

> 默认全关,以下任一步前系统行为不变。配置项可写 `.env` 或运行时 flag override(`ENABLE_AUTO_SUBMIT` 以 `ENABLE_` 开头,可热刷)。

### 7.1 一次性:迁移 + 重启

```bash
# 1) 应用迁移(纯加 auto_submit_audit 表,additive,has_table 守卫,零风险)
cd backend && alembic upgrade head        # head 应为 m4a9c7e2b1f8

# 2) 重启 worker + beat 加载新代码(默认全关,重启后行为不变)
run.bat --restart                          # 或你的常规重启方式
```

### 7.2 Stage 0 — 影子模式(≥7 天,0 真提交)

经 ops flag 热翻(两个都要开;均 `ENABLE_` 前缀、已注册 SUPPORTED_FLAGS):
```
PATCH /ops/flags/ENABLE_AUTO_SUBMIT       {"value": true}   # mode 默认 shadow
PATCH /ops/flags/ENABLE_CAN_SUBMIT_REFRESH {"value": true}  # 保 G4 新鲜度可持续(必开)
```
- 每 6h(:35)auto-submit beat 跑完整守门栈,把 would-submit / skipped 全量写 `auto_submit_audit`,**不调 submit_alpha**。每 6h(:50)can_submit 刷新 beat 重验 backlog。
- 复核:`GET /ops/auto-submit/audit?outcome=would_submit`(看名单 + 每条 gate_results 信号值);`?outcome=skipped` 看被哪个 gate 挡下。
- **进入 Stage 1 判据**:连续 ≥7 天人工核 would-submit 名单、确认 0 垃圾;recon verdict 在该 region 稳定 supported。
- **排障**:would-submit 恒空 → 多半 **G4 新鲜度**(`_brain_can_submit_at` > 12h)。**这正是为何 `ENABLE_CAN_SUBMIT_REFRESH` 必须同时开**:它每 6h 重盖 can_submit 验证戳。若仍空,确认该 beat 在跑(`GET /ops/auto-submit/audit?outcome=skipped` 看 skip_reason 是否 G4_freshness 占多),或手动跑一次 `POST /alphas/refresh-can-submit`。注:`_brain_can_submit_at`(can_submit 验证新鲜度)≠ `metrics_snapshot_at`(完整 metrics 同步)。

### 7.3 Stage 1 — live(≥14 天,极保守)

```
.env:  AUTO_SUBMIT_MODE=live
       AUTO_SUBMIT_DAILY_CAP=4          # 每 UTC 日上限(你定)
       AUTO_SUBMIT_PER_RUN_CAP=2        # 单次 beat 上限
       AUTO_SUBMIT_REQUIRE_RECON_VERDICT=supported
```
- 行为:每次 beat 最多提交 `per_run_cap` 个、每日最多 `daily_cap` 个、最正交/additive/supported 的顶档;其余仍进人工 drain 队列。
- 监控:`GET /ops/auto-submit/audit?outcome=submitted`(已提交)/ `?outcome=rejected`(BRAIN 拒,会自动回退当日配额计数)。
- 紧急停:`ENABLE_AUTO_SUBMIT=False`(整功能停)或 `AUTO_SUBMIT_MODE=shadow`(退回只观察)。
- **进入 Stage 2 判据**:≥14 天 0 起垃圾事故 + BRAIN 接受率 100%。

### 7.4 Stage 2 — 放宽(可选)

```
AUTO_SUBMIT_DAILY_CAP=  (上调)
AUTO_SUBMIT_REQUIRE_RECON_VERDICT=weak   # 纳入 weak(>60% 但 <70%,≥15 对)
AUTO_SUBMIT_REGIONS=USA,...              # 多 region(逐 region 跑保 self_corr 同区)
```
**永不放宽**:G5 BRAIN 红线 / G6 margin≥5bps / G9 相关性 blocked 排除 / dilutive(value_tier≠0)绝不提 / FALSIFIED 整 region 停手。

### 7.5 配置项速查

| 旋钮 | 默认 | 作用 |
|---|---|---|
| `ENABLE_AUTO_SUBMIT` | False | 主开关 |
| `AUTO_SUBMIT_MODE` | shadow | off / shadow / live |
| `AUTO_SUBMIT_DAILY_CAP` | 4 | 每 UTC 日 live 提交上限 |
| `AUTO_SUBMIT_PER_RUN_CAP` | 2 | 单 beat live 提交上限 |
| `AUTO_SUBMIT_MARGIN_BPS_MIN` | 5.0 | 经济门 bps |
| `AUTO_SUBMIT_CANSUBMIT_MAX_AGE_H` | 12 | can_submit 新鲜度窗口 |
| `AUTO_SUBMIT_REQUIRE_FRESH_CANSUBMIT` | True | 新鲜度未知/超期 → 不提交 |
| `AUTO_SUBMIT_REGIONS` | USA | CSV,逐 region 跑 |
| `AUTO_SUBMIT_REQUIRE_RECON_VERDICT` | supported | Stage1 仅 supported;weak 放宽 |
| `AUTO_SUBMIT_CORR_THRESHOLD` | 0.7 | self/among-set 相关上限 |
| `ENABLE_CAN_SUBMIT_REFRESH` | False | 周期 can_submit 刷新 beat 开关(保 G4 新鲜度;auto-submit live 前必开) |
| `CAN_SUBMIT_REFRESH_MAX_PER_RUN` | 200 | 每次刷新 beat 的 BRAIN GET 上限(最旧戳优先) |

> **G4 新鲜度的可持续性**:G4 keying off `metrics._brain_can_submit_at`(由 `refresh_can_submit` 盖戳)。`run_can_submit_refresh` beat(每 6h :50,gated by `ENABLE_CAN_SUBMIT_REFRESH`)对 backlog 重验 → 戳保持 <12h → G4 持续可满足;同时把 BRAIN 现已拒绝的 alpha 降级出 backlog。**不开此 beat,G4 戳会 ~12h 后过期,would-submit 掉回 0。**
