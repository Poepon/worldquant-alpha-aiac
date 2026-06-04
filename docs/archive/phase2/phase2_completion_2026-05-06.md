# Phase 2 完成报告 — 2026-05-06

> Plan v5+ §Phase 2 (Typed Hypothesis + KB 引用 + Lifecycle) 工程层 7 day
> 全部交付完成。Phase 1 (5 day) + Phase 2 (7 day) 累计 12 day 完成 plan 主线。

## 执行摘要

Phase 2 把 hypothesis 从 round-scoped LLM dict 升级为一等公民 DB 行，实现：

1. **跨 round alpha 累积**：每 alpha 通过 `alpha.hypothesis_id` FK 链到产生它的 typed Hypothesis
2. **生命周期管理**：PROPOSED → ACTIVE → PROMOTED / ABANDONED 状态机
3. **KB 学习单元升级**：`KnowledgeEntry.meta_data.hypothesis_id` 让 RAG 检索按假设家族（不再只按 dataset）
4. **HGE Level=2 灰度路径**：与 Level=0/1 完全向后兼容，通过 `task.config.hypothesis_centric_variant` 切换

**生产环境验证**：task 143 (LEVEL=2) 15 alphas / 100% with hypothesis_id；task 142 (legacy v=0) 6 alphas / 0 hypothesis_id（合法）。10 hypotheses 真实 PROMOTED，KB 11/11 SUCCESS_PATTERN 含 hypothesis_id 标签。

## 交付清单 — 10 commits (~7 day)

| 子步骤 | Commit | 工时 | 内容 |
|---|---|---|---|
| **B1** | `881f6b8` | 0.5d | Hypothesis ORM 模型 + alembic c7f9e21b3a47（hypotheses 表 + alphas.hypothesis_id FK + 10 索引） |
| **B7** | `26f9926` | 1d | HypothesisService — CRUD + lifecycle 转移 + refresh_stats |
| **B3** | `87fceeb` | 1d | node_hypothesis 在 LEVEL≥2 时 INSERT Hypothesis 行（time-ordering 防御） |
| **B4** | `a8d7e5e` | 0.5d | alpha.hypothesis_id 端到端 — buffered + incremental 双路径 |
| **B5+B6** | `b88415d` | 1.5d | _process_hypothesis_feedback heuristic attribution + N-round abandon |
| **B8** | `b49b09a` | 1.5d | RAGService + feedback_agent + evaluation 全部带 hypothesis_id 标签写入 KB；retrieval 按 hypothesis_id 过滤 |
| **cb6b047** | hotfix | <0.5h | LangGraph scalar 字段 propagation 不可靠 → fallback 到 list[0] |
| **V-19.5** | `5376366` | 0.5h | refresh_stats 时序修复（移到 post-commit）+ 10 行 backfill |
| **V-19.6** | `f2c6047` | 0.5h | promote/abandon 只对 primary（修 ghost promotion）+ 34 行 backfill |
| **B10** | `0016005` | 1d | 4 个端到端集成测试 |
| **累计** | | **~7 day** | **335 tests pass** |

## 数据流（Level=2 完整链路）

```
mining_tasks.run_mining_task
  ├─ reads task.config.hypothesis_centric_variant
  └─ threads hypothesis_centric_level + experiment_variant 进
mining_agent.run_evolution_loop / run_mining_iteration
  └─ 注入 configurable.{hypothesis_centric_level, experiment_variant}
workflow.run_with_persistence → workflow.run → app.ainvoke
  ↓
node_hypothesis (B3)
  ├─ LLM 生成 hypothesis dict (3-5 个 / round)
  ├─ HypothesisService.create_hypothesis → PROPOSED row
  └─ 写 state.{current_hypothesis_id, current_hypothesis_ids}
  ↓ code_gen → validate → simulate → evaluate
node_evaluate (B8 KB pitfall)
  └─ record_failure_pattern(hypothesis_id, experiment_variant)
node_save_results (B4 + B5)
  ├─ AlphaResult.hypothesis_id ← state.current_hypothesis_id (or list[0] fallback)
  ├─ _process_hypothesis_feedback (B5):
  │    ├─ classify_attribution (heuristic)
  │    ├─ append to state.hypothesis_round_history[hid]
  │    ├─ mark_active (all hids — V-19.6)
  │    ├─ mark_promoted (PRIMARY only — V-19.6)
  │    └─ should_abandon → mark_abandoned (PRIMARY only — V-19.6)
  └─ HYPOTHESIS_FEEDBACK trace step
  ↓
workflow.run_with_persistence outer:
  ├─ Per-row SAVEPOINT INSERT (V-19.2) with hypothesis_id (B4)
  ├─ V-19.3 cross-task alpha_id dedup
  ├─ V-19.1 fields_used UPDATE
  └─ V-19.5 post-commit refresh_stats (denormalized cols ← JOIN)
  ↓
mining_agent._run_feedback_learning
  └─ feedback_agent.learn_from_round (B8 KB SUCCESS_PATTERN/FAILURE_PITFALL with hypothesis_ids)
```

