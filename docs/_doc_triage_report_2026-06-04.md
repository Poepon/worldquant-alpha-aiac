# docs/ 文档库整理报告（2026-06-04）

> 由多 agent workflow（21 个 agent，逐篇对照活代码 + git log + CLAUDE.md 核实）生成。
> 工作脚本：`docs/_doc_triage_workflow.js`（可重跑）。本文件与该脚本均为临时工作产物，整理完成后可删。

## 一、总体诊断

逐篇核实 70 篇顶层文档后，分布如下：

| 去向 | 数量 |
|---|---|
| **KEEP（留顶层活跃区）** | 17 篇 .md（+ `SourceMaterials/`） |
| **ARCHIVE（移入 docs/archive/）** | 48 篇 .md（+ 4 个目录） |
| **DELETE（可删，近重复/空快照）** | 3 篇 .md（全 untracked） |
| **UNCERTAIN（待人工）** | 2 篇 .md |

**核心结论**：docs/ 顶层已严重「沉积」——约四分之三是已完成/已退役/被取代的一次性历史文档（plan 完成报告、RCA、spike、版本号 backlog、脏数据快照），只有约 1/4 是描述今天活系统的现行文档。最大的误导源是 **2026-05-18/19 tier/cascade big-bang 退役** 与 **2026-05-29 串行→流水线迁移**：大量 2026-05 中旬文档以 T1/T2/T3 tier、CONTINUOUS_CASCADE、串行轮为前提，今天读会被带向已删架构。

**最该先做的 3 件事**：

1. **删 3 个无价值快照**：`dirty_data_scan_1900`（含 SQL bug 的近重复）、`backlog_drain_0528`（空表早期快照）、`llm_revert_smoke_2026-06-01`（脚本自动覆写的空数据）——全部 untracked，删除零风险。
2. **批量归档 48 篇历史文档** 到 `docs/archive/` 对应子目录（已有 phase1/phase2/phase3_readiness/spike 归档惯例可直接复用），把顶层清到只剩 ~17 篇活文档 + 一份 `docs/INDEX.md`。
3. **修 3 篇会被直接验伪的状态文档**：`README.md`（引用已删的 AlphaLab 页、把 shim 当真实文件、Roadmap 标错）、`backend/REFACTORING_STATUS.md`（「待改」router 重构实际已全部完成）、`backend/CODE_STATUS.md`（定格在 2026-01 重构态，漏掉之后全部主线）。这三篇不在 70 篇 docs/ 内但 CLAUDE.md 引用，误导性最高。

---

## 二、按去向分组

### KEEP — 活跃区（17 篇，描述现行系统或持久外部参考）

| 文件 | 状态 | 理由 |
|---|---|---|
| competitive_analysis_v2_2026-05-19.md | REFERENCE | 工业 8 家+学界 25 系统全景，外部调研持久；§4.2 内部 flag 注解轻微过时 |
| competitive_analysis_v3_2026-05-26.md | CURRENT | 竞品链当前头部；selection-vs-discovery + BRAIN self-corr 模型，核心 fact 仍准 |
| competitive_analysis_r14_stop_loss_2026-05-27.md | REFERENCE | R14 在流水线里的 stop-loss 方法论 reference，前提仍成立 |
| alphagbm_skills_research_2026-05-15.md | REFERENCE | AlphaGBM/skills 工程模式调研，方法论持久 |
| qlib_alpha_research_2026-05-16.md | REFERENCE | Qlib+学术因子库调研，外部知识持久 |
| rd_agent_alpha_gpt_research_2026-05-16.md | REFERENCE | RD-Agent/Alpha-GPT 架构方法学，agents/core 设计来源 |
| industry_alpha_optimization_survey_2026-06-03.md | REFERENCE | 业界优化 5 层模型+Grinold/DSR/PBO，最新，已驱动 commit |
| heartbeat_liveness_redesign_2026-06-03.md | CURRENT | 现行 heartbeat 活系统设计，已 ship（c07a1ea），代码逐条吻合 |
| optimization_closure_plan_v1_2026-05-28.md | CURRENT | Stage A 已 ship、B/C 待 14d GO gate，活路线图 |
| orchestrator_plan_2026-05-29.md | CURRENT | Phase 1 已 ship、Phase 2 待 soak，活路线图 |
| rag_knowledge_retrieval_design_2026-05-21.md | CURRENT | P0 已 ship，准确描述现行 RAG + 活跃 roadmap |
| dataset_bandit_acceptance_runbook.md | CURRENT | 准确描述运行中的 ENABLE_DATASET_VALUE_BANDIT 系统 |
| r12_obs_rollout_checklist.md | CURRENT | R12 决策（7/4±5d）仍 pending，当下应执行的 playbook |
| sprint5_r12_decision_runbook.md | CURRENT | R12 三路条件清理 runbook，决策日未到 |
| phase_c_llm_routing_ab_runbook_2026-05-30.md | REFERENCE | 单 node A/B 通用流程仍可复用（示例模型需加注记） |
| v26_38_39_field_insight_deprecation.md | CURRENT | enum/write-gate 状态与代码完全一致，A/B/C 决策仍未做 |
| v26_58_is_valid_tristate_backlog.md | CURRENT | state.py 三态定义不变，仍是准确的待办观察项 |

