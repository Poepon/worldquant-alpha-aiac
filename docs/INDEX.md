# docs/ 索引

> **开发只保留一个主线文档 = [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md)。**
> 竞品/架构/调研 reference 留在本目录根;操作输出(scan/audit/backup)留原地。

## 📋 开发主线(唯一)

| 文档 | 说明 |
|---|---|
| **[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)** | **唯一开发主线(2026-06-07)** — 当前态速览 / 战略 / 本轮轨迹 / 持有策略(greenfield 分支 B) / 已交付 / 重启 SOP / NO-GO / 重评触发。 |

## 竞品 / 架构 / 调研(reference,留根目录)

| 文档 | 说明 |
|---|---|
| [competitive_analysis_v3_2026-05-26.md](competitive_analysis_v3_2026-05-26.md) | 竞品头部:selection-vs-discovery + BRAIN self-corr<0.7 提交门 + Grinold 广度轴 |
| [competitive_analysis_v2_2026-05-19.md](competitive_analysis_v2_2026-05-19.md) | 工业 8 家 + 学界 ~25 系统全景 + AIAC gap(部分 flag 注解过时) |
| [industry_alpha_optimization_survey_2026-06-03.md](industry_alpha_optimization_survey_2026-06-03.md) | 业界优化 5 层 + Grinold IR / DSR / PBO / CPCV(robustness 选择层来源) |
| [quant_pipeline_6layer_2026-06-05.md](quant_pipeline_6layer_2026-06-05.md) | 量化 6 层切分 + 各层成熟度(架构全景) |
| [kb_redesign_unified_2026-06-07.md](kb_redesign_unified_2026-06-07.md) | **知识库架构重设计(统一版,v1+v2 融合,定稿)** — 两份外部理想方案 × 两轮 workflow 对抗审查 × live 实证;两堵墙 / 多 Agent↔池阶段对应 / 三库 NO-GO / 止损期最终架构。**最新,优先读** |
| [kb_layered_architecture_2026-06-05.md](kb_layered_architecture_2026-06-05.md) | 知识库分层架构全景(写侧闭环 / dormant 脚手架)— 旧版背景,以上方重设计为准 |
| [rd_agent_alpha_gpt_research_2026-05-16.md](rd_agent_alpha_gpt_research_2026-05-16.md) | RD-Agent CoSTEER + Alpha-GPT 方法学(`agents/core/` 来源) |
| [qlib_alpha_research_2026-05-16.md](qlib_alpha_research_2026-05-16.md) | Qlib + 学术因子库调研(KB seed) |
| [alphagbm_skills_research_2026-05-15.md](alphagbm_skills_research_2026-05-15.md) | AlphaGBM / skills 工程模式调研 |

> 脚本生成的带日期产物(`backlog_drain_*` / `dirty_data_scan_*` / `llm_alpha_quality_benchmark_*` / `transfer_harvest_*`)已在 `.gitignore`,不污染顶层。
