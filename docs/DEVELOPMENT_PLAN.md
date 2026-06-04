# AIAC 2.0 开发计划（汇总）

> 生成日期：2026-06-04 · 技术负责人汇总 · 跨 8 条开发线提取的开口工作项归并、去重、按依赖排序。
> 组织方式：**NOW / NEXT / LATER** 三档（非按开发线），每项标注「支持 / 违背 / 中性」战略基调。
> **修订 2026-06-04 下午**：自动提交从「待验证影子」变为 **LIVE + 首笔真实提交(15816)**;N1/§6 据实重写,L5/§4 据 os 探查结论更新。所有数字经 live PG 实测核实(见各段)。

---

## 0. 战略定位（定调）

本系统经记忆中 **≥4 次独立推导**反复收敛为同一结论：**execution-limited / selection-limited，不是 discovery-limited**。真瓶颈不在「挖不出 alpha」，而在「挖出来的 clean alpha 没人提交」——截至 2026-06-04 下午,史上仅 **13 次提交**（含当天首笔自动提交 15816）vs **66 can_submit 积压**（live PG 实测,全 USA）。BRAIN 的提交门是 **self-corr < 0.7（同 region）**，所以提交价值取决于「与已提交池的正交度」而非「单 alpha 质量」。

**2026-06-04 自动提交 live 实测强力印证此判断**：最近一次 beat 评估 41 个候选,1 提交、40 跳过,其中 **36 个(90% of skips)卡在 `G3b_self_corr`（self_corr ≥ 阈值）**,另 2 卡 sharpe、2 卡推荐。即:66 积压里真正「与已提交池正交、可提交」的子集只有**个位数**,提交速率天然被**正交度**（而非候选数量/per_run_cap）节流。这正是 breadth 天花板的运行时显形 —— 抽干当前正交子集后,唯一能继续提交的路是**新正交数据源**（见 L1）,不是多挖更多同源 alpha。

由此，**只有两根高杠杆**：

1. **抽干提交积压**：按 marginal 价值 / 正交度排序 + self-corr 自动筛选，并**验证 offline ΔSharpe 代理是否可信**（`marginal_recon` kill-switch）。
2. **新正交数据源扩 breadth**：Grinold 意义下广度 = 独立下注 = **数据源**，不是 universe 轮转。当前 catalog 仅 USA，真大 breadth = 跨区域（CHN/HKG/JPN/EUR），blocked-on Consultant。

被反复**驳回**的低杠杆（除非有新证据，一律 LOW / NO-GO）：多挖（供给已 >> 选择能力）、EVAL 阈值微调、universe 轮转（同 region 撞 self-corr 门 + 相关 PnL）、纯设置扫掠优化（Stage A 实测 0.2% 转化 = STOP 闸 46× 偏离）、深度因子工程（集中价量字段 edge 已挖尽）、RAG 深层 / CoSTEER 重投入（同线 A/B 实测 category-overlap 对 sharpe 零效应 p=0.91）。

**本次汇总的关键事实更正**：记忆里大量「未 commit」标注已过时。核实 `git log`，以下全部已落地 master / gitea：`13a75a7`（sweep deflation + 正交抽干）、`06b7536`（L2 ΔSharpe + 覆盖诊断）、`e42a838f` / `9111bfc`（marginal recon + kill-switch）、`d376fd1`（手动优化）、`b846db1`（多厂商 LLM + Brain 凭证迁移）。**今天几乎没有「新建」工作要做——绝大多数高杠杆动作是「运维执行 + 监控 soak」，不是写代码。**

---

## 1. NOW（P0 — 现在就做，最高杠杆且 ready）

### N1. 自动抽干提交积压（execution-limited 唯一真动针）