### ARCHIVE — 移入 docs/archive/（48 篇 + 4 目录）

| 文件 | 状态 | 理由 |
|---|---|---|
| phase4_a_b_plan_2026-05-19.md (v1) | SUPERSEDED | 未 review 草稿，被 v2 显式归档 |
| phase4_a_b_plan_v2_2026-05-19.md | SUPERSEDED | 防御性版，决策被 v3 全推翻 |
| phase4_a_b_plan_v3_2026-05-19.md | SUPERSEDED | 工时被 v4 判低估 50% |
| phase4_a_b_plan_v4_2026-05-19.md | SUPERSEDED | 12 项执行漏洞被 v5 修正 |
| phase4_a_b_plan_v5_2026-05-19.md | HISTORICAL_DONE | 链尾终版，14 PR 全已 ship |
| competitive_analysis_ai_alpha_mining_2026-05-17.md (v1) | SUPERSEDED | cascade 孤例立论已随 tier 退役失效，被 v2 取代 |
| phase1_ab_report_2026-05-05_p1_threshold.md | SUPERSEDED | 早期 A/B 快照，被同日 completion 取代 |
| phase1_completion_2026-05-05.md | HISTORICAL_DONE | Phase 1 收官报告 |
| phase2_architecture.md | REFERENCE(弱) | schema 仍准，但外层流程图预流水线迁移 |
| phase2_completion_2026-05-06.md | HISTORICAL_DONE | Phase 2 完成报告，默认 LEVEL=0 |
| phase2_implementation_plan.md | SUPERSEDED | 被 completion 取代 |
| phase3_evaluation_2026-05-06.md | STALE | 建立在 tier/串行/DeepSeek 前提上，Phase 3 从未实施 |
| rca_2026-05-13_can_submit_iqc_gap.md | HISTORICAL_DONE | V-23.A/E 已 ship |
| rca_2026-05-13_v25_hypothesis_orphan.md | HISTORICAL_DONE | V-25.B/C 已 ship |
| rca_2026-05-14_v27_1_cascade_lock_race.md | STALE | cascade 锁机制已退役，结构上不可能发生 |
| rca_2026-05-14_v27_92_hypothesis_state_machine_dual_track.md | HISTORICAL_DONE | 方案 A 已落库 |
| serial_to_pipeline_migration_plan_2026-05-29.md | HISTORICAL_DONE | Phase C 删串行已执行 |
| sim_pipeline_impl_plan_2026-05-27.md | HISTORICAL_DONE | Sub-phase 0/1/2 全 ship |
| delay0_sim_pipeline_design_2026-05-27.md | HISTORICAL_DONE | 流水线原始设计已落地 |
| per_function_llm_routing_plan_2026-05-29.md | HISTORICAL_DONE | PR1-5 全 ship；§3 选型表已被 7034050 回退（陷阱） |
| dataset_steering_bandit_plan_2026-05-22.md | REFERENCE | Tier A 已 ship；Tier B 未建 |
| dataset_selection_audit_plan_2026-06-01.md | STALE | 自带作废标记，H1-H6 全翻案 |
| dirty_data_cleanup_proposal_2026-05-28.md | HISTORICAL_DONE | 已由清理脚本逐条执行 |
| dirty_data_scan_2026-05-28_1902.md | HISTORICAL_DONE | 清理前基线快照（被 proposal 引用） |
| dirty_data_scan_2026-05-28_1908.md | HISTORICAL_DONE | 清理后验证快照 |
| v22_series_report.md | STALE | tier+composite 已物理删除；含 pk=7810 真实提交记录 |
| v26_11_kb_isolated_session_backlog.md | HISTORICAL_DONE | record_* 已由 V-27.93 隔离 |
| v26_1_6_cascade_sigkill_backlog.md | STALE | CONTINUOUS_CASCADE 已退役 |
| v26_52_blocked_fields_audit.md | STALE | composite_fields 模块已删除 |
| v27_backlog.md | STALE | A-D 闭环+open 项被 cascade 退役吞掉 |
| quality_review_mining_task_2026-05-13.md | SUPERSEDED | V-26 系列，被 5-14 V-27 吸收 |
| quality_review_mining_task_2026-05-14.md | HISTORICAL_DONE | V-27 系列，所审子系统多已退役 |
| code_review_v27_fixes_2026-05-14.md | HISTORICAL_DONE | V-27 修复 review，已收尾 |
| ops_dashboard_guide.md | STALE | 9 页/28 端点严重低估实际 28 页/82 端点 |
| g9_portfolio_spike_report_2026-05-20.md | HISTORICAL_DONE | 决策被遵守，Phase 5 未启动 |
| r13_brain_sim_daily_pnl_spike_report_2026-05-20.md | HISTORICAL_DONE | 结论 100% 并入 factor_lens 实现 |
| sprint0_baseline_spike_2026-05-19.md | HISTORICAL_DONE | 校准数字被 config.py 逐字引用 |
| datafields_snapshot_v1.md | STALE | 2026-05-03 冻结快照，schema 已规范化 |
| operators_snapshot_v1.md | STALE | 2026-05-03 冻结快照，live 计数已漂移 |
| L1_dedup_tuning_guide.md | STALE | 80% knob 已孤儿化，框在退役 tier round 上 |
| backlog_cascade_stuck_t2.md | STALE | cascade 列已 DROP |
| backlog_drain_2026-05-26_0530.md | HISTORICAL_DONE | 完整 USA 抽干快照，有复盘价值 |
| backlog_iqc_submission.md | HISTORICAL_DONE | before-and-after API 已全栈落地 |
| phase15_task_schema_refactor_plan.md | STALE | 核心 tier/cascade 前提已退役 |
| plan_inverted_hypothesis.md | STALE | 从未实现（0 代码），Phase 0 即搁置 |
| aqr_kelly_seed_2026-05-20.md | HISTORICAL_DONE | seed+代码已 ship |
| retrospective_p012_2026-05-16.md | HISTORICAL_DONE | 19 commit 全落库，部分 tier 内容已退役 |
| master_implementation_plan_2026-05-17.md | STALE | 256KB 巨型路线图，cascade 双轨前提次日即被推翻 |
| 目录：portfolio_theme/ · v26_retrospective/ · cell_stats_cutover_backup_2026-05-26/ · phase16a_cutover_backup_2026-05-29/ | — | 一次性分析 / DB 回滚备份，cutover 已稳定 |

