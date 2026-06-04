export const meta = {
  name: 'doc-triage',
  description: '分类 docs/ 下 70 篇文档:现行/参考/被取代/历史/过时,并产出重组与清理方案',
  phases: [
    { title: 'Classify', detail: '每个主题簇一个 agent,读全文并对照活代码/git 核实状态' },
    { title: 'Synthesize', detail: '跨簇取代链 + 按状态分组 + docs/ 重组与清理行动清单' },
  ],
}

// 今天 = 2026-06-04(脚本内 Date.now() 不可用,硬编码给 agent 判新旧)
const TODAY = '2026-06-04'

// 每篇文档恰好归入一个簇。clusters 覆盖全部 70 个顶层 docs/*.md。
const CLUSTERS = [
  { name: 'phase4-plans', docs: [
    'docs/phase4_a_b_plan_2026-05-19.md', 'docs/phase4_a_b_plan_v2_2026-05-19.md',
    'docs/phase4_a_b_plan_v3_2026-05-19.md', 'docs/phase4_a_b_plan_v4_2026-05-19.md',
    'docs/phase4_a_b_plan_v5_2026-05-19.md',
  ], hint: '同一 plan 的 v1→v5 迭代。判定哪个是最终 ship-candidate、其余是否 SUPERSEDED。core CLAUDE.md/memory 指 v5 是 ship-ready 终版。核实 v5 描述的 R11/R12/R13/R14/PR 是否已落地(git log + config flag)。' },

  { name: 'competitive-analysis', docs: [
    'docs/competitive_analysis_ai_alpha_mining_2026-05-17.md',
    'docs/competitive_analysis_v2_2026-05-19.md',
    'docs/competitive_analysis_v3_2026-05-26.md',
    'docs/competitive_analysis_r14_stop_loss_2026-05-27.md',
  ], hint: '竞品分析 v1→v2→v3 链 + r14 专题分支。竞品分析多为 REFERENCE(时效性弱),但旧版可能被新版 SUPERSEDED。判断 v1/v2 是否被 v3 完全涵盖。' },

  { name: 'early-phases-1-2-3', docs: [
    'docs/phase1_ab_report_2026-05-05_p1_threshold.md', 'docs/phase1_completion_2026-05-05.md',
    'docs/phase2_architecture.md', 'docs/phase2_completion_2026-05-06.md',
    'docs/phase2_implementation_plan.md', 'docs/phase3_evaluation_2026-05-06.md',
  ], hint: 'Phase 1/2/3 早期(5月初)完成报告/架构/计划/评估。completion/evaluation 类多为 HISTORICAL_DONE;phase2_architecture 若描述仍现行架构可能 CURRENT/REFERENCE,但注意 tier 系统已于 2026-05-19 退役、串行已于 2026-05-29 退役——核实是否 STALE。' },

  { name: 'rca', docs: [
    'docs/rca_2026-05-13_can_submit_iqc_gap.md', 'docs/rca_2026-05-13_v25_hypothesis_orphan.md',
    'docs/rca_2026-05-14_v27_1_cascade_lock_race.md',
    'docs/rca_2026-05-14_v27_92_hypothesis_state_machine_dual_track.md',
  ], hint: 'RCA 根因分析,本质是一次性历史记录(HISTORICAL_DONE),保留作复盘价值。注意 cascade_lock_race 涉及已退役的 cascade 系统——其描述的机制已 STALE,但作为历史 RCA 可 ARCHIVE。' },

  { name: 'external-research', docs: [
    'docs/alphagbm_skills_research_2026-05-15.md', 'docs/qlib_alpha_research_2026-05-16.md',
    'docs/rd_agent_alpha_gpt_research_2026-05-16.md',
  ], hint: '外部技术调研(AlphaGBM/Qlib/RD-Agent+Alpha-GPT)。多为 REFERENCE(时效性弱、塑造了系统设计哲学)。判断是否仍有参考价值。' },

  { name: 'pipeline-heartbeat', docs: [
    'docs/delay0_sim_pipeline_design_2026-05-27.md', 'docs/heartbeat_liveness_redesign_2026-06-03.md',
    'docs/serial_to_pipeline_migration_plan_2026-05-29.md', 'docs/sim_pipeline_impl_plan_2026-05-27.md',
  ], hint: '生产者-消费者流水线 + heartbeat 设计。流水线已于 2026-05-29 (Phase C) 落地、串行已退役。migration_plan/impl_plan 是否已完成→HISTORICAL_DONE?heartbeat_liveness_redesign 2026-06-03 最新,可能 CURRENT。核实 agents/pipeline/ 现状 + mining_tasks.py 的 _pipeline_heartbeat_timeout。' },

  { name: 'llm-routing', docs: [
    'docs/per_function_llm_routing_plan_2026-05-29.md', 'docs/phase_c_llm_routing_ab_runbook_2026-05-30.md',
    'docs/llm_revert_smoke_2026-06-01.md',
  ], hint: 'per-function LLM 路由 plan + A/B runbook + revert smoke。路由 PR1-5 已 ship,默认模型已由 deepseek 改 kimi-k2.6 (2026-06-01)。plan 是否已完成→HISTORICAL_DONE?核实 llm_service.py:resolve_model_for + ENABLE_PER_FUNCTION_LLM_ROUTING。' },

  { name: 'dataset-bandit', docs: [
    'docs/dataset_bandit_acceptance_runbook.md', 'docs/dataset_selection_audit_plan_2026-06-01.md',
    'docs/dataset_steering_bandit_plan_2026-05-22.md',
  ], hint: '数据集导流 bandit。**dataset_selection_audit_plan_2026-06-01 已被显式标记作废(commit 027e350:H1-H6 假设全翻案,真因是 EVAL_PROVISIONAL 阈值)→ STALE/DELETE 候选**,务必读文件确认是否已带「作废」标记。steering_bandit_plan 已实施(ENABLE_DATASET_VALUE_BANDIT)。' },

  { name: 'dirty-data', docs: [
    'docs/dirty_data_cleanup_proposal_2026-05-28.md', 'docs/dirty_data_scan_2026-05-28_1900.md',
    'docs/dirty_data_scan_2026-05-28_1902.md', 'docs/dirty_data_scan_2026-05-28_1908.md',
  ], hint: '脏数据清理提案 + 同日 3 次扫描快照(1900/1902/1908,仅相隔几分钟)。扫描快照是一次性输出(HISTORICAL_DONE),3 份近乎重复→大概率只需留最后一份或全部 ARCHIVE。proposal 是否已执行?核实 scripts/_cleanup_dirty_data_*.py + git log。' },

  { name: 'optimization-orchestrator', docs: [
    'docs/optimization_closure_plan_v1_2026-05-28.md', 'docs/orchestrator_plan_2026-05-29.md',
    'docs/industry_alpha_optimization_survey_2026-06-03.md',
  ], hint: '优化闭环 plan + orchestrator plan + 业界优化调研。两个 plan 均已部分/全部落地(backend/services/optimization/ 存在、tasks/orchestrator.py 存在,flag 默认 OFF)。判断 plan 是已完成(HISTORICAL_DONE)还是仍有未做项(CURRENT)。survey 2026-06-03 最新=REFERENCE。' },

  { name: 'backlogs-v22-v26-v27', docs: [
    'docs/v22_series_report.md', 'docs/v26_11_kb_isolated_session_backlog.md',
    'docs/v26_1_6_cascade_sigkill_backlog.md', 'docs/v26_38_39_field_insight_deprecation.md',
    'docs/v26_52_blocked_fields_audit.md', 'docs/v26_58_is_valid_tristate_backlog.md',
    'docs/v27_backlog.md',
  ], hint: '旧版本号(v22/v26/v27)的 backlog/report/audit,5月13-15日。版本号体系本身已不用。多为 HISTORICAL_DONE 或 STALE。注意 cascade_sigkill 涉及已退役 cascade。逐个判断 backlog 项是否已做或已无关。' },

  { name: 'quality-review', docs: [
    'docs/quality_review_mining_task_2026-05-13.md', 'docs/quality_review_mining_task_2026-05-14.md',
    'docs/code_review_v27_fixes_2026-05-14.md',
  ], hint: '挖掘任务质量评审(5-13 与 5-14,后者 77KB 是大文件)+ v27 code review 修复记录。评审/修复记录是一次性历史(HISTORICAL_DONE)。5-13 是否被 5-14 取代?判断其中结论是否仍指导现行系统。' },

  { name: 'runbooks-sops', docs: [
    'docs/production_canary_sop_2026_05_18.md', 'docs/ops_dashboard_guide.md',
    'docs/r12_obs_rollout_checklist.md', 'docs/sprint5_r12_decision_runbook.md',
  ], hint: 'Runbook/SOP/checklist——这类是「操作手册」,若描述的流程/页面仍现行则 CURRENT 且高价值(KEEP)。ops_dashboard_guide 尤其要核实是否匹配现行 Ops 页面。R12 决策 runbook/checklist 关联 R12 决策日(memory 称 2026-07-04±5d),判断是否仍 pending。' },

  { name: 'spikes', docs: [
    'docs/g9_portfolio_spike_report_2026-05-20.md', 'docs/r13_brain_sim_daily_pnl_spike_report_2026-05-20.md',
    'docs/sprint0_baseline_spike_2026-05-19.md',
  ], hint: 'Spike(技术验证)报告——一次性历史记录(HISTORICAL_DONE)。判断 spike 结论是否已并入正式实现(若是,spike 可 ARCHIVE)。' },

  { name: 'snapshots-reference', docs: [
    'docs/datafields_snapshot_v1.md', 'docs/operators_snapshot_v1.md', 'docs/flag_lifecycle.md',
  ], hint: 'datafields/operators 快照(2026-05-03,_v1)很可能已被 live DB 取代→STALE/过时;flag_lifecycle 描述 feature flag 生命周期约定,若仍现行则 REFERENCE/CURRENT。核实快照内容 vs DB 的 Operator/DataField 表是否还一致。' },

  { name: 'backlogs-misc', docs: [
    'docs/L1_dedup_tuning_guide.md', 'docs/backlog_cascade_stuck_t2.md',
    'docs/backlog_drain_2026-05-26_0528.md', 'docs/backlog_drain_2026-05-26_0530.md',
    'docs/backlog_iqc_submission.md',
  ], hint: '杂项 backlog + L1 去重调参指南 + 提交积压抽干输出。backlog_cascade_stuck_t2 涉及已退役 cascade/T2→STALE。backlog_drain 两份(同日相隔 2 分钟)是抽干输出快照(HISTORICAL_DONE,近重复)。L1_dedup_tuning_guide 若 diversity_tracker 仍用则 REFERENCE。' },

  { name: 'big-misc-plans', docs: [
    'docs/phase15_task_schema_refactor_plan.md', 'docs/plan_inverted_hypothesis.md',
    'docs/rag_knowledge_retrieval_design_2026-05-21.md', 'docs/aqr_kelly_seed_2026-05-20.md',
    'docs/retrospective_p012_2026-05-16.md',
  ], hint: '大型 plan/design + 复盘。phase15 TaskSchema refactor:核实 ENABLE_TASK_SCHEMA_V2 是否已 flip(部分落地?)。rag_knowledge_retrieval_design 对照 rag_service.py + memory(L0/L2/L3 在生产 dormant)。plan_inverted_hypothesis 是否实现?retrospective_p012 是复盘=HISTORICAL_DONE。' },

  { name: 'master-plan', docs: [
    'docs/master_implementation_plan_2026-05-17.md',
  ], hint: '**250KB 巨型总规划(2026-05-17)**。这是最关键的判定:它很可能是个一次性的大蓝图,其中大量内容已被后续 phase4/pipeline/optimization 等独立 plan 取代或已落地。通读结构(可只读章节标题 + 抽样),判断它现在是 CURRENT 路线图、还是已被碎片化的后续 plan 取代的 HISTORICAL/STALE 蓝图。给出它是否还值得作为单一事实来源。' },
]