- **做什么**：`auto_submit_tasks` beat 每 6h（:35）按正交度顺序自动提交 1 条最正交的 additive 候选;`SubmitBacklogMonitor` 的「正交抽干顺序」面板作人工补充。
- **正交子集有多大(两种度量,别混淆)**：面板的 drain-order 用**离线 pairwise self-corr**估算 → 66 积压里约 **41 看似可正交 / 25 离线相关阻塞**;但自动提交 beat 的 G3b 用 **BRAIN 权威 self_corr(vs prod 池)**,最近一次 beat 实测 41 候选里 **36 个被 self_corr ≥ 阈值挡掉、仅个位数真正交**。**以 BRAIN 权威度量为准** —— 真正能提交的远少于离线 41,且每提交一条 prod 池就变、下一轮重算（§0)。离线 drain-order 仅作排序启发,不是「41 条都能发」。
- **下一步动作**：自动提交 beat 现为**主路径**（见下「更新」）;`/ops/submit-backlog` 面板（`SubmitBacklogMonitor.jsx:251 submitPks`）降为人工补充/急用通道。**注意区分两条独立路径**:`config.py:1057-1060` 的 Stage A SubmitPolicy 恒返 `queue`、NEVER auto-submits —— 那是**优化闭环(L2)** 的策略;本 N1 的 `auto_submit_tasks` beat 是**另一条路径**,直接调 `submit_alpha`、不经优化闭环,守门栈自带 fail-closed（见下）。
- **依赖**：worker + beat 在线 **且已在 `48e7246` 后重启**（否则定时 beat 撞 PENDING,见下「⚠ 运维必做」）。
- **来源**：drain-order 端点 + 面板（`13a75a7`/`06b7536`）+ 自动提交闭环（`5a5be7b`…`48e7246`）；战略基调 P0「抽干积压」。
- **战略**：✅ 支持（直击真瓶颈、不烧 sim 配额,仅消耗提交额度 —— 而提交正是这里要花的「钱」;每条提交都层层 fail-closed 守门）。
- **附带价值**：每次成功提交都会触发 `AlphaService._freeze_predicted_marginal`（`e42a838f`）冻结 offline+BRAIN 两预测进 `metrics._recon_predicted_delta_sharpe` → 给 N2 累积 forward-test 样本。
- **更新(2026-06-04 下午 — 已 LIVE + 首笔真实提交)**：自动提交不再是「待验证影子」——已切 **live 并完成首笔真实提交**:alpha **15816(`YPQ1e3VM`)→ IQC2026S2**,date_submitted 2026-06-04 15:23:15,Sharpe 1.75/Fitness 1.70/Turnover 0.38、BRAIN 8 项 check 全 PASS（live PG 核实）。当前生效开关:`ENABLE_AUTO_SUBMIT=true`(DB 热覆盖) + `ENABLE_CAN_SUBMIT_REFRESH=true` + `.env AUTO_SUBMIT_MODE=live` + `AUTO_SUBMIT_PER_RUN_CAP=1`(每 beat 只提**最正交的 additive 候选**,提交后 :50 刷新 can_submit、下个 :35 beat 重挑,逐条最优) + `daily_cap=4`。守门栈层层 fail-closed:G0 主开关 / G1 recon kill-switch / G2-G3 can_submit 严格 SQL + BRAIN 形式合规 / G3b self_corr 已测<阈 / G4 新鲜度(`metrics._brain_can_submit_at`≤12h) / G5 BRAIN 红线 / G6 margin≥5bps / G7 推荐=SUBMIT / G8 additive(sign_tier) / G9 正交排序 / G10 `submit_alpha` 原生兜底。设计见 `auto_submit_design_2026-06-04.md`。
- **⚠ 运维必做(尚未确认已做)**：`/check` 修复(`48e7246`,gate4 改用 GET `/alphas/{id}/check` 权威结果、根治 `/correlations/SELF` 永久 PENDING)是 **in-module 代码、无热加载**。**运行中的 celery worker 若在 `48e7246`(提交于 2026-06-04 23:20 北京)之前启动,定时 :35 beat 仍跑旧代码、仍会撞 PENDING 发不出**。首笔 15816 之所以发成功,是用 fresh 进程**手动触发**的(15:07 旧码 pending 被拒 → /check 修复 → 15:23 fresh 进程提交成功;两次 beat_run_id 均在 audit 表可查)。**要让定时 beat 真正自主提交,须 `run.bat --restart` 让 worker/beat 加载 `48e7246`。** 重启后第一件事:看 `GET /ops/auto-submit/audit?latest_only=true` 确认定时 beat 真提交(而非全 PENDING-rejected)。
- **急停**：热翻 `ENABLE_AUTO_SUBMIT=false`（ops flag,即时,无需重启）;`mode`/`cap` 改 .env 须重启（故意的安全摩擦,防误操作放大提交量）。