### DELETE — 可删（3 篇，全 untracked，零风险）

| 文件 | 理由 |
|---|---|
| dirty_data_scan_2026-05-28_1900.md | 含 A5 SQL bug 的近重复，被 2 分钟后的 1902 完全取代 |
| backlog_drain_2026-05-26_0528.md | 空/不完整早期快照，被 2 分钟后的 0530 完全覆盖 |
| llm_revert_smoke_2026-06-01.md | 脚本每 5min 自动覆写的空数据（total 0），结论已沉淀在 commit/config/memory（删前先停 `_watch_llm_revert_smoke.py`，否则重生） |

### UNCERTAIN — 待人工（2 篇，都倾向 KEEP）

| 文件 | 为何待人工 |
|---|---|
| production_canary_sop_2026_05_18.md | 机制本体（回滚 SQL/升级树/双窗口）仍可用，但 §1 含已退役的 ENABLE_DAG_TRACE + tier 期 schema 措辞。**人工决定**：轻量更新后 KEEP，还是直接 ARCHIVE。 |
| flag_lifecycle.md | 三层框架/退役清单全准确，唯一滞后是 flag inventory 未追加 2026-05-20 后的新批次（ENABLE_OPTIMIZATION_LOOP、LLM 路由 flag）。建议 KEEP + 小幅追加。 |

---

## 三、跨簇取代链