const ALL_PATHS = CLUSTERS.flatMap(c => c.docs)

const VERIFICATION_HINTS = `
今天是 ${TODAY}。仓库根目录是 worldquant-alpha-aiac(你的 cwd)。

【已知的系统级变更——必须用 git log / Grep / Read 代码去核实,不要盲信】
- Tier 系统(T1/T2/T3 ladder、CONTINUOUS_CASCADE、agent_mode、starting_tier、TIER{1,2,3}_* config)→ 2026-05-19 big-bang 退役。任何围绕 tier/cascade 构建的文档很可能 STALE。
- DAG trace(ENABLE_DAG_TRACE、R6 DAG、watchdog)→ 2026-05-24 退役(commit b72de0d 附近)。
- 串行挖掘路径(_run_one_round_inline、wait_for 整轮)→ 2026-05-29 Phase C 退役,被 agents/pipeline/ 生产者-消费者流水线取代。
- 前端 AlphaLab / FactorLibrary → 2026-05 退役。
- 默认 LLM deepseek-chat → 2026-06-01 改为 kimi-k2.6(commit 7034050);生产端点=阿里云 MaaS,不是 DeepSeek 原厂。
- dataset_selection_audit_plan_2026-06-01 → 已显式作废(commit 027e350,H1-H6 全翻案,真因=EVAL_PROVISIONAL 阈值)。

【状态(status)定义——严格区分】
- CURRENT:仍准确描述现行活系统;开发者今天读它不会被误导。KEEP。
- REFERENCE:时效性弱的参考(外部调研/竞品/方法论);即便系统变了仍有参考价值。KEEP。
- SUPERSEDED:同一主题存在更新版本的文档,本文被其取代。ARCHIVE(在 superseded_by 填取代它的文件路径)。
- HISTORICAL_DONE:一次性历史记录(RCA/completion/spike/scan/review/复盘),所描述的工作已完成;有复盘价值但非活文档。ARCHIVE。
- STALE:描述的是已退役/已大改的系统,今天读会误导。ARCHIVE 或 DELETE。
- UNCERTAIN:需要人来判断。

【recommendation】KEEP(留在 docs/ 顶层活跃区) / ARCHIVE(移入 docs/archive/) / DELETE(可删,无保留价值,如近重复快照) / MERGE(应并入另一文档) / UNCERTAIN。

【核实方法】用 git log --oneline / git log -S / Grep 代码 / Read 关键文件 / 读 CLAUDE.md。evidence 字段要具体:引用 commit 哈希、file:line、config flag 名、或 CLAUDE.md 的表述。confidence 给 high 仅当你真的核实过代码/git。
`.trim()