### N2. 维持并监控 marginal_recon kill-switch（验证 offline ΔSharpe 代理可信度）

- **做什么**：kill-switch 已**实时接线 fail-closed**（drain-order 端点每次调用对本积压算 offline ΔSharpe ↔ BRAIN before-and-after 的 sign-agreement）。live 实测 n≈38–42 ≥ 15、verdict = **supported**（约 87.5% 符号一致 / Spearman 0.70）。
- **下一步动作**：**无需开发**。定期看 `GET /ops/marginal-reconciliation` 的 verdict；若某区样本跌破 `MIN_PAIRS_FOR_VERDICT=15`（`marginal_recon.py:39`）或符号一致率 ≤ `KILL_SIGN_AGREEMENT=0.60`（`:38`），端点自动 `route_on_sign_verdict` fail-closed 退纯广度（`9111bfc`，`:50`），**无需人工干预**。运维上确认 worker 在线让 IQC marginal auto-audit 持续回填 `_iqc_marginal`。
- **依赖**：worker + beat 在线（IQC auto-audit）。
- **来源**：`backend/marginal_recon.py`；`9111bfc` fail-closed；战略基调 P0「验证 offline ΔSharpe 代理是否可信」。
- **战略**：✅ 支持。**与 N1 互锁**：必须先真提交（N1）→ 累积 forward-test pairs → 才能把 verdict 从「事后对账」推进到「forward 二次印证」。当前在 verdict=supported 期间，ΔSharpe 排序作权威 routing 可接受；若退回 insufficient_sample 则降为辅助参考。
- **更新(2026-06-04)**：首笔 15816 提交已触发 `_freeze_predicted_marginal` —— offline predicted ΔSharpe=**0.119**、BRAIN-pre-submit=**0.09** 已冻进 `metrics._recon_predicted_delta_sharpe`,即 forward-test 累计样本从 0 → **1 对**。N1 持续自动提交会继续累积;但注意 forward 的 **realized 腿仍结构性 blocked**（见 L5）,当前能累积的只是 predicted↔BRAIN-pre-submit 一致性,不是 predicted↔真实 OOS。

> **N1 + N2 是本计划的重心。** 二者完全踩中战略基调 P0，且都是「运维执行 + 监控」而非工程。所有其它线在它们完成前都应让路。

---

## 2. NEXT（P1 — 数周内 / 待 soak 或近期 gate）

### X1. 多厂商 LLM 配置 + Brain 凭证迁移加密 DB —— 端到端冒烟（防退化）

- **做什么**：`b846db1`（今天落地，已 push gitea/master）引入两项重运维改动：① 全局 flag `ENABLE_PER_FUNCTION_LLM_ROUTING`（startup 默认 OFF `config.py:307`，记忆记 5-31 经 DB runtime-override 翻 ON）的线上状态需确认；② Brain 平台凭证从 `.env` 迁移到加密 DB（adapter DB 优先、`.env` 仅兜底）。
- **下一步动作**：worker/backend 重启加载新代码后：(a) 查 `/ops/flags` 或 `FeatureFlagOverride` 确认路由 flag 状态；跑一轮 FLAT 看 `LLMCallLog` per-node model 是否落 **kimi-k2.6**（确认 `__default__` 兜底 + 凭证迁移没把路由打回 `.env` OPENAI_*）。(b) 跑一次真实 BRAIN 调用（simulate 或 sync）确认 DB-优先凭证解析能登录（DB 凭证未配 / 解密失败会静默回退 .env 或登录失败）。
- **依赖**：worker 重启 + 一轮 FLAT 产出 + 一次真实 BRAIN 调用。
- **来源**：`b846db1`、`7034050`（默认模型 durable 回退 kimi）、`config.py:83-84`。
- **战略**：◼ 中性（「防退化」非「求进展」；属降本 + 稳定性维护）。**但 P1 优先级正当**——若凭证迁移把生产打坏会同时阻断 N1/N2 所需的 worker 在线。