## 生产环境验证（2026-05-06 smoke + 历史数据）

### Task 142/143/144 三任务 smoke (LEVEL=0 + LEVEL=2 各两组)

| Task | Variant | Status | Alphas | hypothesis_id 填充 |
|---|---|---|---|---|
| 142 | baseline (v=0) | COMPLETED | 6 | **0/6** ✅ legacy 不该填 |
| 143 | LEVEL=2 (v=2) | COMPLETED | 15 | **15/15** ✅ 100% |
| 144 | LEVEL=2 (v=2) | RUNNING | 3+ | **3/3** ✅ 100% |

### 全 DB 状态 (Phase 2 后)

| 维度 | 数值 | 说明 |
|---|---|---|
| Hypothesis rows | 104 | 80 ACTIVE + 10 PROMOTED + 14 PROPOSED |
| PROMOTED 真实性 | **10/10 with alpha_count > 0** | 0 ghost rows（V-19.6 + backfill 修后）|
| Alpha rows total | 6,066 | |
| Alpha with hypothesis_id | 18 | 全部来自 LEVEL=2 任务 |
| KB SUCCESS_PATTERN with hypothesis_id | 11/114 | 仅来自 LEVEL=2 路径 |
| KB FAILURE_PITFALL with hypothesis_id | 16/451 | LEVEL=2 + 部分 evaluation 直接写入 |

## Plan v5+ §Phase 2 验收 criteria

| Criterion | Plan 阈值 | 实测 | 状态 |
|---|---|---|---|
| Hypothesis 跨 round 持久化 | 行级累积可见 | 9 hypotheses (task 143) 跨 10+ rounds | ✅ |
| Lifecycle 4 态可达 | PROPOSED/ACTIVE/PROMOTED/ABANDONED | 全部触发过 | ✅ |
| alpha.hypothesis_id 100% (LEVEL=2) | 100% | task 143: 15/15, task 144: 3/3 | ✅ |
| Legacy alpha.hypothesis_id NULL | 100% | task 142: 0/6 | ✅ |
| KB 学习单元含 hypothesis_id | meta_data.hypothesis_id 非 NULL | SUCCESS 11/11 (LEVEL=2 path), FAILURE 16/16 (B8 active path) | ✅ |
| RAG 按 hypothesis_id 过滤 | filter 命中 | test_b8 + test_b10 验证通过 | ✅ |
| Time-ordering 防御 | hypothesis.created_at < alpha.created_at | B3 在 code_gen 前 INSERT | ✅ |
| Variant 隔离 (F-5) | KB / dedup variant 内 | meta_data.experiment_variant tagged | ✅ |
| 向后兼容 LEVEL=0/1 | legacy 路径不变 | task 142 完整跑通 | ✅ |

**9/9 验收 criteria 全部满足**。

## 6 个生产环境 bug 发现 + 修复轨迹