const DOC_SCHEMA = {
  type: 'object',
  properties: {
    docs: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          title: { type: 'string', description: '一句话说明这是什么文档' },
          category: { type: 'string', enum: ['plan', 'rca', 'retrospective', 'research', 'runbook', 'report', 'snapshot', 'proposal', 'backlog', 'spike', 'design', 'review', 'other'] },
          status: { type: 'string', enum: ['CURRENT', 'REFERENCE', 'SUPERSEDED', 'HISTORICAL_DONE', 'STALE', 'UNCERTAIN'] },
          superseded_by: { type: 'string', description: '若 SUPERSEDED/MERGE,填取代它的文件路径;否则空字符串' },
          recommendation: { type: 'string', enum: ['KEEP', 'ARCHIVE', 'DELETE', 'MERGE', 'UNCERTAIN'] },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          evidence: { type: 'string', description: '为何是这个状态——引用 commit/file:line/flag/CLAUDE.md' },
          summary: { type: 'string', description: '1-2 句内容摘要' },
        },
        required: ['path', 'title', 'category', 'status', 'superseded_by', 'recommendation', 'confidence', 'evidence', 'summary'],
      },
    },
    cluster_notes: { type: 'string', description: '本簇内的取代链/重复/交叉观察' },
  },
  required: ['docs', 'cluster_notes'],
}