### X2. delay-0 原生 FLAT session 真跑一轮（接线已 ship，产出待 soak）

- **做什么**：起一个 delay-0 FLAT session（AUTO 跨 11 个 delay-0 数据集，bandit 冷启均匀），观察是否产出过 delay-0 严门的 alpha（`EVAL_SHARPE_MIN_DELAY0=2.0` / Fitness≥1.3，`config.py:1036`）。delay-0 是**真新可挖面**（字段 roster 不同：analyst4 +50 / fundamental6 +106 / news12 +22 独占字段）。
- **下一步动作**：起 AUTO delay-0 session，数据验证「严门是否压低产出」而非假设。注意记忆载 delay-0 曾撞长生命周期 client-rot sim-hang（已修 `d650222`/`36bc39b`，需 worker 重启加载）。
- **依赖**：worker 重启；一轮 session soak。
- **来源**：`46cb31c`（端到端接线）+ `b8a956`（delay-aware 阈值）；`config.py:1036`。
- **战略**：✅ 支持（delay-0 是 USA catalog 内的正交新面，符合 breadth 基调；属窄轴 breadth）。

### X3. USA catalog 新到 / 未开采数据集持续监控（coverage tab 运营节奏）

- **做什么**：P1-2 已**推翻「USA catalog 饿死」前提**（目录新鲜：available=19、TOP3000 sync 实测 BRAIN 持续新增 17→18→19）。用现有「数据覆盖」tab 监控 `is_new`/`is_untapped`，对新到数据集（如 earnings4 = 375 字段全新数据集，0 alpha 是才 9h 没挖**非饿死**）一键强制挖立即覆盖。
- **下一步动作**：低频运营，**非工程项**。定期看 coverage tab + 对 is_new/is_untapped 行点「强制挖」。
- **依赖**：无（工具已 ship）。
- **来源**：`backend/routers/ops.py:5062` GET /ops/datasets/coverage（`06b7536`）。
- **战略**：✅ 支持（P2 运营优先级，但归到 NEXT 因与 breadth 同向）。

---

## 3. LATER / 条件性（P2 — 待决策日 / 外部 gate）

### L1. 跨区域数据源同步（CHN/HKG/JPN/EUR）—— 唯一真大 breadth 轴，blocked-on Consultant

- **做什么**：catalog 当前只有 USA = 真天花板。一旦 BRAIN Consultant 升级（`ENABLE_BRAIN_CONSULTANT_MODE` 翻 ON），先 `POST /datasets/sync?region=CHN/HKG/JPN/EUR` 填 cell（cell-stats schema 已支持 per-region），再扩 FLAT 到新 region。这是 Grinold 意义上的**独立下注**（不撞同-region self-corr 门）。
- **依赖**：**blocked-on-decision** —— 等用户收到 Consultant 升级邮件后手动翻 flag。`config.py:352 CONSULTANT_REGION_UNIVERSES` 已预置 5 region；`sync_datasets_from_brain` 默认 region=USA（`tasks/sync_tasks.py:481`），daily beat 不同步其它 region。
- **来源**：`06b7536` commit msg；`config.py:348`。
- **战略**：✅ 支持（升级后这是最高杠杆 breadth）。**升级前不可执行**，标为未来。

### L2. 优化闭环（Stage A 自动 beat + B/C 升档）—— 正式封棺，维持 STOP