- **链 1 Phase 4 plan v1→v5**（5 篇）：严格线性，文件头自述。留 v5（已 ship 的历史终版），v1-v4 全归档（superseded_by=v5）。
- **链 2 竞品分析 v1→v3 + r14**（4 篇）：v1 归档（cascade 立论失效）；v2/v3/r14 **互补并存** KEEP（v3 是窄专题，未涵盖 v2 的工业/学界全景）。
- **链 3 Phase 1/2 plan→completion**（4 篇）：全归档。
- **链 4 脏数据三份近重复**：1900（含 bug）删；1902（清理前基线）+1908（清理后验证）归档作前-后对照。
- **链 5 backlog_drain 两份**（间隔 2 分钟）：0528 删；0530 归档。
- **链 6 流水线 设计→实施→退役**（3 篇）归档；heartbeat_redesign（06-03）KEEP。
- **链 7 dataset bandit plan→runbook**：runbook KEEP；plan v3 + selection_audit 归档。
- **链 8 quality review V-26→V-27**（3 篇）全归档。
- **master_implementation_plan**：无后继 master plan 收编，内容被碎片化进多份独立 plan → STALE 归档。

---

## 四、docs/ 重组方案

### 4.1 目标顶层结构

```
docs/
├── INDEX.md                 ← 新建：活文档导航 + archive 指引
├── competitive/             ← 竞品/外部调研（7 篇）
├── design/                  ← 现行设计/路线图（heartbeat/rag/optimization_closure/orchestrator）
├── runbooks/                ← 现行运维手册（dataset_bandit/r12_obs/sprint5_r12/llm_routing_ab/flag_lifecycle）
├── backlog/                 ← 仍准确的活 backlog（v26_38_39/v26_58）
├── SourceMaterials/         ← 外部参考（保留）
└── archive/                 ← 历史归档（已存在，新增 phase4_plans/competitive_v1/rca/pipeline_migration/
                                 llm_routing/dataset_bandit/dirty_data/v22_v26_v27/snapshots/misc_plans 子目录）
```

> 顶层从 70 篇压到约 17 篇活文档 + INDEX.md。若团队习惯扁平，也可活文档全留 docs/ 顶层、仅把 48 篇移进 archive/，不建 competitive/design/runbooks 子目录。

### 4.2 自动遥测 / 子目录处理

- **6 个 beat 遥测目录已正确 gitignore**（alpha_health_check / hypothesis_health_check / macro_narratives / negative_knowledge / pillar_balance / regime_state），0 tracked，无需动作。
- **straggler `git rm --cached`**：`docs/llm_op_monitor/2026-05-11.md`、`docs/phase3_readiness/2026-05-06.json`。
- **`docs/iqc_audit/`（10 tracked，非 ignore）**：脚本产物，建议 `git rm --cached` + 加 `.gitignore`。**注意保留 `audit_2026-05-11_1231.json`**（被 `pairwise_self_corr.py` 读取为输入）。
- **`docs/SourceMaterials/`**：真实外部参考（BRAIN 示例代码），KEEP，不要当遥测 gitignore。

### 4.3 行动清单（建议执行顺序 A → B → D → C → E）

- **A. 零风险删除**（3 个 untracked .md；删 llm_revert_smoke 前先停 watcher 脚本）
- **B. git 缓存清理**（2 个 straggler + iqc_audit/ 加 .gitignore，保留 audit_2026-05-11_1231.json）
- **C. 批量归档**（48 篇 + 4 目录，按 4.1 子目录 git mv / move）
- **D. 新建 `docs/INDEX.md`**（17 篇活文档分组导航 + 「历史见 archive/」一句）—— 消除「沉积感」最有效的一步
- **E. 修 3 篇状态文档**（README.md / REFACTORING_STATUS.md / CODE_STATUS.md，不在 70 篇内但 CLAUDE.md 引用，误导性最高）

---

## 五、归档时需附注的「陷阱」

1. **per_function_llm_routing_plan §3 选型表 + canary/runbook 示例** 里的 `hypothesis=dsv4-pro / code_gen=qwen3.6-plus` 已于 2026-06-01（commit 7034050）被 A/B 推翻、回退 kimi-k2.6。归档时在文件头加一行「模型选型以 commit 7034050 + config.py 为准」。
2. **大量 2026-05 中旬文档以 tier/cascade/串行轮为前提**，这些架构已退役。归档目录名带主题即可隐性提示「历史架构」。
3. **v22_series_report 含真实 IQC 提交记录（pk=7810, Δscore=+341）**，有历史价值，归档勿删。
4. **iqc_audit/audit_2026-05-11_1231.json 是活脚本输入**，清理时务必保留。
