# 设计稿:Backlog 候选「当前数据 re-sim / 衰减检查」前端功能(2026-06-08)

> 状态:**草案,待对抗审查**。源于「在线 re-sim 是关键缺口」(`reference_wq_consultant_compensation_model`)+ 手动实证 QPE25Yer 当前数据 1.35→1.34(`reference_brain_simulate_dedup_existing_alpha`)。

## 0. 目标 / 非目标

- **目标**:在 `SubmitBacklogMonitor` 页,人工按需把一个(或一批 REVIEW)backlog 候选**在当前数据上 re-sim**,对比冻结-IS baseline,判「持平 / 软衰减 / 硬衰减」。补体检卡「冻结 IS 子周期」的口径短板。
- **只读**:仅 simulate,**绝不 submit**(守 worldquant-brain skill 只读 + auto-submit 独立栈)。
- **非目标**:不挖矿、不抽干 backlog(§6 NO-GO 不变);不写 `alpha.metrics`(结果会随 regime 漂,按需算);不替代 regime 监测器(那是 cohort 每日告警,这是 per-candidate 人工复核)。

## 1. 范围(用户定:两者都做)

- **A. 单候选按需**:drain 表每行「当前数据」按钮 → 跑 1 个(~3min,1 槽)→ 行内出 tag。
- **B. 批量复核 REVIEW**:表上方「复核 REVIEW 短名单(N)」按钮 → 跑当前所有 REVIEW(7-11 个)→ 分块并发(chunk=槽上限,复用 regime `cc3b6f1` 分块)→ 进度 + 回填各行 tag。

## 2. 后端

### 2.1 执行模型(异步 celery + 轮询)
- 单 sim ~3min → 同步 HTTP 阻塞不可接受 → **celery 任务 + Redis 结果 + 轮询端点**(复用 regime beat 同款 async 路径)。
- `POST /ops/submit-backlog/resim-current` body `{alpha_pks: int[]}` → 入队 celery `resim_backlog_current` → 返 `{job_id}`(Redis NX 防并发重入,同 manual-optimize)。
- `GET /ops/submit-backlog/resim-current/{job_id}` → `{status, done, total, results:[{alpha_pk, brain_id, baseline, resim, verdict, perturb_used, margin, turnover}]}`。

### 2.2 celery 任务 `resim_backlog_current(alpha_pks)`
1. 从 `alphas` 取 expression + 结构设置 + baseline(metrics->>sharpe/fitness/turnover/margin)。
2. **分块 re-sim**(chunk=`BRAIN_SIM_SLOT_LIMIT_USER`,复用 regime 分块 + `make_variant` + `BrainSimulator`)。
3. **dedup 绕过(关键)**:先用存储设置原样跑;若 resim≈baseline(|Δ|≤stale_eps=陈旧/缓存)→ **微扰 `decay+1` 重试,仍 stale 再 `decay+2`**;**仅当 turnover ≤ 阈值(慢信号,默认 0.3)才微扰**(decay 改对慢信号经济近等价;高换手快信号微扰会失真 → 标 `verdict="dedup_unmeasurable"` 不硬扰)。
4. **verdict**:`held`(resim≥1.25)/`soft_decay`(0.7≤resim<1.25)/`hard_decay`(resim<0.7)/`dedup_unmeasurable`。口径仍 = **当前数据 IS**(非 OS,见 [[reference_brain_os_hidden_is_only]])。
5. 结果写 Redis(job_id key,TTL 24h),**不写 alpha.metrics**。

### 2.3 复用 / 不碰
- 复用:`regime_monitor.make_variant`、`BrainSimulator`(分块)、slot 机制、`_iqc_marginal`/体检卡不变。
- 不碰:auto-submit 栈、优化闭环、drain-order 排序逻辑(只在 item 上加可选 `resim_current` 字段供前端 join,或前端独立 store)。