- **决策**：维持 STOP。`ENABLE_OPTIMIZATION_LOOP` 保持 OFF（`config.py:1061`），不再投 Stage A 14d 观察。
- **下一步动作（可选清洁）**：在 plan 文档头部标 **SUPERSEDED-by-execution-limited 判决**，避免后人误以为还在等 14d gate。**无需改代码。**
- **依赖**：无（已被 2026-06-03 三探针 + 3-critic 对抗审查 0.2% 单点实测短路 = STOP 闸 46× 偏离）。
- **来源**：memory `project_optimization_methodology_refuted_execution_limited_2026_06_03`；`optimization_tasks.py:83-87` beat fail-closed。
- **战略**：✅ 支持判 STOP（详见 §5 NO-GO）。保留的副产物（manual 优化 / robustness filter / drain / recon）作为 execution-limited 工具箱继续用。

### L3. R12 决策（~2026-07-04±5d）—— 建议整体搁置，不安排日历时间

- **决策**：建议 **NO-GO / 搁置**。R12 押的是「LLM 当 research-assistant vs 当 expression-author」= L1 生成 / discovery 轴质量问题，与 execution-limited 基调**冲突**。
- **实证（2026-06-04 live PG）**：`ENABLE_LLM_ASSISTANT_MODE` 不在 override 表 = 仍 default OFF（`config.py:494`）；12,794 个 alpha 的 `metrics->>'llm_mode_used'` **全为 NULL**（obs 从未起算）；6 个 sentinel stamp key 里 **5/6 全时段 0 行**。即便 7/4 跑 evaluator 也只会返回 INSUFFICIENT。
- **下一步动作**：把 `ENABLE_LLM_ASSISTANT_MODE` / R12 critical-path 正式标 deferred（写进 `flag_lifecycle.md`）。**不消耗 sim 预算跑 30d obs**（稀缺 sim 槽应给 N1/N2/L1）。决策工具链（`scripts/r12_decision_evaluator.py` + `verify_sentinel_restore` + runbook）已 ship 且 decision-independent，留着不碍事。把记忆/计划里「决策日 ~2026-07-04」标为 **stale**（它从 Sprint1 ship +30d 起算，而 obs day-0 从未发生）。
- **来源**：`ab7974d`、live PG query、`config.py:494`、`docs/r12_obs_rollout_checklist.md`。
- **战略**：❌ 冲突 → 搁置。Sprint 5 cleanup（B4.2 retire G3 shadow / 6 sentinel 退役）随之永久挂在 R12-GO 闸后，G3 shadow 代码长期保留（无害）。

### L4. 挖掘 Orchestrator（自动续挖）Phase 1 完成并冻结 —— 不翻 flag

- **决策**：Phase 1 代码已完整 ship（flag default OFF = no-op，零成本零危害），**整体冻结**。`ENABLE_AUTO_ORCHESTRATOR` 翻转、Phase 2 bandit、Phase 3 多账号全部 gated on 提交瓶颈解决之后再议。
- **理由**：orchestrator 核心价值 = 自动多挖 / 续命产出更多 alpha，与 execution-limited 正面**冲突**（供给已 >> 选择能力）。即便 soak gate 真过了也不该优先做。
- **soak gate 现状（不是「差临门一脚」）**：① 流水线 ≥48h soak 用户已跳过（0 实战数据）；② §4 前置「heartbeat-abort ≥1 周无 false-positive」在 2026-06-03 被**反向证伪**（task 3930 出首个 PROV 后 14min 被 heartbeat 误杀，`c07a1ea` 刚 fix 但 0 post-fix soak）。
- **下一步动作**：若未来真要翻 flag，先让 `c07a1ea` per-coroutine liveness watchdog 在生产跑满几天确认无误杀，再从 max_running=1/daily=2 保守起步（而非 default 3/10）。**R14 task stop-loss 维持 deferred、不单独接线**（无 orchestrator 接班则 PAUSE 留死结 = 配额空转反目标，现态正确）。
- **来源**：`config.py:893`；plan §4 前置；`docs/heartbeat_liveness_redesign_2026-06-03.md` + `c07a1ea`；`mining_tasks.py:1141`（R14 未接线）。
- **战略**：❌ 冲突 → 冻结。

### L5. forward-test realized 腿 + recon 累积 —— blocked-on-data，挂着观察

