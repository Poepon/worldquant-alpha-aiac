# AIAC 平台 × 量化研究流程 6 层切分

> 生成日期：2026-06-05
> 方法：8-agent workflow 并行映射全平台（6 层调查 + 端到端主干追踪 + 对抗性 critic）。**critic agent 直接查了 live Postgres**，人工又复核了承重事实。
> 状态图例：🟢 live ｜ 🟡 部分/受限 live ｜ 🔴 休眠/死代码
> 行号锚点截至 2026-06-05。

---

## 0. ⚠️ 关键前置：config 默认值 ≠ 线上实况（已查 Postgres 核验）

判 live/休眠**必须查 `feature_flag_overrides` + 真实行数**，不能只读 `config.py` 默认。本文档基于 `alpha_gpt@localhost:5433` 实查（2026-06-05）：

| flag / 配置 | config.py 默认 | **线上实况** | 证据 |
|------|:---:|:---:|------|
| `ENABLE_R1A_HOOK` | False | **true** | `r1a_attribution_log`=5763，最新 12:06 UTC |
| `ENABLE_LLM_JUDGE` | False | **true** | ~5474 行带 r5_cost |
| `ENABLE_LLM_MUTATE_ALPHA` | False | **true** | r1b MUTATE 臂活 |
| `ENABLE_AUTO_SUBMIT` | False | **true** | `auto_submit_audit`=447 |
| `AUTO_SUBMIT_MODE`（.env） | shadow | **live** | 同上 |
| `HYPOTHESIS_CENTRIC_LEVEL`（.env） | 0 | **2** | `hypotheses`=1806（含真实生命周期转移） |
| `ENABLE_HIERARCHICAL_RAG` | False | **true** | `r8_query_log`=1473 |
| `ENABLE_DATASET_VALUE_BANDIT` | False | **true** | mining_weight 加权选集 |
| `ENABLE_R8_QUERY_LOG` | False | **true** | layer_hits L0:0/L1:6261/L2:0/L3:0 |
| `ENABLE_ORTHOGONAL_PROMPT_STEERING` | False | **true 但 code 未部署** | orthogonality_score 0/12977（commit `a020242` 未 push） |
| `ENABLE_OPTIMIZATION_LOOP` | False | **false** | 仍关，手动触发独立 |

**核心后果**：平台有**两套 CoSTEER**——`agents/core/` 的 pickle 版（EvolvingKnowledge/ExperimentTrace，**休眠**，调用恒 `trace=None`）；以及 **DB 支撑的假设中心闭环**（R1a 归因 → r1b RETRY/MUTATE → `hypotheses` 生命周期 → R5 判官），**这套线上每分钟在跑**。模型层是**闭环**，不是开环。

实查行数快照：`alpha_pnl`=3,973,740 / `r1a_attribution_log`=5763 / `r1b_retry_log`=863 / `hypotheses`=1806（ACTIVE651/PROPOSED489/SUPERSEDED290/ABANDONED285/PROMOTED91）/ `auto_submit_audit`=447（skipped442/would_submit3/submitted1/rejected1）/ backlog can_submit-未提交=**67（全 USA）** / 史上提交=**13**。

---

## 1️⃣ 数据层 Data 🟢 ~85%（最强层）

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `adapters/brain_adapter.py` | 单一数据源 + 执行网关（融在一文件） | 🟢 | `:1843 get_datasets` `:1853 get_datafields` `:1907 get_alpha_pnl` `:2230 get_user_alphas` |
| `tasks/sync_tasks.py` | BRAIN→PG 同步（目录 06:00 / alpha 6h） | 🟢 | `:364 sync_datasets` `:736 sync_user_alphas`（内嵌 verdict 逻辑 :959/997） |
| `models/metadata.py` | def + per-(delay,universe) cell_stats | 🟢 | `:14 DatasetMetadata` `:43 DatasetCellStats` `:119 DataFieldCellStats` |
| `services/correlation_service.py` | PnL pickle 缓存 + self-corr 引擎 | 🟢 | `:367 refresh_os_alpha_cache` `:324 compute_max_corr_vs_pool` |
| `models/alpha.py` `AlphaPnl` | alpha_pnl 表（**397万行/3215 alpha**） | 🟢 | `:222 AlphaPnl` |
| `tasks/datafield_prune.py` | 数据质量自愈（拒绝字段标 is_active=False） | 🟢 | `:40 prune_invalid_datafields` |
| `ace_lib.py` / `helpful_functions.py` | 同步参考实现，不被 backend import | 🔴 | — |