phase('Classify')

// 18 个主题簇 + 子目录评估 + 状态文档核查,全部并行(synthesis 需要全部结果 → barrier 合理)。
const clusterThunks = CLUSTERS.map(c => () => agent(
  `你是文档分流分析师。核实并分类以下 ${c.docs.length} 篇文档(主题簇:${c.name})。

待分类文档:
${c.docs.map(d => '  - ' + d).join('\n')}

本簇提示(需核实,非定论):${c.hint}

${VERIFICATION_HINTS}

【全部 70 篇顶层文档清单(用于判断跨文档取代关系)】
${ALL_PATHS.map(p => '  - ' + p).join('\n')}

步骤:① 用 Read 读你负责的每篇文档全文(大文件可读结构+抽样);② 用 git log / Grep / Read 代码核实其描述的系统/计划/flag 在今天是否仍成立;③ 对每篇给出 status + recommendation + 具体 evidence。务必逐篇核实,不要只凭文件名或提示下结论。`,
  { label: `classify:${c.name}`, phase: 'Classify', schema: DOC_SCHEMA }
))

// 子目录评估(自动生成报告 vs 备份 vs 归档)
const subdirThunk = () => agent(
  `你是文档分流分析师。评估 docs/ 下的**子目录**(不是顶层 .md)。今天 ${TODAY}。cwd 是仓库根。

用 Bash(ls/find/stat)+ 抽样 Read + Grep 代码,判断每个子目录属于哪类、是否该保留:
  - docs/alpha_health_check/  docs/hypothesis_health_check/  docs/llm_op_monitor/  docs/macro_narratives/  docs/negative_knowledge/  docs/pillar_balance/  docs/regime_state/  docs/iqc_audit/  docs/portfolio_theme/
    (这些疑似 celery beat 任务自动追加的运行报告/遥测输出。核实:Grep backend/tasks 或 services 看是哪个任务在写、是否仍在写。若是自动输出→不算需人工整理的「文档」,建议保留目录但可考虑加入 .gitignore 或定期清理。)
  - docs/cell_stats_cutover_backup_2026-05-26/  docs/phase16a_cutover_backup_2026-05-29/  (DB 迁移 cutover 备份。判断是否仍需保留。)
  - docs/archive/  (已归档区,30 文件。确认它就是归档,本次重组应把新判定为 ARCHIVE 的文档移进这里。)
  - docs/SourceMaterials/  docs/phase3_readiness/  docs/v26_retrospective/  (核实内容与价值。)

对每个子目录给一行结论:类别(auto-telemetry / db-backup / archive / source-material / mixed)+ 是谁/什么在写 + 保留建议(KEEP-as-is / GITIGNORE-CANDIDATE / ARCHIVE / REVIEW)+ 证据。`,
  { label: 'assess:subdirs', phase: 'Classify' }
)