- **做什么**：predicted↔realized ΔSharpe 真 live 对账。**结构性不可得（2026-06-04 直接探查确认,不是「等几天」）**：① 本地 `alpha_pnl` 是冻结 OS 回测窗（止 2023-12-29，delete-then-insert 无增长）;② BRAIN `GET /alphas/{id}` 的 `os.osISSharpeRatio` 对**所有**已提交 alpha（含 2026-04-19 即 1.5 个月前的)**均 null、os.checks 全 PENDING**,且 `/check` **不触发** os 计算;③ `before-and-after-performance` 对已提交 alpha 返 **400**（只对未提交 CAN_SUBMIT alpha 出值）;④ `/correlations/prod` 403（USER 无 Consultant）。→ realized 腿没有任何可用数据源。
- **下一步动作**：**os 轮询 beat 暂缓**（无数据 = 空转,2026-06-04 决定）。forward 端点已诚实标 `blocked_no_live_pnl` + `realized_blocked_reason`。N1 自动提交会持续累积 predicted↔BRAIN-pre-submit 一致性（非真实 OOS,见 N2）。**~2026-07 复查**任一已提交 alpha 的 `os.osISSharpeRatio` 是否开始填充;填了再建 os 轮询 + 接 `marginal_recon` realized 腿。在此之前**不押日历时间**。
- **来源**：`ops.py:5396/5424`；`marginal_recon.py:9-19` DATA REALITY 注释；2026-06-04 BRAIN 只读探查 + memory `project_auto_submit_shipped_2026_06_04`。
- **战略**：◼ 中性（blocked-on-external-data,无可推进动作）。

### L6. 低杠杆维护清理（一次性、可选）

- **v26_38_39（FIELD/HYPOTHESIS_INSIGHT 退役）**：建议直接选 **A 彻底退役**（约 2h）——删 enum 3 成员（`knowledge.py:34-36`）+ 写块（`feedback_agent.py:1131-1193`）+ 计数器（`metrics_tracker.py:441`）+ hard-delete 已软删的 4170 行 + 删 doc。retrieve 路径 6 个月零流量已证伪「KB insight 检索有价值」。若不想现在动代码，默认 C 但**把 Q3 deadline 钉死**（防再次无限 punt）。◼ 中性。
- **v26_58（is_valid 三态）**：保持 deferred（触发 consumer 未出现，无 bug）。仅更新 doc 修正两处过时事实：① RESET 行号已漂移（实际 `validation.py:754` + `r1b_loop.py:290`，非文档的 `:386`）；② 「所有 caller 用 truthiness」已部分不实（`persistence.py:731/1239/1242/1246` 已用显式 `is False`）。◼ 中性。
- **4 个优化探针脚本未跟踪**：决定 commit 或删除 `_probe_optimization_methodology.py` / `_probe_optimization_retarget.py` / `_probe_marginal_one.py` / `backlog_drain_rank.py`（诊断遗留物，非生产路径）。◼ 中性。
- **FACTOR_LENS(R13) 空转核查**：自 5-20 ON 但 `build_factor_returns_snapshot.py` parquet 无运行证据；若不存在则每 alpha soft-skip（无害但空转烧 CPU）。一次性核查 `/ops/r13/snapshot-stale-check`，要么补建 parquet 要么 flip OFF。◼ 中性。
- **LLM 路由收尾**：per-region override（`resolve_model_for` region 形参 no-op，`llm_service.py:372`）= LOW deferred，无实证需求不做；Phase C runbook + plan 两文档可归档到 `docs/archive/llm_routing/`；MaaS SDK ValidationError in-call 重试（约 5 行）= 自愈中价值低，观望。

---

## 4. 决策日历

