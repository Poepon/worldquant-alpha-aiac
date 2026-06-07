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
| **regime 监测器(#41)** | ✅ 已建+push;待激活(重启 + 翻 `ENABLE_REGIME_MONITOR`)→ 每日 re-sim 探老边际恢复 |
| **真杠杆** | 新正交供给(跨区域=Consultant 考核 / 新数据源)—— **regime 锁着** |
| **下一步(用户)** | run.bat 重启(生效池暂停 + 载 regime beat/端点/选择器新字段)+ 翻 `ENABLE_REGIME_MONITOR` |

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
- **设计基线**:6 层量化切分 + KB 写侧闭环见 reference `kb_layered_architecture_2026-06-05.md` / `quant_pipeline_6layer_2026-06-05.md`(⚠️ 该 reference 部分前提已被 b89b732 改写——`rag_service`/`core/`/`knowledge_extraction` 已删,读时以本节为准)。
- **当前 live 闭环(走池,非旧 core/)**:① RAG 读 = `agents/hierarchical_rag.py`(池 HG stage);② **KB 写 = 池 E-stage `agents/pipeline/persister.py`**(本会话 Phase 1 四轨 Track A:SUCCESS_PATTERN + hypotheses un-gate);实测 `knowledge_entries` 12153 / `hypotheses` 2120 **今天仍在写** = 写侧闭环 live。
- **已退役死路(b89b732)**:`agents/core/` 已空(仅 `__pycache__`、源全删、**无 live caller**=死尾待清;注:之前审计误称它被 E-eval 用,实测证伪)、`rag_service.py` / `core/integration.py` / `knowledge_extraction.py` 删;`r1a_attribution_log` 写停在 **06-05**(FLAT-era 归因路随 b89b732 退役)。`feedback_agent.py` 仍在(consultant/sync 反馈路)。
- **Phase 2 的内涵** = 把反馈从「池 E-stage 同步写」升级为「异步 reconcile beat 读 EVALUATED → 插新 hyp_intent 闭环」,未激活(flag OFF)。
- **残留尾巴**:DB 71 个 FLAT 历史孤儿 task(可查不可起)+ `agents/core/` 空目录(源删仅 pycache,死尾待清)+ `feedback_agent.py`(consultant 路)+ **⚠️ `CLAUDE.md` 仍按已删的 `mining_tasks`/FLAT/`rag_service`/`core` 描述架构 = stale,待更新**(否则按文档找 run_mining_task/rag_service 会扑空)。

### 2A.2 其它 live 子系统 + flag 现状(2026-06-07 实测 `feature_flag_overrides` 表,非 cache)
> ⚠️ 注意:ad-hoc 子进程读 `settings.ENABLE_X` 会拿 config 默认(cache 冷),**flag 真值以 DB `feature_flag_overrides` 表为准**(本表已 DB 实测)。

| 子系统 | flag / 状态 | 一句 |
|---|---|---|
| **LLM 路由(per-function)** | `ENABLE_PER_FUNCTION_LLM_ROUTING`=**ON**(05-31)| `llm_service` per-node 选型;`LLM_PROVIDERS` 命名注册(DB 加密 key);active provider=**aliyun_coding_plan**(06-04 token-plan 额度耗尽降级);熔断 per-(provider,endpoint,model)。详 CLAUDE.md |
| **dataset bandit** | `ENABLE_DATASET_VALUE_BANDIT`=**ON**(05-22)| discounted Beta-Bernoulli `mining_weight` 日频 beat(`dataset_weight_refresh`)+ 池 scheduler 加权采样;reward=binary can_submit |
| **auto-submit** | `ENABLE_AUTO_SUBMIT`=**ON**+live(见 §0)| 6h beat,fail-closed 守门栈 G0-G10/G5b;backlog 死→~0 产出 |
| **BRAIN adapter / 凭证** | live | `brain_adapter` 单例 + DB 加密凭证(`WQBCredential`/`credentials_service`)优先 .env;`BRAIN_AUTH_CIRCUIT` 熔断 + fleet reauth coalescing |
| **优化闭环** | `ENABLE_OPTIMIZATION_LOOP`=OFF | `services/optimization` 冻结接口(Layer3 `run_one_cycle`);6h beat OFF;manual `POST /ops/optimization/optimize-alpha` 独立;§6 NO-GO |
| **regime 监测 / 认知 reconcile / Consultant** | 全 OFF(默认)| `ENABLE_REGIME_MONITOR`(#41 待激活)/ `ENABLE_POOL_COGNITIVE_RECONCILE`(Phase 2)/ `ENABLE_BRAIN_CONSULTANT_MODE`(跨区域闸)|
| **cell_stats 规范化** | live(commit `6a37cb7`,cutover 05-26)| 四表 per-(delay,universe) 统计,支撑池 HG 细粒度特征 |
| **Celery beats** | 12+ live | pool-scheduler(5min)/ lease-recycle(2min)/ regime-monitor(daily 07:30,gated)/ cognitive-reconcile(15min,gated)/ sync-datasets(daily)/ daily-feedback / os-corr-refresh 等;清单见 `celery_app.py:119+` |

## 3. 当前持有策略(greenfield 分支 B)

- **止损**:池停烧(drain 热 + .env 持久),挖掘产 0 可提交 = 纯烧 token/sim,已止。
- **监测**:**regime-turn 监测器(#41)** 每日 re-sim 13 提交 + 10 backlog 抽样于当前数据(rolling test_period,**口径 current IS 非 OS**);提交集 re-sim 均值回 ≥0.5 或 ≥1 个过 1.25 → `REGIME_TURNING` 告警 → 重启生产。
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