## 3. 前端(SubmitBacklogMonitor.jsx)
- 单候选:drain 表加列/按钮「当前数据」→ POST 单 pk → 轮询 job → 行内 tag `1.35→1.34 持平`(绿)/`1.7→0.63 衰减`(红)/`dedup 无法测`(灰)。
- 批量:表上方按钮「复核 REVIEW(N)」→ POST 全 REVIEW pks → 进度条 → 回填。
- 诚实标注:tag tooltip 注明「当前数据 IS,非 OS;微扰 decay=K 强制 fresh」+ 时间戳。

## 4. 待审查的设计决策(请多镜头证伪)
- **D1 dedup 绕过的经济有效性**:decay 微扰对慢信号是否真经济近等价?turnover≤0.3 阈值是否合理?快信号标「无法测」是否漏判?有没有更稳的 force-fresh(改 expression?)?
- **D2 verdict 阈值**:held/soft/hard 用 1.25/0.7 是否对?是否该相对 baseline(如 resim≥0.8×baseline)而非绝对门?
- **D3 与 regime 监测器重复**:regime 已每日 re-sim top-10 backlog;此功能 per-candidate 是否冗余?能否直接复用 regime 已有 rows 避免重跑(命中即免 sim)?
- **D4 成本 / 槽争用**:批量跑 11 个 = 11 sim ~25min;若 pool 恢复(`ENABLE_POOL_PIPELINE`)槽争用?是否要预算闸 / 限流?
- **D5 异步轮询的健壮性**:job_id 丢失 / worker 挂 / 用户关页 → 任务状态如何?Redis NX 锁释放?
- **D6 范围蔓延 / NO-GO 一致性**:这会不会变相鼓励抽干 backlog(§6 NO-GO)?是否该在 UI 明示「持平≠该提交,仍有 marginal 稀释门(#40)」?
- **D7 perturb 结果代理有效性**:微扰后的 sim 是「微扰 alpha」的表现,作「原 alpha 当前数据」的代理在什么条件下成立/失效?
- **D8 端点契约 / 既有代码错配**:`alpha_pk` vs `brain_id`、drain-order item 字段、celery 任务注册、Redis 客户端(sync vs async)是否与现有约定一致?

---

## 5. 对抗审查结论 + v2 修订(2026-06-08,workflow `wqxbthho4`,40→25 确认)

**判定 = CONDITIONAL GO**:方向(per-candidate 当前数据复核补 regime top-N 盲区)成立;但 v1 核心机制 **decay 微扰**是未证假设,是 4 条 HIGH 的共同根因。据此 v2 修订:

### 5.1 关键反框:删掉 decay 微扰(消 H1/H7/M4 整摊)
审查数据(regime 05:38 rows)实证 **17/23 alpha 不微扰即返 fresh**,仅 ~26% 命中 dedup。故 **v2 不默认微扰**:
- **默认路径**:用存储结构设置原样 re-sim(复用 `make_variant`)。返 fresh(|Δ|>stale_eps)→ 出 verdict;命中缓存(≈baseline)→ 诚实标 `unmeasurable_cached`(=「BRAIN 返存储值,当前数据无法测」),**不强行微扰**。
- 微扰**降级为可选高级开关**(默认关),且若启用,结果明确标「微扰版 alpha(decay=K),非原 alpha」——不作提交依据。turnover 阈值连带删除。
- 这样 v2 **不依赖 H1 离线验证即可 ship**(代价:~26% 候选当下标「无法测」,可接受)。

### 5.2 v2 必纳入(HIGH)
- **H2 相对 verdict**:`stable ≥0.9×baseline / soft 0.6-0.9× / hard <0.6×`(baseline>0.1 守卫);新 config `RESIM_VERDICT_*_RATIO`。tag 显百分比(`1.35→1.34 持平(−1%)`)。
- **H3 margin 经济门**:verdict 加 `margin_killed`(resim_margin<5bps,优先于 hard),复用 `marginal_analysis` 5bps 逻辑。
- **H4 NO-GO 一致性**:feature flag 默认 OFF;`held 但 can_submit=False` → `hold_gated`;UI 红字「持平≠该提交,仍需过 self_corr<0.7 + marginal 双门」;功能名「regime 衰减诊断」;禁任何 batch-submit-held。并排展示 self_corr/marginal,不单看 sharpe。
- **H5 异步契约**:celery `@task(bind=True,max_retries=3)` + 捕获 Timeout/403/429 退避;**sync redis**(`redis_pool.get_redis_client`,非 asyncio);Redis 结果 `{status,error,partial,retry}` TTL 7d;body `{alpha_pks:int[]}`;任务注册 `__init__`+`celery_app`,仅 manual POST 非 beat。

### 5.3 v2 应纳入(MED)
- **M1 regime 复用**:POST 前查 `regime_monitor:latest` rows,命中且 <6h 直接复用免 sim;`_is_stale` 抽共享函数两处共用。
- **M3 槽闸**:POST 加 Redis NX 锁(`aiac:resim:batch_*` TTL 30min);chunk=`SLOT_LIMIT_USER-1`(留 1 槽);若 `ENABLE_POOL_PIPELINE` 启,预检 `brain:concurrent_sims` 满则返 `pool_saturated`。
- **M2 取样缺口标注**:UI 注「regime 仅采 top-N by Sharpe,本功能可指定任意候选」。
- **M6 审计**:celery 任务加 audit log(who/when/job_id)。

### 5.4 LOW(实现时定)
任务文件落位(新 `tasks/resim_backlog_tasks.py`)、GET 404+TTL 过期提示、前端独立 store 管轮询(不动 DrainOrderItem API)、成本计数器(reused vs fresh)、config `RESIM_BACKLOG_*` 热翻。

### 5.5 落地顺序
1. 后端:新 celery 任务(默认无微扰)+ 2 端点(POST 入队 / GET 轮询)+ config + regime 复用 + 槽闸;
2. 前端:单候选按钮 + 批量复核 REVIEW + 相对 verdict tag + NO-GO 文案 + self_corr/marginal 并排;
3. live 验证 1-2 候选(fresh + cached 各一)+ 单测 + build;
4. flag 默认 OFF,呈批后再开。

> **结论:v2 删微扰后无阻塞前提,可直接实现。** decay 微扰若未来要作正式 force-fresh,须先跑 H1 离线验证(分族量化失真),当前不依赖它。

### 5.6 实现 + live 验证(2026-06-08,已建)
- 后端:`resim_backlog.py`(纯 verdict,11 单测)+ `tasks/resim_backlog_tasks.py`(分块 chunk=槽上限−1 + regime<6h 复用 + Redis 增量 + NX 锁)+ ops 2 端点(POST/GET,flag gate + NX + 批量上限 30)+ config 8 设置 + `ENABLE_RESIM_BACKLOG` 注册 SUPPORTED_FLAGS。
- 前端:`api.js` 2 调用 + `SubmitBacklogMonitor.jsx` drain 表「当前数据」列(单候选「测」+ verdict tag)+ 批量「复核 REVIEW(N)」+ 轮询 + NO-GO 红字 Alert。build PASS。
- **live 验证(4 候选批量,job b0476e0a)全通过**:E5kNZA1G→stable(reused=True 复用 regime 1.55,92%)/ QPE25Yer→unmeasurable_cached(1.35→1.35 dedup)/ rKAr8Xeo+6XzYJEdJ→margin_killed(margin −13bps;两者表达式逐位相同=真重复 alpha,同结果验证一致性)。分块 + Redis 增量轮询(done 1→3→4)+ regime 复用 + 4 verdict 路径全验真。
- 验证期 `ENABLE_RESIM_BACKLOG` 热翻 ON(runtime-override),保持开启供使用。