| 日期 / Gate | 决策点 | 判据（GO / NO-GO） | 当前态 |
|---|---|---|---|
| **持续 / 实时** | marginal_recon kill-switch 数据 gate | 符号一致率 **≤60% over ≥15 pairs → 停用 offline ΔSharpe 退纯广度**（自动 fail-closed）；>60% & ≥15 → 按 sign 分层 routing | live ≈87.5% / n≈38–42 = **supported / PASS**（端点每次调用实时算，非一次性 milestone）|
| **N1 进行中 → 数周** | forward-test 累积达 ≥15 pairs（predicted↔BRAIN-pre-submit) | 累积到 15 对后 forward 侧给 verdict，可二次印证 offline 代理；**依赖 N1 真提交触发 freeze hook** | **1 对已 freeze**（15816,offline 0.119/BRAIN-pre 0.09）;N1 自动提交持续累积（过去 13 提交大多无法回填）|
| **~2026-07** | BRAIN OS 端点是否暴露 realized 数据 | 任一已提交 alpha 的 `os.osISSharpeRatio` 由 null → 有值 = GO 建 os 轮询接 recon realized 腿;仍 null = 继续挂起 | **blocked**（2026-06-04 实测 1.5mo 前的也 null,/check 不触发,before-after 400）|
| **~2026-07-04 ±5d**（**stale**）| R12 critical-path 决策 | 原 plan：Sprint1 ship +30d obs → evaluator GO/NO-GO/PARTIAL。**但 obs day-0 从未发生**（12794 alpha 0 llm_mode stamp / 5的6 sentinel 0 行）→ 今天跑只会 INSUFFICIENT | **建议 NO-GO / 搁置**；日期标 stale（不再安排日历时间）|
| **优化 Stage A 14d 观察 gate** | Stage A → B 升档转化率 gate | 原 plan：>20% GO / <10% STOP；**已被 0.2% 单点实测短路（46× 偏离 STOP 线）** | **closed-by-STOP**（不再跑 14d cohort）|
| **Orchestrator soak gate** | 翻 `ENABLE_AUTO_ORCHESTRATOR` | 前置：流水线 ≥48h soak 无问题 **且** heartbeat-abort ≥1 周无 false-positive。**两前置均未满足**（48h 被跳过 + 3930 误杀 fix 后 0 soak）| **未过 + 即便过也不优先**（execution-limited 冲突）|
| **Consultant 升级邮件到达**（外部，不可预测）| 翻 `ENABLE_BRAIN_CONSULTANT_MODE` → 同步跨区域 catalog | 收到 BRAIN Consultant 升级邮件 = GO；解锁 L1 跨区域 breadth | blocked-on 外部事件 |

---

## 5. 明确 NO-GO / 搁置（防止重复踩坑）

| 项 | 判定 | 一句理由 |
|---|---|---|
| **多挖 / 自动续挖（orchestrator 翻 flag、Phase 2 bandit、Phase 3 多账号）** | NO-GO / 冻结 | 供给已 66 积压 >> 选择能力（史上仅 13 提交;且 66 积压里 BRAIN 权威 self_corr 下仅个位数真正交可发）,自动多挖只把供给推得更远超,放大错误轴 |
| **纯设置扫掠优化（Stage A 自动 beat + Stage B/C 升档）** | NO-GO | 实测 0.2% 转化 = plan 自己 <10% STOP 闸的 46× 偏离；Stage C 还需 CONSULTANT 80 槽 |
| **EVAL 阈值微调（含 turnover-cap 0.4→0.7 全局放宽）** | NO-GO（全局）| delay 不对称内在自洽 + 0.4-0.85 是 PROVISIONAL 降级非 FAIL；改全局 band 影响 394/1426 alpha = 高风险，违「阈值改动致 89%→1%」教训。若必须，只做**优化器专属窄改**（但优化器 beat 已停，无紧迫性）|
| **universe 轮转扩 breadth** | NO-GO | 同-region self-corr<0.7 门 + Grinold 广度=独立下注（换股票池=相关 PnL≈0 breadth）；实证 transfer-harvest 70→4→self-corr 后净新 0。唯一例外 = 已逐个 verify 的 TOPSP500 窄例（RRd2kvJz self-corr 0.31），非通用 lever |
| **深度因子工程 / 集中价量字段** | NO-GO | edge 已挖尽，既杀提交多样性又无增益 |
| **RAG 深层激活（L0/L2/L3 / pgvector P1 / reranker P1.5 / agent 导航 P2）** | NO-GO / LOW | 同线 A/B 实测 category-overlap 对 mean sharpe **零效应**（p=0.91, d=0.023，需 ~29k sims/arm 才读得出）；pgvector 需 ≥10 人日 infra；均 L1 深度杠杆 |
| **CoSTEER 重投入（打开 R1b 流水线 flag / mutate 深链）** | LOW | 历史空转 0/10108 alpha 引用变异假设；打开烧 retry/mutate 成本无净增证据。环闭合 + live 链深已验证 = 基础设施保留，不再投激活 |
| **AlphaGen 式「为组合而搜」+ AST 多样性罚注入 prompt** | LOW | survey 自标 P2 + 「不做」清单；execution-limited 下生成侧增强属低优，需先证 P0/P1 见顶 |
| **R12 LLM assistant-mode 30d obs** | NO-GO / 搁置 | discovery 轴假设；换 LLM 模式不解决 self-corr<0.7 提交门；obs 从未起算，跑也 INSUFFICIENT，消耗稀缺 sim 在错误轴 |