// 状态文档核查(CLAUDE.md 引用的「活设计笔记」是否仍准确)
const statusDocThunk = () => agent(
  `你是文档分流分析师。核查仓库里被 CLAUDE.md 当作「活设计笔记」引用的状态文档,判断它们今天是否仍准确(CURRENT)还是已过时(STALE)。今天 ${TODAY}。cwd 是仓库根。

逐个 Read 并对照活代码 + git log 核实:
  - backend/CODE_STATUS.md
  - backend/REFACTORING_STATUS.md
  - backend/agents/IMPROVEMENT_ANALYSIS.md
  - backend/agents/core/ARCHITECTURE.md
  - README.md
  - docs/flag_lifecycle.md(若它声称是 flag 生命周期权威)

重点:这些文档最后更新于何时(git log -1)?它们描述的状态(已退役的 tier/cascade/串行?路由层?optimization 层?)与今天的代码是否一致?是否提到已不存在的模块/flag?给每篇:CURRENT/PARTIALLY-STALE/STALE + 最后更新日期 + 具体过时点(若有)+ 是否建议更新。`,
  { label: 'assess:status-docs', phase: 'Classify' }
)

const classifyResults = await parallel([...clusterThunks, subdirThunk, statusDocThunk])

// 前 N 个是簇结果(含 docs[]),后两个是 subdir / status-doc 的文本结论。
const clusterCount = CLUSTERS.length
const clusterOut = classifyResults.slice(0, clusterCount).filter(Boolean)
const subdirOut = classifyResults[clusterCount] || '(子目录评估未返回)'
const statusOut = classifyResults[clusterCount + 1] || '(状态文档核查未返回)'

