# AIAC 2.0 开发主线（唯一主线文档）

> **更新 2026-06-07** · 这是项目开发的**单一主线文档**。历史计划/设计/runbook 已归档到 `docs/archive/`(可追溯不删);竞品分析/架构/调研 reference 留在 `docs/` 根;操作输出(scan/audit/backup)留原地。
> 组织:**当前态速览 → 战略 → 本轮轨迹 → 持有策略 → 已交付 → 重启 SOP → NO-GO → 重评触发**。所有结论经 live PG / git / BRAIN 只读实测(见各段)。

---

## 0. 当前态(一屏速览)

| 维度 | 状态 |
|---|---|
| **战略** | execution/selection-limited **+ regime 低谷** → greenfield 分支 B(止损+监测+熬市场转)|
| **生产(挖掘)** | ⏸ **暂停** — `drain hg/s/e`(Redis 热)+ `.env ENABLE_POOL_PIPELINE=false`(下次重启全清停)|
| **auto-submit** | ⚠️ `ENABLE_AUTO_SUBMIT=true` + `.env AUTO_SUBMIT_MODE=live`(自 06-04,DB 实测)—— **不是关闭**;但 backlog 全 SKIP(#40)→ 06-04 后仅 0-1 提交 = **输出饿死非关闭**。独立 6h beat,不受池 drain 影响,守门栈 fail-closed(0 烂提交)。要真停热翻 flag |
| **提交选择器(#39)** | ✅ 已建+push(robustness∩正交∩去重 + sub-univ Sharpe);前向就绪,新供给来时挑货 |
| **regime 监测器(#41)** | ✅ **已激活+live**(2026-06-08 重启 + `ENABLE_REGIME_MONITOR` ON);手动重跑实测 verdict=**REGIME_DOWN**(submitted mean 1.7→0.63,frac_recovered 0.22<0.5);每日 07:30 beat 自动跑 |
| **真杠杆** | 新正交供给(跨区域=Consultant 考核 / 新数据源)—— **regime 锁着** |
| **下一步(用户)** | 熬市场转(主动权在市场):等 07:30 beat 报 `REGIME_TURNING` / Consultant 到位 → 恢复生产(§5)|

---

## 1. 战略定位(为何在这)

**反复(≥5 次独立推导 + BRAIN 权威数据)收敛:不是 discovery-limited,是 execution/selection-limited,且当前叠加 regime 低谷。**

1. **execution-limited 精确化(#40 实证,BRAIN 权威)**:史上 13 提交 / 67 backlog;BRAIN 自己的 before-and-after 判 **59/67 my-pool ΔSharpe<0(稀释)、54/67 竞赛 ΔScore<0、双正=0、49/67 带「边际 Sharpe 被套利」guardrail**。→ **backlog 不是杠杆**(非测量/门问题,是真稀释 + regime 套利)。
2. **submit-yield 自 05-20 塌到 ~0 = 两层**:① **字段卫生缺失**(ONESHOT→FLAT 切换,`_get_dataset_fields` 喂 LLM 时间戳/ISO/宇宙flag → 退化式)→ **#25c `ENABLE_FIELD_HYGIENE` 已修+live**(mean −0.34→−0.07、univ1 灭);② **REGIME 漂移**(老边际衰减/反转,提交赢家 mLxlen69 sh 2.01→re-sim −0.74、Xgkr0O6l 2.43→0.87)。#25c 后 yield 仍 0/683 = 必要不充分。「恢复 ONESHOT +1.5」是错目标(老结构已死)。
3. **BRAIN OS 架构隐藏**(用户确认 + 13/13 提交 os_metrics PENDING 实证):模拟只返 IS,OS(Semi-OS/Real-OS)提交后盲测、永不可作 steering/对账输入 → **提交前抗过拟合 robustness 是唯一可控质量杠杆**(见 [[reference_brain_os_hidden_is_only]] / `industry_alpha_optimization_survey`)。
4. **WQ 计酬模型**(用户提供):Base(日,数量+质量+ValueFactor 过拟合控制)+ Quarterly(季,平台 Weight + OS)。→ 目标 ≈ 质量×robustness + 数量 + 正交度;**「marginal ΔSharpe vs 我池」是可观测的 Quarterly/Weight 代理(平台 Weight 测不了)→ marginal/recon 机器保留**(对抗审查 `wd7190y28` 否决「拆 marginal」)。
5. **真杠杆只剩新正交供给**:Grinold 广度=独立下注=数据源(非 universe 轮转);USA catalog 已基本见顶,真大 breadth=跨区域(CHN/HKG/JPN/EUR)= **Consultant 考核解锁**(且考核需好提交,鸡生蛋)。regime 锁着 → 等市场转。

---

## 2. 本轮轨迹(2026-06-07 决策链)

greenfield 重定向 → #25c 字段卫生(commit `93a04a7`)→ regime 漂移实证(只读 re-sim)→ #39 选择器(robustness 模块 + sub-univ + 端点正交缺陷修 + auto-submit G3b 移植,`60905c1`)→ **#40 实证 backlog 死**(BRAIN 权威稀释 + regime 套利)→ WQ 计酬模型 resolve 目标 → 统一/拆-marginal 设计经对抗审查 `wd7190y28` 全 FLAGED 否决,收敛为外科手术(保 marginal 机器)→ **止损**(drain + .env)→ #41 regime 监测器(`6882512`)→ 前端选择器展示 + regime 告警(`2af3dda`)。

> 教训沉淀:① 大计划定稿前 workflow 多镜头对抗证伪(本轮 `w4lpfssg7`/`wd7190y28` 各推翻数个臆断);② 判 live 必查 `feature_flag_overrides` + 实测,勿读 config 默认;③ SQL 端点 mock 抓不到 bug,必打 live PG。

---

## 2A. 生产架构现状(四池化 / FLAT 退役)— 2026-06-07 实测

- **生产架构 = 四池(HG/S/E)解耦流水线**:常驻 worker 池(supervisor Popen-respawn)+ 持久 DB 队列(`hyp_intent` → `candidate_queue` → `alphas`)+ scheduler beat 喂意图;run.bat 启 Pool Supervisor + workers;gate 于 `ENABLE_POOL_PIPELINE`(.env,本会话止损已改 false 待重启,原 true 在跑)。近 30d 产出大头走池(POOL task + scope-less intent)。
- **FLAT / ONESHOT 已退役**:commit **`b89b732`(2026-06-06,−24240 行)** 删 `mining_tasks.py`(`run_mining_task`/`_run_flat_iteration`)+ FLAT/ONESHOT 路由 + orchestrator + `mining_agent`/`feedback_r1b`/`feedback_g5`/`field_screener`/`strategy_agent`/`evolution_strategy` + `experiment_runs` 表。**不可再起 FLAT/ONESHOT**(无 `run_mining_task`、无 `/tasks` POST、无 `/ops/flat-sessions`);无残留 import(不崩)。
- **迁移 Phase 状态**:Phase 0(地基)/ 1a(特征抽取)/ 1b(池架构全建:三循环+supervisor+scheduler+claim-lease+drain+budget+endpoints+beats)/ 1c-delete(删旧路 b89b732)= ✅ DONE;**Phase 2(认知 reconcile async beat 重接反馈)= ❌ 未激活**(`ENABLE_POOL_COGNITIVE_RECONCILE`=false,`cognitive_reconcile_tasks.py` 在但 gate 住;gate 在 yield 回升后再激活,见 §6)。

### 2A.1 知识库 / 反馈环分层现状(post-migration,2026-06-07 实测)
- **🆕 KB 重设计(定稿,2026-06-07)**:两份外部理想方案(生命周期 8 阶段 / LLM-driven 神经-符号)× 两轮 workflow 对抗审查(`w4mms4g0y`/`wx1sqvntx`)× live 实证 → `kb_redesign_unified_2026-06-07.md`。核心裁决:两份设计逃不出两堵墙(OS 隐藏 + 无人类),v2 多 Agent=给已有池阶段换名字 / 三库(向量+图+时序)=NEW_INFRA 反比例,净杠杆≈0;止损期只建 IS 体检卡 + 翻 `ENABLE_REGIME_MONITOR` + 诚实标注(os_sharpe cosmetic 改名 / decay_curve 标 IS-proxy / L0L2L3 known-dead),其余冻结;唯一杠杆仍是已 live 提交选择栈。
- **设计基线**:6 层量化切分 + KB 写侧闭环见 reference `kb_layered_architecture_2026-06-05.md` / `quant_pipeline_6layer_2026-06-05.md`(⚠️ 该 reference 部分前提已被 b89b732 改写——`agents/core/` + `agents/knowledge_extraction.py` 已删,读时以本节为准;但 `agents/services/rag_service.py` + `agents/hierarchical_rag.py` 仍 live)。
- **当前 live 闭环(走池,非旧 core/)**:① RAG 读 = `agents/hierarchical_rag.py`(池 HG stage);② **KB 写 = 池 E-stage `agents/pipeline/persister.py`**(本会话 Phase 1 四轨 Track A:SUCCESS_PATTERN + hypotheses un-gate);实测 `knowledge_entries` 12153 / `hypotheses` 2120 **今天仍在写** = 写侧闭环 live。
- **已退役死路(b89b732)**:`agents/core/` 已空(仅 `__pycache__`、源全删、**无 live caller**=死尾待清)、`agents/core/integration.py` / `agents/knowledge_extraction.py` 删;`r1a_attribution_log` 写停在 **06-05**(FLAT-era 归因路随 b89b732 退役)。`feedback_agent.py` 仍在(consultant/sync 反馈路)。**⚠️ 更正:`agents/services/rag_service.py` 未删(KB 读路径核心,live);之前误判删是查错路径 `services/rag_service.py`。**
- **Phase 2 的内涵** = 把反馈从「池 E-stage 同步写」升级为「异步 reconcile beat 读 EVALUATED → 插新 hyp_intent 闭环」,未激活(flag OFF)。
- **残留尾巴**:DB 71 个 FLAT 历史孤儿 task(可查不可起)+ `agents/core/` 空目录(源删仅 pycache,死尾待清)+ `feedback_agent.py`(consultant 路)。`CLAUDE.md` 架构段已据实更新(本会话)。

### 2A.2 其它 live 子系统 + flag 现状(2026-06-07 实测 `feature_flag_overrides` 表,非 cache)
> ⚠️ 注意:ad-hoc 子进程读 `settings.ENABLE_X` 会拿 config 默认(cache 冷),**flag 真值以 DB `feature_flag_overrides` 表为准**(本表已 DB 实测)。

| 子系统 | flag / 状态 | 一句 |
|---|---|---|
| **LLM 路由(per-function)** | `ENABLE_PER_FUNCTION_LLM_ROUTING`=**ON**(05-31)| `llm_service` per-node 选型;`LLM_PROVIDERS` 命名注册(DB 加密 key);active provider=**aliyun_coding_plan**(06-04 token-plan 额度耗尽降级);熔断 per-(provider,endpoint,model)。详 CLAUDE.md |
| **dataset bandit** | `ENABLE_DATASET_VALUE_BANDIT`=**ON**(05-22)| discounted Beta-Bernoulli `mining_weight` 日频 beat(`dataset_weight_refresh`)+ 池 scheduler 加权采样;reward=binary can_submit |
| **auto-submit** | `ENABLE_AUTO_SUBMIT`=**ON**+live(见 §0)| 6h beat,fail-closed 守门栈 G0-G10/G5b;backlog 死→~0 产出 |
| **BRAIN adapter / 凭证** | live | `brain_adapter` 单例 + DB 加密凭证(`WQBCredential`/`credentials_service`)优先 .env;`BRAIN_AUTH_CIRCUIT` 熔断 + fleet reauth coalescing |
| **优化闭环** | `ENABLE_OPTIMIZATION_LOOP`=OFF | `services/optimization` 冻结接口(Layer3 `run_one_cycle`);6h beat OFF;manual `POST /ops/optimization/optimize-alpha` 独立;§6 NO-GO |
| **regime 监测 / 认知 reconcile / Consultant** | `ENABLE_REGIME_MONITOR`=**ON**(06-08 激活,live REGIME_DOWN);其余 OFF | `ENABLE_POOL_COGNITIVE_RECONCILE`(Phase 2)/ `ENABLE_BRAIN_CONSULTANT_MODE`(跨区域闸)仍 OFF |
| **cell_stats 规范化** | live(commit `6a37cb7`,cutover 05-26)| 四表 per-(delay,universe) 统计,支撑池 HG 细粒度特征 |
| **Celery beats** | 12+ live | pool-scheduler(5min)/ lease-recycle(2min)/ regime-monitor(daily 07:30,gated)/ cognitive-reconcile(15min,gated)/ sync-datasets(daily)/ daily-feedback / os-corr-refresh 等;清单见 `celery_app.py:119+` |

## 3. 当前持有策略(greenfield 分支 B)

- **止损**:池停烧(drain 热 + .env 持久),挖掘产 0 可提交 = 纯烧 token/sim,已止。
- **监测**:**regime-turn 监测器(#41,已 live)** 每日 07:30 re-sim 13 提交 + 10 backlog 抽样于当前数据(rolling test_period,**口径 current IS 非 OS**);标定后 turn 规则 = **fresh 子集 frac_recovered≥0.5 ∧ mean_delta≥−0.25 ∧ mean≥1.0**(剔除 BRAIN dedup 缓存假恢复,旧松逻辑曾误报)→ `REGIME_TURNING` 告警 → 重启生产。当前实测 **REGIME_DOWN**。
- **选择器就绪**:#39 前向资产,新供给来时挑 robust∩orthogonal∩additive(保留 marginal/recon Quarterly 代理)。
- **等**:`REGIME_TURNING` 告警 / Consultant 考核到位 → 重启生产;否则熬市场转,主动权在市场不在工程。

---

## 4. 已交付(本会话 commit,全 push gitea)

| commit | 内容 |
|---|---|
| `93a04a7` | #25c 字段卫生(`ENABLE_FIELD_HYGIENE`,止 ONESHOT→FLAT 退化)|
| `60905c1` | #39 抗过拟合 稳健∩正交 提交选择器(`robustness_selector.py` + drain-order 端点 robustness/sub-univ/min_robustness 门/fragile 桶 + 端点 corr-to-pool 缺陷修 + auto-submit G3b 移植 + G5b sub-univ 门)|
| `6882512` | #41 regime-turn 在线 re-sim 监测器(`regime_monitor.py` + beat + Redis 持久 + `/ops/regime-monitor` + `ENABLE_REGIME_MONITOR`)|
| `2af3dda` | 前端:选择器展示(稳健列/sub-univ列/稳健门/fragile桶)+ regime 监测页 + OpsOverview `REGIME_TURNING` 告警 banner |

(早先本会话:`10dbdc8` Phase1 四轨 / 设计文档 `9f495d7`/`285b8a3`。)

---

## 4A. KB 重设计推进 TODO(止损期·低风险优先)

> 源 = `kb_redesign_unified_2026-06-07.md`(v1+v2 融合定稿)。接线已逐点实证。开工序 **A→B→C→D**。
> ⚠️ 全局依赖:**B1/B4 改的是 pool E-stage 代码 → 需 `run.bat --restart` 生效(重启前查 drain 残留是否有意止损);A/C 不碰 pool worker**。

> **✅ 完成状态(2026-06-08):A/B/C + 根治 double-acquire + regime 标定 全 ship,4 commit push gitea master。**
> - **Phase A**(`aff8fb1`):`ENABLE_REGIME_MONITOR` ON(DB override)+ beat 注册 + 串行 re-sim fix(绕 double-acquire 死锁)。首跑 23/23 实证 regime **DOWN**(58qroezz 2.33→0.89、mLxlen69 2.01→−0.74)。
> - **regime verdict 标定**(`aff8fb1`):剔除 BRAIN dedup 缓存假恢复(stale_eps)+ turn 改 fresh frac≥0.5 ∧ mean_delta≥-0.25 ∧ mean≥1.0(旧松逻辑误报 TURNING)。离线回放→REGIME_DOWN。✅ **2026-06-08 手动重跑(c5338212,~36min,23/23 0 error)live verdict 已更新成 REGIME_DOWN**(n_stale=6 / submitted frac_recovered=0.22 / mean_resim 0.63 vs baseline 1.7),stale TURNING 已覆盖。
> - **根治 double-acquire**(`aff8fb1`,`simulator.py`):`_run_one` 删冗余 acquire(simulate_alpha 已管槽)→ 1 slot/sim;47 测过。优化闭环潜伏死锁一并解除。**2026-06-08 串行重跑全程 concurrent_sims=1 实证根治生效。**
> - **Phase B**(`d9cb53e`):verdict_basis=IS / decay IS-proxy(纠 "OS-metric" 误注释)/ V-12 docstring / L0L2L3 known-dead。
> - **Phase C**(`74b4ec7`):`is_diagnostic_card.py`(11 测)+ ops drain-order 接线 + 前端体检卡列。restart 后 **live 验证**(端点返 card,分布 HOLD 4/REVIEW 7/SKIP 61)。
> - **docs**(`f201d0e`):`kb_redesign_unified` + INDEX + §2A.1/§4A 指针。
> - **regime 并发实验 → 回退串行**(2026-06-08,`c54680c` 试并发 → 本次回退):重启后 live 实测并发 **3-wide 无死锁**(max concurrent_sims=3,根治确认 live),**但净回归** —— `run_batch` 每 sim 的 600s `wait_for` 同时覆盖「排队等槽」,真 sim(~3min)下 3-wide 清不完 23 个 → **13/23 撞 sim_timeout(600s)**(串行 0 错)。**回退串行**(每 sim 独享新槽+full 600s → 23/23 零错);每日 off-hours 探针信号完整性 ≫ 提速。发现入 [[reference_brainsim_double_acquire_deadlock_2026_06_07]]。

**Phase A — regime_monitor 热翻(零代码,先做)**
- [x] A1 前置:确认 celery **beat + 主 worker 在线**(regime 跑主 worker 非 pool worker);每天 ~23 BRAIN sims + 需 creds。
- [x] A2 翻 `ENABLE_REGIME_MONITOR=true`(热;在 SUPPORTED_FLAGS,**无需 .env/重启**)。
- [x] A3 验证:等 07:30 beat 或手动触发 → `GET /ops/regime-monitor` + `RegimeMonitor.jsx` 出数(口径=current IS 非 OS)。

**Phase B — 诚实标注(标签/注释级,低风险)**
- [x] B1 `verdict_basis='IS'`:`rag_service.py:1605`(SUCCESS meta_data)+ `:~1390`(FAILURE)各加一行(⚠️ JSONB JSON-null footgun)。旧行可选 backfill。**依赖 pool 重启**。
- [x] B2 `decay_curve` IS-proxy:`decay_service.py:88` snapshot 加 `"basis":"IS"` + docstring + 读侧(`alpha_health_service`/`regime_monitor`)注释 + `models/alpha.py` 列注释。验证 `test_decay_service.py`。
- [x] B3 L0/L2/L3 known-dead:`backend/CODE_STATUS.md` 追加(current_expression 永不传,r8 0/2211)。纯文档。
- [x] B4 `os_sharpe` cosmetic:`evaluation.py:225-254` docstring 正名 IS train/test-split(os_sharpe live 恒 None);可选删死前缀。⚠️ eval 热路径,**必跑 `test_suite.py --all` 回归 0 漂移**。**依赖 pool 重启**。

**Phase C — IS 诊断体检卡(唯一新建,中风险;零新指标/库/sim)**
- [x] C0 设计决策:**端点按需组装,不写 `alpha.metrics`**(robustness/corr 池相对、每 drain 重算,持久化会 stale)。
- [x] C1 确认 `_iqc_marginal.recommendation` + V12/V16 flag 落点,补 `DrainOrderItem` 缺的 3 维(过拟合/turnover/FAILURE-相似)。
- [x] C2 `ops.py:4846 _item()` 加 `diagnostic_card`:5 维{过拟合(V12/V16)、流动性(universe×turnover)、拥挤(max_corr_to_selected)、历史相似(FAILURE 命中)、提交建议(robustness_verdict⊕marginal rec⊕value_tier)}+overall。**剥离 OS 字段**。
- [x] C3 前端 `SubmitBacklogMonitor.jsx` 渲染 card(展开行/tooltip)。
- [x] C4 测试 ops endpoint + `npm run build`(eslint 无配置→build 验证)。**依赖 uvicorn --reload 自动加载**。

**Phase D — 收尾**
- [x] D1 commit + push gitea(含 KB 重设计 doc + INDEX/§2A.1 指针 + 上轮 rag_service 误标更正 + B/C 代码;**排除** `scripts/verify_prompt_changes_2026_05_24.py`)。
- [x] D2(可选)存 memory:KB 重设计结论(v2=换皮镀金 / L0L2L3 真死 / V-12 cosmetic / 唯一杠杆=提交选择)。

**接线核实快照**:regime_monitor=真零代码(beat `celery_app.py:138` 已注册 + flag `feature_flag_service.py:566` 在 SUPPORTED_FLAGS 热翻);体检卡 robustness 半边已在 `DrainOrderItem`(`ops.py:4535-4548`),缺 3 维 + 编排 + 前端。

---

## 5. 重启 / 恢复 SOP

**run.bat 重启会一并生效**:① `.env ENABLE_POOL_PIPELINE=false` → 池全清停(scheduler/supervisor/workers);② 后端载入 regime beat + `/ops/regime-monitor` 端点 + 选择器新字段;③ 前端重 build/serve。
⚠️ **重启前**:这次 `drain hg/s/e` 是**有意止损**(非残留),别清错方向。

- **启动 regime 监测**:重启后翻 `ENABLE_REGIME_MONITOR` on(Feature Flag 控制台,热;`ENABLE_POOL_PIPELINE` 非热——不在 SUPPORTED_FLAGS,只 .env)。daily beat 07:30 跑;看 `GET /ops/regime-monitor`。
- **恢复生产挖掘**(regime 转后):`.env ENABLE_POOL_PIPELINE=true` + `clear_drain` hg/s/e(⚠️ 会 flood 处理 ~1800 积压队列,干净恢复先 `purge_pending` 或重启 fresh)+ run.bat 重启。
- **急停 auto-submit**:热翻 `ENABLE_AUTO_SUBMIT=false`(**当前 =true + mode live**,但 backlog 死→0 产出;真要停才翻)。

---

## 6. 明确 NO-GO / 冻结(防重复踩坑)

| 项 | 判定 | 一句理由 |
|---|---|---|
| 多挖 / orchestrator 翻 flag / Phase2 bandit / 多账号 | NO-GO 冻结 | 供给 >> 选择能力;regime 下挖更多 = 0 yield 纯烧 |
| 抽干 backlog 当产能杠杆 | NO-GO | #40:BRAIN 权威 59/67 稀释 + 49/67 regime 套利,双正=0 |
| 纯设置扫掠优化(`ENABLE_OPTIMIZATION_LOOP`)| NO-GO | 0.2% 转化 = STOP 闸 46× 偏离;保留 manual/robustness/recon 工具箱 |
| EVAL 阈值 / 提交门微调 | NO-GO | yield=0 是分布整体衰减(p95 signed sharpe 0.55<<1.25),非门偏高;违「阈值改致 89%→1%」教训 |
| universe 轮转扩 breadth | NO-GO | 同-region self-corr 门 + 相关 PnL≈0 breadth;TOPSP500 等实证 0-yield |
| delay-0 挖掘作 breadth | NO-GO | 230 alpha avg −0.15 / 0 can_submit = 根本无信号 |
| 深度因子工程 / 集中价量 | NO-GO | edge 已挖尽,杀多样性 |
| RAG 深层 / CoSTEER 重投入 | LOW | A/B category-overlap 对 sharpe 零效应(p=0.91);环已闭,基础设施保留不再投激活 |
| 拆 marginal / 统一两路选择器 / 提 per_run_cap | NO-GO | 对抗审查 `wd7190y28`:Weight 不可证伪→my-pool ΔSharpe 是可观测代理,marginal/recon 载重该留 |
| live OS 对账(原 #34)| 架构死 | OS 提交后盲测隐藏,永不可作 steering 输入(13/13 os_metrics PENDING)|
| R12 LLM assistant-mode 30d obs | 搁置 | discovery 轴假设;obs 从未起算,跑也 INSUFFICIENT |

---

## 7. 重评触发 / 决策日历

| 触发 | 动作 |
|---|---|
| **regime 监测器报 `REGIME_TURNING`**(提交集 re-sim 回升 / ≥1 过门)| 复核 → 恢复生产挖掘(§5)+ 重评 #39 在线 re-sim / #31 reward |
| **Consultant 考核到位**(`ENABLE_BRAIN_CONSULTANT_MODE`)| 跨区域 sync(CHN/HKG/JPN/EUR)+ 扩**池挖掘**到新 region(非 FLAT,已退役)= 真大 breadth(唯一能抬 \|sharpe\| 顶过 1.25 门)|
| **~2026-07 BRAIN OS 端点**(复查 `os.osISSharpeRatio` 是否填)| 若填 → 建 os 轮询接 recon realized 腿;仍 null → 继续挂(目前实测永 PENDING)|

---

## 附:索引

- **归档**(历史计划/设计/runbook):`docs/archive/`(本主线的详细背书,如 `dev_plan_greenfield_2026-06-07.md` / `unified_submit_selector_design_2026-06-07.md` / `pool_native_reward_redesign_2026-06-07.md` / `orthogonality_steered_exploration_plan_2026-06-05.md` / `four_pool_decoupling_plan_2026-06-05.md` 等)。
- **Reference**(留 `docs/` 根):`competitive_analysis_v3` / `industry_alpha_optimization_survey` / `quant_pipeline_6layer` / `kb_layered_architecture` / `rd_agent_alpha_gpt_research` / `qlib_alpha_research` 等。
- **Memory**(`~/.claude/.../memory/`):`project_dev_plan_branch_b_regime_trough_2026_06_07` / `reference_wq_consultant_compensation_model` / `reference_brain_os_hidden_is_only` / `project_submit_yield_collapse_field_hygiene_2026_06_07`。
- **代码状态**:`backend/CODE_STATUS.md` / `REFACTORING_STATUS.md` / `agents/core/ARCHITECTURE.md` / 根 `CLAUDE.md`。