---

## 6. 冲突 / 存疑（供人拍板）

1. **N1「手动 vs 自动提交」—— ✅ 已决（2026-06-04 实施）**：决策 = **上独立的 `auto_submit_tasks` beat 自动提交**（不走优化闭环的恒-queue SubmitPolicy）。配额风险用守门栈 + `per_run_cap=1` + `daily_cap=4` + shadow-先行（已跑 5 beat / 3 would_submit）控制;急停热翻 `ENABLE_AUTO_SUBMIT`。首笔 15816 已真实提交、8 项 check 全 PASS。手动 drain 面板保留作补充。**此条不再是开放冲突,留作决策记录。**
   - **唯一遗留运维项**：见 N1「⚠ 运维必做」—— 定时 beat 自主提交前须重启 worker 加载 `48e7246`(/check 修复),否则定时 beat 仍撞 PENDING。这是**执行步骤**,非战略冲突。

2. **优化闭环该「停」还是「续」**：已基本收口为 STOP。技术负责人判断 = **STOP 自动闭环 + 保留 manual/robustness/drain/recon 工具箱**。无矛盾，但若用户对「彻底封棺 vs 留 default-OFF 半活」有偏好可拍板（建议仅在文档头标 SUPERSEDED，不删代码）。

3. **R12 整体搁置 vs 跑 obs**：技术负责人判断 = 搁置（理由见 L3）。但这是 discovery-vs-execution 的战略级选择，若用户认为 LLM 模式假设仍值得 30d + sim 预算验证，可推翻——请明确知悉这会触发 6 sentinel 级联 OFF 且占用本应给 N1/N2/L1 的 sim 槽。

---

## 附：各开发线收口状态一览

| 开发线 | 状态 | 本计划归属 |
|---|---|---|
| 优化闭环（L1 扫掠 / 边际） | 收口（STOP + 转向产出已 ship）| N1/N2 副产物 + L2 封棺 |
| Breadth（新数据源 / 覆盖 / L2 组合层）| 工具齐备 LIVE，开口=blocked-on-data/decision | N2 验证 + X2/X3 + L1 跨区域 |
| 提交积压抽干 + 边际对账 | recon 闭环已 ship;**自动提交 live + 首笔 15816 已发**;drain 进行中（待重启让定时 beat 自主跑）| **N1 + N2（重心）** |
| 挖掘 Orchestrator | Phase 1 完成，flag OFF no-op | L4 冻结 |
| R12 + Phase 4 deferred | 工具链 ship，obs 从未起算 | L3 搁置 |
| 挖掘质量 RAG + CoSTEER | 修债完成，A/B 零效应 | §5 NO-GO/LOW |
| LLM 路由 + 多厂商 | 收口，b846db1 今天落地 | X1 冒烟 + L6 收尾 |
| v26 活跃 backlog | 未决未实施，无 bug | L6 维护 |