const allDocs = clusterOut.flatMap(r => (r && r.docs) ? r.docs : [])
const clusterNotes = clusterOut.map((r, i) => `### ${CLUSTERS[i] ? CLUSTERS[i].name : 'cluster' + i}\n${(r && r.cluster_notes) || ''}`).join('\n\n')

log(`已分类 ${allDocs.length} 篇文档;开始汇总`)

phase('Synthesize')

const SYNTH_SCHEMA = {
  type: 'object',
  properties: {
    report_markdown: { type: 'string', description: '面向用户的完整中文 Markdown 报告(见 prompt 要求的结构)' },
    delete_candidates: { type: 'array', items: { type: 'string' }, description: '建议删除的文件路径(近重复快照等无保留价值的)' },
    archive_candidates: { type: 'array', items: { type: 'string' }, description: '建议移入 docs/archive/ 的文件路径' },
    keep_active: { type: 'array', items: { type: 'string' }, description: '建议留在 docs/ 顶层活跃区的文件路径' },
    needs_human: { type: 'array', items: { type: 'string' }, description: 'UNCERTAIN、需人工决定的文件路径' },
  },
  required: ['report_markdown', 'delete_candidates', 'archive_candidates', 'keep_active', 'needs_human'],
}

const synthesis = await agent(
  `你是文档库总编。下面是对 docs/ 全部 70 篇顶层文档逐篇核实后的分类结果(JSON),以及子目录评估和状态文档核查的文本结论。请汇总成一份给用户的、可执行的中文文档整理报告。

【逐篇分类结果 JSON】
${JSON.stringify(allDocs, null, 1)}

【各簇交叉笔记】
${clusterNotes}

【子目录评估】
${subdirOut}

【状态文档核查】
${statusOut}

你的任务:
1. **跨簇取代链**:跨越主题簇找出所有「同主题、新版取代旧版」的链条(如 phase4 plan v1→v5、competitive v1→v3、dirty_data_scan 三份近重复、backlog_drain 两份),明确每条链「留哪个、归档/删哪些」。
2. **按 recommendation 分组**:把 70 篇归入 KEEP(活跃)/ARCHIVE(归档)/DELETE(可删)/MERGE(应合并)/UNCERTAIN(待人工),每组给一个紧凑表格(文件 | 状态 | 一句话理由)。
3. **冲突复核**:如果某篇 confidence=low 或不同信号矛盾,标出来放进 UNCERTAIN,不要硬下结论。
4. **docs/ 重组方案**:给出建议的目录结构(例如保留哪些在顶层、archive 子目录如何按主题/时间组织、自动遥测目录是否该 gitignore),以及一个「现在能安全执行」的具体行动清单(移动/删除哪些文件、是否建一个 docs/INDEX.md 索引)。
5. report_markdown 用清晰的中文 Markdown:开头给一段「总体诊断」(70 篇里大致多少现行/参考/可归档/可删 + 最该先做的 3 件事),然后是分组表格、取代链、重组方案、行动清单。表格用文件名(去掉 docs/ 前缀也可)保持紧凑。

同时把 delete_candidates / archive_candidates / keep_active / needs_human 三组路径数组单独填好(用于后续自动执行),路径用相对仓库根的完整路径(docs/xxx.md)。`,
  { label: 'synthesize:report', phase: 'Synthesize', schema: SYNTH_SCHEMA }
)

return {
  total_docs_classified: allDocs.length,
  synthesis,
  raw_docs: allDocs,
  subdir_assessment: subdirOut,
  status_doc_assessment: statusOut,
}
