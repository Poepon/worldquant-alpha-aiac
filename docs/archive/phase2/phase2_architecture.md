# Phase 2 Architecture — 一页流程图

> Plan v5+ §Phase 2 (HGE Level 2+) 工程层完整数据流。读完这一页能开始
> debug Phase 2 production 任务。

## TL;DR

```
LLM → typed Hypothesis (DB row)
       ↓
       state.current_hypothesis_id
       ↓
       alpha.hypothesis_id (B4 FK)
       ↓
       lifecycle (PROPOSED→ACTIVE→PROMOTED|ABANDONED|SUPERSEDED)
       ↓
       KB (meta_data.hypothesis_id + variant tag)
```

## 触发条件

`task.config.hypothesis_centric_variant = 2` (per-task) OR
`HYPOTHESIS_CENTRIC_LEVEL >= 2` (env default)

## 核心数据流

```
┌───────────────────────────────────────────────────────────────────────┐
│ mining_tasks.run_mining_task                                         │
│   ↓ reads task.config.hypothesis_centric_variant                     │
│   ↓ resolves active_level                                            │
│ mining_agent.run_evolution_loop / run_mining_iteration               │
│   ↓ injects configurable {                                           │
│       trace_service, db_session, brain_adapter,                      │
│       available_dataset_pool, hypothesis_centric_level,              │
│       experiment_variant, llm_service ★ B5 v2                        │
│     }                                                                 │
│ workflow.run_with_persistence → workflow.run → app.ainvoke (LangGraph)│
└──────────────────────────────────┬────────────────────────────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              │ Per round:                                │
              │                                           │
              │  rag_query → distill_context → hypothesis │
              │                                  ↓ B3     │
              │  ┌─────────────────────────────────────┐  │
              │  │ if level >= 2:                       │  │
              │  │   HypothesisService.create_hypothesis│  │
              │  │   → Hypothesis row PROPOSED          │  │
              │  │   state.current_hypothesis_id ← row.id│ │
              │  │   (V-19.7: only PRIMARY persisted)   │  │
              │  └─────────────────────────────────────┘  │
              │                                  ↓        │
              │  t1_strategy_select → t1_expand → validate│
              │                       (Phase 1: cross-DS) │
              │  validate ⇄ self_correct (V-15)           │
              │              ↓                             │
              │  simulate (V-19.3 cross-task dedup)       │
              │              ↓                             │
              │  evaluate (V-12 OS gate, V-16 suspicion) │
              │   ↓ failure_feedback_queue (B8)           │
              │   └→ rag_service.record_failure_pattern   │
              │       with hypothesis_id + variant         │
              │              ↓                             │
              │  save_results                              │
              │  ┌─────────────────────────────────────┐  │
              │  │ B4: AlphaResult.hypothesis_id ←      │  │
              │  │     state.current_hypothesis_id      │  │
              │  │     (cb6b047 fallback to ids[0])     │  │
              │  │                                      │  │
              │  │ B5: _process_hypothesis_feedback     │  │
              │  │   ├ classify_attribution             │  │
              │  │   │   ├ B5 v2: LLM (llm_service)     │  │
              │  │   │   └ fallback: heuristic 75% rule │  │
              │  │   ├ append entry to history[hid]     │  │
              │  │   ├ mark_active (all hids — V-19.6)  │  │
              │  │   ├ mark_promoted (PRIMARY — V-19.6)│  │
              │  │   └ should_abandon → mark_abandoned  │  │
              │  │       (PRIMARY — V-19.6, B6 N=3)     │  │
              │  └─────────────────────────────────────┘  │
              │                                            │
              │  ↓ pending_alphas → AlphaResult list       │
              └────────────────────────────────────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              │ workflow.run_with_persistence outer:       │
              │                                            │
              │  Per-row SAVEPOINT INSERT (V-19.2)        │
              │   ↓ Alpha(hypothesis_id=...)              │
              │   ↓ V-19.3 cross-task dedup pre-INSERT     │
              │   ↓ failure: persistence_errors.log (T04)  │
              │     SAVEPOINT rollback isolates the row    │
              │  outer commit                              │
              │   ↓ V-19.1 fields_used UPDATE              │
              │   ↓ V-19.5 refresh hypothesis_stats        │
              │     (post-commit so JOIN sees new alphas)  │
              │   ↓ B7 enqueue_can_submit_refresh          │
              └────────────────────────────────────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              │ mining_agent._run_feedback_learning        │
              │   ↓ extract hypothesis_ids from alphas     │
              │   ↓ task.config.hypothesis_centric_variant │
              │ feedback_agent.learn_from_round (B8)       │
              │   ↓ LLM aggregates round patterns          │
              │ KnowledgeEntry.meta_data {                 │
              │   hypothesis_id: ...,                      │
              │   hypothesis_ids: [...] (accumulator),     │
              │   experiment_variant: "1" | "2"            │
              │ }                                          │
              └────────────────────────────────────────────┘
```

## 关键 SQL queries (debug 用)