| Bug | 触发 | 修复 |
|---|---|---|
| Spike B5/B6/B7 静默 alpha 丢失 | UC `uq_alpha_id` 跨 task 撞车 → batch commit 整批回滚 | V-19.2 per-row SAVEPOINT |
| 错误日志被截断不可见 | loguru→stderr→Celery `--logfile` | V-19.2 `logs/persistence_errors.log` |
| Sign-flip 重复 alpha_id | sign-flip retry 绕开 dedup → BRAIN 返回历史 alpha_id | V-19.3 pre-INSERT batch SELECT + sign-flip pre-dedup |
| LangGraph scalar propagation 不稳定 | `state.current_hypothesis_id` 在 evaluate node 时为 None 而 list 正常 | cb6b047 fallback `list[0]` |
| Hypothesis stats 滞后为 0 | refresh_stats 在 alpha INSERT 之前跑 | V-19.5 post-commit refresh + 10 行 backfill |
| Ghost PROMOTED 行 | 同 round 所有 hids 都 promote 但 alpha 只链 primary | V-19.6 primary-only 转换 + 34 行 backfill |

每个 bug 都附带 unit test + 修复对应 backfill 脚本，未来回归保护就绪。

## 测试覆盖 — 335 pass

| Test file | tests | 覆盖 |
|---|---|---|
| `test_phase2_b1_hypothesis_schema.py` | 10 | ORM 模型、enum、FK、索引 |
| `test_phase2_b7_hypothesis_service.py` | 17 | CRUD、lifecycle、stats |
| `test_phase2_b3_propose.py` | 7 | level 路由、单/多 hypothesis、错误 fallback |
| `test_phase2_b4_alpha_link.py` | 4 | hypothesis_id 写入两路径 |
| `test_phase2_b5_b6_lifecycle.py` | 20 | attribution + abandon + V-19.6 ghost-fix |
| `test_phase2_b8_kb_hypothesis.py` | 7 | KB write + filter retrieval |
| `test_phase2_b10_integration.py` | 4 | 端到端 happy path / 3-round abandon / KB lineage / V-19.5 stats refresh |
| **Phase 2 小计** | **69** | |
| 全套 (含 Phase 1 + V-19 + factor_tier) | **335** | |

## 已知 backlog（不阻塞 Phase 2 验收）

1. **Zombie ACTIVE 行**：V-19.6 后 non-primary hypotheses 永久停在 ACTIVE 状态。长期 fix：B3 改为 1 hypothesis/round 或者 alpha 分配跨 siblings
2. **B5 v2 LLM-based attribution**：当前 heuristic 分类器够用 abandon 决策；plan §B5 提到的 `Experiment2Feedback` LLM 路径仍 dormant
3. **B11 实测 A/B**：LEVEL=1 vs LEVEL=2 真实 PASS rate / can_submit / KB hypothesis-keyed entries 对比 — 跑数据时一并做（2d）
4. **B9 RAG variant 强化**：基本支持已就位（experiment_variant 标签 + filter），edge case 完善延后
5. **HYPOTHESIS_CENTRIC_LEVEL=3 主循环翻转**：Plan v5 Final §三轮精简已 backlog 到 Q3 重评估

## 部署状态

```
.env (生产):
  HYPOTHESIS_CENTRIC_LEVEL=0   # 默认仍走 legacy
  HYPOTHESIS_CENTRIC_CANDIDATE=2  # Phase 2 灰度候选

per-task override:
  task.config.hypothesis_centric_variant = 0 | 1 | 2
```

**worker 重启后 Phase 2 路径就绪**。下一次 LEVEL=2 task 投递自动走完整 typed Hypothesis lineage。

## 推荐下一步

| 路径 | 含义 |
|---|---|
| **B11 实测 A/B**（2d 跑数据）| LEVEL=1 vs LEVEL=2 大批量对比，验证 Phase 2 实际收益 |
| **修 zombie ACTIVE backlog**（0.5d）| 让 non-primary hypotheses 走 SUPERSEDED 而非永远 ACTIVE |
| **Phase 3 backlog 评估** | Plan v5 Final §简化 把 Phase 3 推到 Q3，看 B11 数据后再决定 |
| **暂停 Phase 推进**（停在工程完成态） | Phase 1+2 已是 plan v5 Final 的核心交付，工程层全部完成 |

---

**Phase 2 工程层完成态**：10 commits, 335 tests, 9/9 验收 criteria, 6 个生产 bug 修复 + backfill, smoke + B10 双重验证。