**缺口**：无自有 PIT/幸存者偏差/血缘/对账；**只有 `os_pnls_USA.pkl`**，CHN/EUR/HKG/JPN 缺失 → 非美区 self_corr 静默退化 UNKNOWN。

## 2️⃣ 特征层 Feature 🟡 ~45%

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `DatasetCellStats.mining_weight` | bandit 加权选数据集（live 杠杆） | 🟢 | `mining_tasks.py:1209` 读 / `:1326 weighted_choice` |
| `agents/services/rag_service.py` + `hierarchical_rag.py` | 模式检索条件化（**只 L1 命中**） | 🟡 | `rag_service.py:419 query`；`hierarchical_rag.py:653 layer1_pillar`（L0:392/L3:464/L2:916 休眠） |
| `alpha_semantic_validator.py` | 算子/字段合法性门（每轮 pre-sim） | 🟢 | `:826 validate` `:148 OperatorRegistry` |
| `knowledge_extraction.py` | AST 骨架/算子链（索引键） | 🟢 | `:188 expression_to_skeleton` `:144 ast_distance` |
| `pillar_classifier.py` / `family_classifier.py` | 五支柱 / 族签名 | 🟢/🟡 | `pillar:260 infer_pillar`；`family:34 family_signature`（L2 消费侧休眠） |
| `agents/field_screener.py` | 在线字段 IC 筛（**唯一类 IC 排序**） | 🔴 死 | `:113 FieldScreener`（零调用者） |
| `agents/seed_pool/field_fitness_stats.py` | per-field fitness 排序 | 🔴 | `:185 get_high_fit_block`（T1 退役后失调用者） |
| `dataset_selector.py` `DatasetSelector` UCB | UCB 选集类 | 🔴 | `:434`（FLAT 无调用者，被 inline `_pick_dataset` 取代） |
| `agents/services/soft_regularizer.py` | 复杂度/原创性软惩罚 | 🔴 shadow | `evaluation.py:1081`（`CODE_GEN_SOFT_REG_MODE=shadow`） |

**缺口**：**无 IC/IR 字段排序**；无特征归一化/winsorize 阶段；无 feature store/PIT 特征矩阵；字段级无共线去重。字段以未排序池交给 LLM。

## 3️⃣ 模型层 Model 🟢 ~70%（**修正：闭环非开环**）

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `nodes/generation.py` | node_hypothesis + node_code_gen（LLM 生成） | 🟢 | `:413 node_hypothesis` `:1523 node_code_gen` |
| `agents/services/llm_service.py` | LLM 路由/熔断/遥测 | 🟢 | `:352 resolve_model_for` `:829 call` |
| `nodes/validation.py` | 语义门 + self-correct | 🟢 | `:69 node_validate` `:535 node_self_correct` |
| `nodes/evaluation.py` | node_simulate(BRAIN) + node_evaluate(score+verdict) | 🟢 | `:1728 brain.simulate_batch` `:390 compute_verdict_from_signals` |
| `alpha_scoring.py` | 复合评分 + 自适应阈值 | 🟢 | `:530 calculate_alpha_score` `:920 evaluate_with_brain_checks` |
| **R1a 归因 + R5 判官 + r1b RETRY/MUTATE + `hypotheses` 生命周期** | **DB 支撑 CoSTEER 闭环（live）** | 🟢 | `evaluation.py:3181 _r1a_on`；`r1b_loop.py:142/434` 读 _r1a_attribution；DB 5763/863/1806 行 |
| `multi_fidelity_eval.py` `RobustnessGate` | 窗口扰动鲁棒性 | 🟡 | `:418 RobustnessGate`（live）/ `:108 MultiFidelityEvaluator`（休眠） |
| `agents/core/`（pickle CoSTEER） | EvolvingKnowledge/ExperimentTrace | 🔴 | `integration.py enhance_existing_node_evaluate`（**trace=None**） |
| `genetic_optimizer.py` | island GA | 🔴 | `:1279 run_genetic_optimization`（无生产调用，仅窗口扰动被 RobustnessGate 用） |
| `agents/llm_mutate_alpha.py` | LLM 包装变异 | 🟢 | flag ON（r1b MUTATE 臂） |

