# docs/ 索引

> **2026-06-07 整理:开发只保留一个主线文档 = [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md)。**
> 所有历史计划/设计/runbook 已移入 [`archive/`](archive/)(可追溯,不删);竞品/架构/调研 reference 留在本目录根;操作输出(scan/audit/backup)留原地。

## 📋 开发主线(唯一)

| 文档 | 说明 |
|---|---|
| **[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)** | **唯一开发主线(2026-06-07)** — 当前态速览 / 战略 / 本轮轨迹 / 持有策略(greenfield 分支 B) / 已交付 / 重启 SOP / NO-GO / 重评触发。历史细节背书见 `archive/`。 |

## 竞品 / 架构 / 调研(reference,留根目录)

| 文档 | 说明 |
|---|---|
| [competitive_analysis_v3_2026-05-26.md](competitive_analysis_v3_2026-05-26.md) | 竞品头部:selection-vs-discovery + BRAIN self-corr<0.7 提交门 + Grinold 广度轴 |
| [competitive_analysis_r14_stop_loss_2026-05-27.md](competitive_analysis_r14_stop_loss_2026-05-27.md) | R14 task stop-loss 在流水线里的适配方法论 |
| [competitive_analysis_v2_2026-05-19.md](competitive_analysis_v2_2026-05-19.md) | 工业 8 家 + 学界 ~25 系统全景 + AIAC gap(部分 flag 注解过时) |
| [industry_alpha_optimization_survey_2026-06-03.md](industry_alpha_optimization_survey_2026-06-03.md) | 业界优化 5 层 + Grinold IR / DSR / PBO / CPCV(robustness 选择层来源) |
| [quant_pipeline_6layer_2026-06-05.md](quant_pipeline_6layer_2026-06-05.md) | 量化 6 层切分 + 各层成熟度(架构全景) |
| [kb_layered_architecture_2026-06-05.md](kb_layered_architecture_2026-06-05.md) | 知识库分层架构全景(写侧闭环 / dormant 脚手架) |
| [rd_agent_alpha_gpt_research_2026-05-16.md](rd_agent_alpha_gpt_research_2026-05-16.md) | RD-Agent CoSTEER + Alpha-GPT 方法学(`agents/core/` 来源) |
| [qlib_alpha_research_2026-05-16.md](qlib_alpha_research_2026-05-16.md) | Qlib + 学术因子库调研(KB seed) |
| [alphagbm_skills_research_2026-05-15.md](alphagbm_skills_research_2026-05-15.md) | AlphaGBM / skills 工程模式调研 |

## 历史归档 [`archive/`](archive/)

2026-06-07 移入 archive/ 根的**计划/设计/runbook**(本主线的详细背书,勿当现行依据):
`dev_plan_greenfield_2026-06-07` / `dev_plan_post_regime_2026-06-07` / `unified_submit_selector_design_2026-06-07` / `pool_native_reward_redesign_2026-06-07` / `frontend_pool_redesign_2026-06-07` / `kb_feedback_redesign_2026-06-06` / `orthogonality_steered_exploration_plan_2026-06-05` / `four_pool_decoupling_plan_2026-06-05` / `phase1{a-e,b,c,d}_*` / `auto_submit_design_2026-06-04` / `optimization_closure_plan_v1` / `orchestrator_plan` / `heartbeat_liveness_redesign` / `rag_knowledge_retrieval_design` / runbook(dataset_bandit / r12 / sprint5 / phase_c_llm_routing / production_canary)/ `flag_lifecycle` / `llm_per_node_model_selection` / `v26_*` / `_doc_triage_report_2026-06-04` 等。

archive/ 下另有按主题分的早期子目录(phase4_plans / pipeline / llm_routing / rca / dataset_bandit / dirty_data / snapshots / llm_benchmarks / db_backups …),均为已完成/已退役复盘。

## 外部参考
- [`SourceMaterials/`](SourceMaterials/) — BRAIN 平台示例代码等外部输入。

> 脚本生成的带日期产物(`backlog_drain_*` / `dirty_data_scan_*` / `llm_alpha_quality_benchmark_*` / `transfer_harvest_*`)已在 `.gitignore`,不污染顶层。
