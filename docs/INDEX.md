# docs/ 索引

> 本目录顶层只保留**仍描述现行系统或具持久参考价值**的活文档。
> 已完成 / 已退役 / 被取代的历史文档已移入 [`archive/`](archive/)（按主题分目录）。
> 最近一次整理:2026-06-04（详见 `_doc_triage_report_2026-06-04.md`）。

## 竞品 / 外部调研（reference）

| 文档 | 说明 |
|---|---|
| [competitive_analysis_v3_2026-05-26.md](competitive_analysis_v3_2026-05-26.md) | 竞品链当前头部:selection-vs-discovery + BRAIN self-corr<0.7 提交门 + Grinold 广度轴 |
| [competitive_analysis_v2_2026-05-19.md](competitive_analysis_v2_2026-05-19.md) | 工业 8 家 + 学界 ~25 系统全景 + AIAC 5 gap（Phase 4 路线源；§4.2 flag 注解部分过时） |
| [competitive_analysis_r14_stop_loss_2026-05-27.md](competitive_analysis_r14_stop_loss_2026-05-27.md) | R14 task stop-loss 在 producer-consumer 流水线里的适配方法论 |
| [industry_alpha_optimization_survey_2026-06-03.md](industry_alpha_optimization_survey_2026-06-03.md) | 业界优化 5 层模型 + Grinold IR / DSR / PBO / CPCV |
| [rd_agent_alpha_gpt_research_2026-05-16.md](rd_agent_alpha_gpt_research_2026-05-16.md) | RD-Agent CoSTEER + Alpha-GPT 架构方法学（`agents/core/` 设计来源） |
| [qlib_alpha_research_2026-05-16.md](qlib_alpha_research_2026-05-16.md) | Qlib + 学术因子库调研（KB seed 来源） |
| [alphagbm_skills_research_2026-05-15.md](alphagbm_skills_research_2026-05-15.md) | AlphaGBM / skills 工程模式调研 |

## 现行设计 / 活路线图

| 文档 | 说明 |
|---|---|
| [heartbeat_liveness_redesign_2026-06-03.md](heartbeat_liveness_redesign_2026-06-03.md) | 流水线 per-coroutine heartbeat 活系统设计（已 ship c07a1ea） |
| [rag_knowledge_retrieval_design_2026-05-21.md](rag_knowledge_retrieval_design_2026-05-21.md) | 现行 RAG 检索分层设计 + roadmap |
| [optimization_closure_plan_v1_2026-05-28.md](optimization_closure_plan_v1_2026-05-28.md) | 优化闭环:Stage A 已 ship,B/C 待 14d GO gate |
| [orchestrator_plan_2026-05-29.md](orchestrator_plan_2026-05-29.md) | 挖掘 orchestrator:Phase 1 已 ship,Phase 2 待 soak |

## 运维手册 / Runbook

| 文档 | 说明 |
|---|---|
| [dataset_bandit_acceptance_runbook.md](dataset_bandit_acceptance_runbook.md) | `ENABLE_DATASET_VALUE_BANDIT` 验收 / 运维 |
| [r12_obs_rollout_checklist.md](r12_obs_rollout_checklist.md) | R12 决策（约 2026-07-04±5d）前的观测 checklist |
| [sprint5_r12_decision_runbook.md](sprint5_r12_decision_runbook.md) | R12 三路条件清理 runbook |
| [phase_c_llm_routing_ab_runbook_2026-05-30.md](phase_c_llm_routing_ab_runbook_2026-05-30.md) | 单 node LLM A/B 流程（⚠ 示例模型选型已被 commit `7034050` 回退 kimi-k2.6,以 config.py 为准） |
| [flag_lifecycle.md](flag_lifecycle.md) | feature flag 生命周期约定（框架准确;flag 清单待追加 2026-05-20 后新批次） |
| [production_canary_sop_2026_05_18.md](production_canary_sop_2026_05_18.md) | 生产灰度 / 回滚 SOP（⚠ §1 含已退役的 `ENABLE_DAG_TRACE` / tier 期措辞,机制本体仍可用） |

## 仍准确的活 backlog

| 文档 | 说明 |
|---|---|
| [v26_38_39_field_insight_deprecation.md](v26_38_39_field_insight_deprecation.md) | field-insight enum / write-gate 现状 + 待定 A/B/C 决策 |
| [v26_58_is_valid_tristate_backlog.md](v26_58_is_valid_tristate_backlog.md) | `is_valid` 三态待办观察项 |

## 外部参考材料

- [`SourceMaterials/`](SourceMaterials/) — BRAIN 平台示例代码等外部参考输入。

---

## 历史归档 `archive/`

已完成 / 已退役 / 被取代的文档按主题归档（多数 2026-05 中旬文档以 **已退役的 tier/cascade/串行轮** 为前提,仅作复盘,勿当现行依据):

| 子目录 | 内容 |
|---|---|
| `phase4_plans/` | Phase 4 A+B 计划 v1–v5（终版 v5 已全部 ship） |
| `phase1/` `phase2/` `phase3_readiness/` | Phase 1/2/3 早期完成报告 / 架构 / 评估 + 逐日 A/B |
| `rca/` | 4 篇根因分析（部分涉及已退役 cascade 锁） |
| `pipeline/` | 串行→流水线 设计 / 实施 / 迁移（已落地） |
| `llm_routing/` | per-function LLM 路由 plan（PR1-5 已 ship） |
| `dataset_bandit/` | bandit steering plan + 已作废的 selection audit |
| `dirty_data/` | 脏数据清理提案 + 前/后扫描快照 |
| `v22_v26_v27/` | 旧版本号 backlog / report / quality review（体系已弃） |
| `competitive/` | 竞品分析 v1（被 v2 取代） |
| `spike/` | 技术验证 spike 报告 |
| `snapshots/` | datafields/operators 冻结快照 + backlog 抽干快照 |
| `misc_plans/` | master_implementation_plan、phase15 schema、inverted-hypothesis 等已退役/未实施大 plan |
| `cutover_backups/` | DB cutover 回滚备份（cell_stats / phase16a） |
| `portfolio_theme/` `v26_retrospective/` `v22_5_backfill/` | 一次性分析 / 复盘 |