**缺口**：无 CV/CPCV/purged k-fold；**无 DSR/PBO**（万级表达式多重检验无统计去通胀）；无 ensemble/组合 meta-model；无自有回测器（全在 BRAIN）。

## 4️⃣ 组合层 Portfolio 🟡 ~40%（最大教科书缺口）

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `services/correlation_service.py` | self-corr vs 已提交池 | 🟢 | `:432 calc_self_corr` `:324 compute_max_corr_vs_pool` |
| `nodes/evaluation.py` self_corr 硬门 | 准入门（fresh 多 UNKNOWN→只到 PROVISIONAL） | 🟡 | `:448 self_corr gate` `:469 self_corr_acceptable` |
| `marginal_analysis.py` | 多维边际贡献打分卡 SUBMIT/NEUTRAL/SKIP | 🟢 | `:125 analyze_marginal_contribution` `:236 margin 经济门` |
| `marginal_drain.py` | 贪心最远点正交抽干排序 | 🟢 | `:308 greedy_orthogonal_order` `:168 marginal_delta_sharpe` |
| `marginal_recon.py` | killswitch（离线 vs BRAIN 符号一致） | 🟢 | `:50 route_on_sign_verdict` `:38 KILL=0.60` |
| `services/capacity_estimator.py` | 容量（第 5 评分维，非仓位约束） | 🟢 | `:158 estimate`；`alpha_scoring.py:373` |
| `submitted_pool_profile.py` | 组合画像 prompt nudge | 🔴 | flag ON 但 code 未部署 |
| `diversity_tracker.py` | 生成侧新颖度记忆 | 🔴 死 | `:155 DiversityTracker`（零调用者） |

**缺口（最该补）**：**无权重优化器**（Markowitz/HRP/BL）、**无组合级风险模型/因子暴露中性化**、无换手感知组合。每 alpha **1:1 独立提交**，BRAIN 合并。正交只是贪心非 `IR=IC·√BR`。

## 5️⃣ 交易层 Trading 🟡 接线~75% / 有效~5%（**真瓶颈**）

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `services/alpha_service.py` `submit_alpha` | 不可逆提交 + fail-closed 守门栈 | 🟢 | `:462 submit_alpha` `:617 /check self_corr 门` `:695 PROD-corr` |
| `tasks/auto_submit_tasks.py` | auto-submit beat（**mode=live**，涓流） | 🟢 | `:78 run_auto_submit_cycle` `:163 _run_region` |
| `auto_submit_selector.py` | G3-G9 fail-closed 候选选择 | 🟢 | `:29 compute_auto_submit_candidates` `:202 evaluate_guard_stack` |
| `services/optimization/` | 设置扫掠优化闭环（**永不自动提交→backlog**） | 🟢 | `service.py:84 run_one_cycle`；`submit_policy.py:17`(queue-only)；`robustness.py:114` SR0/plateau |
| `tasks/optimization_tasks.py` | 6h beat（flag OFF）+ 手动蓝本触发（独立） | 🟡 | `:75 run_optimization_cycle`（OFF）`:256 run_manual_optimization_cycle` |
| `services/brain_role_switch_service.py` | Consultant 模式切换 | 🔴 | `:57 activate`（`ENABLE_BRAIN_CONSULTANT_MODE` 默认 OFF） |

**真瓶颈**：**67 can_submit（全 USA）未提交 vs 史上仅 13 提交**；auto-submit 447 审计只提 1。瓶颈是提交闸不是上游产能（印证 execution-limited）。
**缺口**：无滑点/成交/TCA（设计如此）；**无实盘已实现 PnL 反馈**（提交后 before-and-after 返 400、os.osISSharpeRatio 全 null）→ 飞行盲。

## 6️⃣ 监控层 Monitoring 🟢 ~70%