```sql
-- 1. Phase 2 task 是否真在跑
SELECT id, status, config->>'hypothesis_centric_variant' AS variant
FROM mining_tasks
WHERE config->>'hypothesis_centric_variant' = '2'
  AND status IN ('RUNNING','COMPLETED')
ORDER BY id DESC LIMIT 10;

-- 2. alpha → hypothesis 链路验证
SELECT a.id, a.task_id, a.alpha_id, a.hypothesis_id, h.status, h.statement
FROM alphas a
LEFT JOIN hypotheses h ON h.id = a.hypothesis_id
WHERE a.task_id = ?
ORDER BY a.id;

-- 3. Hypothesis lifecycle 分布
SELECT status, COUNT(*) AS n,
       COUNT(*) FILTER (WHERE alpha_count > 0) AS with_alphas
FROM hypotheses GROUP BY status;

-- 4. KB 是否有 hypothesis_id 标签 (B8 验证)
SELECT entry_type,
       COUNT(*) FILTER (WHERE meta_data->>'experiment_variant' = '2') AS v2_total,
       COUNT(*) FILTER (
         WHERE meta_data->>'experiment_variant' = '2'
           AND meta_data->>'hypothesis_id' IS NOT NULL
       ) AS v2_with_hid
FROM knowledge_entries
WHERE created_at > NOW() - INTERVAL '7 days'
  AND entry_type IN ('SUCCESS_PATTERN', 'FAILURE_PITFALL')
GROUP BY entry_type;

-- 5. Persistence 健康
-- 应该看到 0 行（所有 PROMOTED 都该有 alpha）
SELECT * FROM hypotheses WHERE status='PROMOTED' AND alpha_count=0;
-- 应该看到 0 行（所有 zombie 都已 SUPERSEDED）
SELECT * FROM hypotheses WHERE status='ACTIVE' AND alpha_count=0;
```

## 已知 Bug / Hotfix 索引

| Bug | Symptom | Fix Commit |
|---|---|---|
| Batch UC violation 整批回滚 | task complete 但 0 alpha | V-19.2 SAVEPOINT (`e346bb9`) |
| Sign-flip 撞历史 alpha_id | INSERT 报错 | V-19.3 cross-task dedup (`4b50060`) |
| LangGraph scalar 不传 | hypothesis_id=None at evaluate | cb6b047 fallback to list[0] |
| stats 滞后 | PROMOTED with alpha_count=0 | V-19.5 post-commit refresh (`5376366`) + backfill |
| ghost PROMOTED | non-primary 也被 promote | V-19.6 primary-only (`f2c6047`) |
| zombie ACTIVE | non-primary 永停 ACTIVE | V-19.7 only persist primary (`cd5375a`) + backfill |
| 错误日志看不见 | loguru → stderr → Celery logfile 截断 | V-19.2 `logs/persistence_errors.log` (T04 加 logrotate) |

## 关键文件索引

| 路径 | 角色 |
|---|---|
| `backend/models/hypothesis.py` | Hypothesis ORM + 25 cols + 10 索引 (B1) |
| `backend/services/hypothesis_service.py` | CRUD + lifecycle + stats + rounds_active (B7) |
| `backend/agents/graph/nodes/generation.py` | node_hypothesis (B3 persist) |
| `backend/agents/graph/nodes/persistence.py` | node_save_results + B5 helper (B4 + B5) |
| `backend/agents/graph/attribution.py` | B5 v2 LLM classifier |
| `backend/agents/graph/early_stop.py` | classify_attribution (heuristic) + should_abandon (B6) |
| `backend/agents/graph/persistence_errors.py` | V-19.2 file log + T04 logrotate |
| `backend/agents/graph/workflow.py` | run_with_persistence (V-19.x SAVEPOINT + V-19.5 refresh) |
| `backend/agents/services/rag_service.py` | record_*_pattern + get_recent_pass_examples (B8) |
| `backend/agents/feedback_agent.py` | learn_from_round (B8 KB write) |
| `backend/alembic/versions/c7f9e21b3a47_*.py` | hypotheses table migration |

## 相关脚本

| 脚本 | 用途 |
|---|---|
| `scripts/phase2_ab_launch.py` | 投递 LEVEL=1 vs LEVEL=2 A/B 批次 |
| `scripts/phase2_ab_compare.py` | 跑对比 → docs/phase2_ab_report_<date>.md |
| `scripts/phase3_readiness_check.py` | Phase 3 GO/NO-GO 检查 (5 gates) |
| `scripts/backfill_hypothesis_stats.py` | V-19.5 兜底（修 alpha_count 滞后）|
| `scripts/backfill_zombie_hypotheses.py` | V-19.7 兜底（清 zombie ACTIVE → SUPERSEDED）|

## 推荐 onboarding 顺序

1. 读这一页（5 min）
2. 跑 `python scripts/phase3_readiness_check.py` 看当前状态（30 sec）
3. 跑上面 SQL #2 看一个 task 的 alpha → hypothesis 链路（2 min）
4. 读 `docs/phase2_completion_2026-05-06.md` 完整交付报告（10 min）
5. 读 `docs/phase3_evaluation_2026-05-06.md` 决策依据（5 min）
6. 浏览 14 个 commit 信息：`git log --oneline --grep "phase2\|phase3\|V-19"` (5 min)