| 组件 | 角色 | 状态 | 锚点 |
|------|------|:---:|------|
| `routers/ops.py`（~70 端点） | 监控台 + flag 审计 + 熔断 + CoSTEER deploy-gate | 🟢 | `:313 list_flags` `:1035 brain_auth_circuit` `:5271 marginal_reconciliation` |
| `services/dashboard_service.py` + SSE | 吞吐 KPI + live-feed（2s 轮询 trace） | 🟢 | `dashboard.py:170 live_feed` |
| `services/alpha_health_service.py` | 0-100 stale/drift/orphan（只读建议） | 🟢 | `:484 run_full_check`（08:00 beat） |
| `tasks/canary_redflag.py` | 5 SQL 红旗（只 ERROR-log 不自动回退） | 🟢 | `:24 RED_FLAGS` `:91 check_redflags` |
| `marginal_recon.py` | killswitch（监控读 + 执行控制双重身份） | 🟢 | `:50 route_on_sign_verdict`（`ops.py:4856` 消费） |
| `services/feature_flag_service.py` | 运行时 flag 可观测 + 审计 | 🟢 | `:61 SUPPORTED_FLAGS` |
| `tasks/orchestrator.py` | 自动编排（beat 跑但 no-op） | 🔴 | `ENABLE_AUTO_ORCHESTRATOR` 默认 OFF |
| `experiment_tracker.py` / `metrics_tracker.py` 文件日志 | A/B 脚手架 / .cursor 日志 | 🔴/🟡 | 脚本用，未接 live 看板 |

**缺口**：**无对已提交 alpha 的实盘衰减/PnL 漂移告警**；告警 log-grep 非 paged SLA；无 TSDB/Prometheus；健康分只读不强制；无 live-book 风险/暴露监控。

---

## 7. 端到端主干（已核验）

```
[数据]同步 BRAIN 目录+alpha_pnl(397万) → [特征]mining_weight 选集+L1 RAG+语义门(未排序字段池)
 → [模型]hyp→codegen→validate→simulate(BRAIN)→evaluate
        ⟲ R1a 归因→r1b RETRY/MUTATE→hypotheses 生命周期→R5 判官  (DB 支撑闭环·live)
 → [组合]self_corr 门+边际打分卡+正交抽干 (无权重优化/风险模型)
 → [交易]auto-submit mode=live 但涓流(1/447) ← 真瓶颈
 → [监控]健康/canary/killswitch (只告警不执行, 无实盘反馈)
```

主干完整、闭环成立；但**提交闸涓流 + 实盘反馈腿结构性缺失**。

## 8. 跨层最大缺口（按杠杆排序）

1. **组合构建缺失**（最大教科书缺口）：无权重优化/风险模型/换手净额。给定 67 USA backlog，"选哪个子集组成分散化 book"正是未解问题。
2. **提交瓶颈**：真约束在不可逆提交闸；往上游加力瞄错墙。杠杆=抽干 backlog（人审吞吐 + killswitch 取信后放宽 auto-submit cap）。
3. **无实盘 PnL 闭环**：已实现 OS 腿结构性不可得 → CoSTEER 闭环优化的全是回测指标、零实盘 ground truth。
4. **无 DSR/PBO/CPCV**：万级表达式选择偏差巨大却无统计去通胀。
5. **区域退化**：os_pnls 仅 USA → 4 区 self_corr 退化 + backlog 全 USA = 事实单区。

## 9. 边界问题（组件跨层）

- `brain_adapter.py` = 数据源 + 执行网关（融在一文件）
- `AlphaService` = god-service（读/统计/学习/提交/优化蓝本一锅）
- `self_corr@0.7` = 模型评估门 + 组合准入门（同阈值跨层）
- `node_simulate` 内嵌执行/成本控制（dedup/Q10 预筛/槽配额）
- `marginal_recon` = 监控读 + 执行 routing 控制双重身份
- **R1a 归因写在 model 层却驱动 generation 层 r1b** = 闭环缝合点

## 附录：复核入口

- flag 实况：`psycopg2.connect(host=localhost,port=5433,dbname=alpha_gpt)` → `select flag_name,flag_value from feature_flag_overrides`
- 行数：`r1a_attribution_log` / `r1b_retry_log` / `hypotheses`(group by status) / `alpha_pnl` / `auto_submit_audit`(group by outcome)
- backlog：`select count(*) from alphas where can_submit and date_submitted is null`
- 相关记忆：`reference_kb_architecture_dormant_scaffolding_2026_06_05`（顶部有本次 live-DB 更正）、`project_rdagent_costeer_loop_closure_2026_05_22`、`project_optimization_methodology_refuted_execution_limited_2026_06_03`、`reference_competitive_analysis_v3_2026_05_26`。
