# Phase 4 A+B 落地方案 v5.0 — post-v4-review fix(ship-candidate final)

> **版本**:v5.0(post-v4 Round A 6/10 verification,**ship-candidate**)
> **日期**:2026-05-19
> **取代**:[`phase4_a_b_plan_v4_2026-05-19.md`](phase4_a_b_plan_v4_2026-05-19.md)(v4.0 综合 v3 三轮 19 项 MUST,但 v4 Round-A review 发现 5 项 fix 有执行漏洞 + 2 P0 新阻塞 + 我 verify 4 项 plan 内部日期矛盾)
> **scope**:14 PR / **~48 人日** / **5 sprint**(Sprint 5 推 R12 decision 之后)
> **承诺约束**(承自 v4.0 + v5 fix):
> - 全局 single-tenant ✓(v4.0 已 verify 真修)
> - audited restore 复用既有 `feature_flag_audit` 表 + **补 `restored_at` ADD COLUMN**(v4 Round-A MUST fix)
> - freeze 约束 Sprint 1-3 6 sentinel code path 禁 delete ✓
> - **B4 拆 2 PR**:G3-v2 grammar Sprint 4 ship 不动 G3 shadow code;完整 retire G3 推 Sprint 5 R12 decision 之后
> - Ship date:**Sprint 4 末 2026-07-19**,Sprint 5 retire/cleanup R12 decision 后(7/4 ± 5d)开

---

## 0. v4.0 → v5.0 12 项 fix(本文新增)

| # | v4 Round-A 发现 | v5 修正 |
|---|---|---|
| **F1** | **A-2 路径错** — plan 写 `backend/services/hierarchical_rag.py`,真实是 `backend/agents/hierarchical_rag.py:908`;legacy `rag_service.py:330 query()` 是 fallback entry,sentinel L0 skip 仅在 `query_hierarchical` 生效 | §6.0.5 路径改 `backend/agents/hierarchical_rag.py:908`(`query_hierarchical` 函数);**新增 `rag_service.py:330 query()` legacy entry 也加 L0 skip 守护**(R12 sentinel ON 时 legacy path 拒绝命中 RAG#0)|
| **F2** | **A-4 致命** — R10-calib SQL 用不存在的 `alphas.daily_pnl` 列;真实 `models/alpha.py:190` 只 `pnl` Float 标量 | §6.7 R10-calib spike 改 **Python-side `CorrelationService.batch_pairwise_correlations()`**(异步拉 PnL series via BRAIN);工时 **0.5 → 1.5 人日** |
| **F3** | **A-5 zombie key 写错 + 漏 keys** — plan 列 `__r1b_consumed_hypothesis`,真实是 `__r1b_consumed_pending_hypothesis`(`workflow.py:275-278` + `mining_tasks.py:1016`);漏 `__BANDIT_CONFIG_KEY`(`mining_agent.py:1213,1230`);`brain_role_snapshot` 影响 BRAIN role 不该清 | §3.1 A-5 / §6.1 drain step 改:`g5_pending_offspring` / `__pending_hypothesis` / `__g5_consumed_offspring` / **`__r1b_consumed_pending_hypothesis`**(修字)/ `__BANDIT_CONFIG_KEY`(新加);**显式不清 `brain_role_snapshot`** |
| **F4** | **B-2 致命** — plan §2 第 9 原则承诺 `prior_override_value` + `prior_action` + `restored_at`,但既有 `FeatureFlagAudit` 表(`models/config.py:107-127`)只有 `old_value/new_value/action`;Alembic j1a2b3c4d5e6 缺 `restored_at` ADD COLUMN → `restore_sentinel()` 直接报 column does not exist | §6.1 Alembic 补 `ADD COLUMN restored_at TIMESTAMP NULL` + `ADD COLUMN restored_by VARCHAR(64)`;§2 原则 9 改用既有字段 `old_value`/`action` 表述 prior state |
| **F5** | **C-3 11 caller 数错** — grep `apply_family_cap` 实际只 **1 caller**(`evaluation.py:2838`);`family_classifier.py:90-158` 本身已 stamp-only;真正重构 = 改 `evaluation.py:2844` 那一行 stamp-only,**`family_classifier.py` 无改动** | §6.10 B3 工时 **4 → 2.5 人日**;描述改 "evaluation.py:2844 stamp-only 延迟 FAIL,family_classifier.py 不动";11 caller 改写为 7 stamp reader(`dag_state.py:670` / `hierarchical_rag.py:822` / persistence / DAG / 测试)契约不变 |
| **F6 (P0)** | **PARTIAL counterfactual SQL 跑不通** — Sprint 末 6 sentinel stamp grep:`_g5_crossover_parent_ids` ✓ / `_g3_ast_originality_blocked` ✓ / `_r10_family_cap_dropped` ✓,但 `_r1b_mutation_triggered` ❌ + `_hypothesis_forest_reference` ❌ + `_simulation_cache_hit` 未确认 — SQL UNION 跑不出 6 行 | **Sprint 0 加 0.5 人日**:补 3/6 sentinel 缺失 stamp(R1b mutate / G8 forest reuse / R9 cache hit)。改动:`backend/agents/graph/nodes/hypothesis_mutate.py` + `backend/services/hypothesis_service.py:fetch_cross_task_promoted` + `backend/agents/sim_cache.py` 各加 1 行 `alpha.metrics['_xxx_triggered']=True` stamp |
| **F7 (P0)** | **既有 audit 表 read 路径混杂** — `feature_flag_service.py:751 list_audit` powers ops Timeline,R12 sentinel ON 一次 INSERT 7 行 → Timeline 刷屏 | §6.1 A1 内加:`list_audit(filter_sentinel=True)` 默认排除 `sentinel_trigger_for IS NOT NULL` 行;前端 ops `/ops/feature-flags` Timeline 加 toggle "show sentinel-triggered rows" |
| **F8** | **Sprint 1 ship 日期 vs R12 decision 矛盾** — plan §7 Sprint 1 5/22-6/4(ship 6/4),plan §10 写 5/26 ship + 50d obs。**真实 6/4 ship → 30d obs 到 7/4**,不是 7/15 | §7 Sprint 末 R12 decision **7/4 ± 5d**(不是 7/15)+ Sprint 4 末 7/19 移到 R12 decision *之后* |
| **F9** | **Sprint 3 起步 vs R10 互验 6 天错位** — plan Sprint 3 起步 6/19,互验决策 6/25 才完成(Sprint 2 ship 6/18 + 7d obs)| §7 Sprint 3 起步推 **6/26**(R10 互验 7d obs 期满后);Sprint 3 推 6/26-7/9 |
| **F10** | **B4 在 Sprint 4 ship 7/3-7/12,R12 decision 7/4 之前 ship freeze 失效** | §6.14 B4 **拆 2 PR**:B4.1 G3-v2 grammar-aware **不动 G3 shadow code**(可 Sprint 4 ship,3 人日);B4.2 完整 retire G3 shadow **推 Sprint 5**(R12 decision 之后,3 人日)|
| **F11** | **worker 模型未明示** — Sprint 1 写 worktree 暗示多 worker,其他 Sprint 不明 | §7.0 新增 "worker model: 1 主 worker + worktree agent 协作 sprint-by-sprint";单 worker 工时 = plan 列工时;worktree agent 用于 A2/A3/A4 并行 rebase 等场景 |
| **F12** | **legacy `rag_service.query()` entry sentinel 处理(扩 F1)** | §6.0.5 PR0.5 工时 0.5 → 0.7 人日(2 entry 各加 L0 skip) |

---

## 1. 摘要(v5.0 更新)

| 维度 | v4.0 | **v5.0** |
|---|---|---|
| PR 总数 | 13 | **14**(B4 拆 2 PR + Sprint 0 sentinel stamp 补)|
| 人日 | 48 | **~48**(F2 +1 / F5 -1.5 / F6 +0.5 / F10 0 split / 其他细节净额 0)|
| Sprint 数 | 5(含 Sprint 末)| **5**(Sprint 5 是 R12 decision 后 cleanup window)|
| Ship date | 7/12 | **7/19**(Sprint 3 推 7d)|
| R12 decision | 7/15±5d(错算)| **7/4±5d**(真实 6/4 ship + 30d) |
| **新增 §6.0.6 sentinel stamp 补 Sprint 0** | n/a | 0.5 人日(P0 阻塞 fix)|

---

## 2. 设计原则

承自 v4.0 9 原则不变,**修正第 9 原则字段名**:

> 9. 激进推翻配 freeze 约束 — 推翻已 ship 机制时,deprecate path code 在 decision point 之前禁止 delete(标 `@deprecated_pending_X_decision`);只允许 flag default OFF,实际 cleanup 推 decision point 之后的下一 sprint;**audit schema 必须能 ground-truth 重建 prior state(复用既有 `feature_flag_audit` 的 `old_value` + `action` + 新增 `restored_at` 三字段)**

---

## 3. v3 fix + v4 fix 决策矩阵(综合)

v3 review 19 项 fix(承自 v4.0 §3.1/3.2/3.3 表,无变更),**叠加 v4 Round-A 5 项重 fix + 2 P0(已纳 §0)**。fix 总数:**19 + 5 + 2 = 26 项**(v5 全部 in-plan resolved)。

---

## 4. PR 依赖图(v5.0)

```
Sprint 0 (前置 / 5/20-5/22 / 2.25 人日)
├─ PR0    LLM_API_CIRCUIT default ON                                1.0
├─ PR0.5  ENABLE_R8_L0 子 flag(query_hierarchical + query() 双 entry skip) 0.7  ← F12 fix
├─ PR0.6  Sprint 0 sentinel stamp 补(R1b mutate / G8 forest / R9 cache hit) 0.5  ← F6 P0 fix
└─ Spike  R14 + R12 baseline                                        0.25  

Sprint 1 (R12 critical / 5/22-6/4 / 12.8 人日 / 2 周)
├─ A1   R12 LLM_MODE + sentinel guard(audited restore + F3 zombie drain + F4 restored_at + F7 audit filter)  9.5
├─ A2   R14 + race fix                                              1.8
├─ A3   flat-F4 cross-region                                        2.0
└─ A4   AQR Kelly KB seed + baseline rebase                         1.0

Sprint 2 (评估+风控+双 spike+互验 / 6/5-6/18 / 11.5 人日 / 2 周)
├─ R13-spike  BRAIN sim daily PnL                                   0.5
├─ G9-spike   portfolio simulator                                   1.5
├─ R10-calib  CorrelationService Python-side pairwise corr  1.5  ← F2 fix(was 0.5)
├─ B1   R11 capacity                                                2.0
├─ B2   R13 factor_lens shadow                                      3.5
└─ B3   R10-v2 + evaluation.py:2844 stamp-only delay(family_classifier 不动)  2.5  ← F5 fix(was 4)

Sprint 3 (学界 SOTA Part 1 + R10 互验决策 / 6/26-7/9 / 10.5 人日 / 2 周)  ← F9 fix(was 6/19-7/2)
├─ R10/R10-v2 互验决策(6/18 ship + 7d obs = 6/25 期满,Sprint 3 6/26 起步)
├─ B5   R8-v3 cognitive layer 7-layer                               6.5
└─ A5.1 G10 PR1(distill + cron + ops endpoint,similarity = token Jaccard) 4.0

【R12 decision point — 2026-07-04 ± 5d】  ← F8 fix(was 7/15)
  Sprint 1 ship 6/4 + 30d obs = 7/4;Sprint 3 内决策点
  - GO  → 6 sentinel 永久 deprecate,Sprint 5 cleanup 路径 LIVE
  - NO-GO → restore_sentinel() + Sprint 5 cancel B4.2 + sentinel code 永久保留
  - PARTIAL → counterfactual SQL by sentinel,选择性 restore + cleanup

Sprint 4 (学界 SOTA Part 2 + B4.1 G3-v2 grammar / 7/10-7/19 / 7 人日 / 1.5 周)  ← F8 fix Ship date
├─ A5.2 G10 PR2(prompt 注入 + refine chain)                        2.5
├─ B4.1 G3-v2 grammar-aware(不动 G3 shadow code,仅新增 path)        3.0  ← F10 fix(B4 拆)
└─ 闭环  baseline × 3 + canary SOP + lifecycle docs                  1.5

Sprint 5 (R12 decision 后 / 7/20+ / ~3-9 人日)  ← F10 fix(B4 retire + 6 sentinel cleanup)
依 R12 decision 路径:
  - GO 路径:B4.2 retire G3 shadow(3 人日)+ 6 sentinel 永久 cleanup(R1b/G5/G8/R9 各 ~1.5 人日 = 6)
  - NO-GO 路径:B4.2 cancel(G3 shadow 留作 author 模式分支),Sprint 5 缩到 0 人日 cleanup
  - PARTIAL 路径:按 counterfactual margin 选择性 cleanup(~3-6 人日)
```

---

## 5. Cross-flag interaction matrix(承自 v4.0 §5)

承自 v4.0 §5 + v4.0 §3bis 矩阵,无大变更。新增一行:

| 新 PR | × 既有 | 处理 |
|---|---|---|
| **F1/F12 PR0.5** | × `rag_service.py:330 query()` legacy entry | sentinel ON 时双 entry 都加 L0 skip 守护;legacy entry 内 `if not settings.ENABLE_R8_L0: skip RAG#0 lookup` |

---

## 6. PR 拆分(14 PR)

### 6.0 PR0 — LLM_API_CIRCUIT(1 人日,Sprint 0)
承自 v4.0 §6.0,无变更。

### 6.0.5 PR0.5 — `ENABLE_R8_L0` 子 flag(0.7 人日,Sprint 0)
**v5 修正(F1/F12)**:
- 路径改 **`backend/agents/hierarchical_rag.py:908`**(`query_hierarchical`)
- **新增 `backend/agents/services/rag_service.py:330 query()`** legacy entry 也加 L0 skip 守护
- 单元测试:R12 sentinel ON 时,**两 entry** 都跳 RAG#0

### 6.0.6 PR0.6 — Sprint 0 sentinel stamp 补(0.5 人日,Sprint 0)
**v5 新增(F6 P0 fix)**:补 3/6 sentinel 缺失 stamp(R12 decision SQL 前置)

| 文件 | 改动 |
|---|---|
| `backend/agents/graph/nodes/hypothesis_mutate.py`(R1b.2 mutate 路径)| 每次成功 mutate 后 stamp `alpha.metrics['_r1b_mutation_triggered']=True` + `mutation_parent_hypothesis_id` |
| `backend/services/hypothesis_service.py:fetch_cross_task_promoted`(G8 forest reuse)| 每次跨 task hypothesis 注入 prompt 时,**新 alpha** 上 stamp `alpha.metrics['_hypothesis_forest_reference']=hypothesis_id_list` |
| `backend/agents/sim_cache.py`(R9 cache hit)| 每次 cache hit 时 stamp `alpha.metrics['_simulation_cache_hit']=True` + cache_key prefix(forensic)|

`_g5_crossover_parent_ids` / `_g3_ast_originality_blocked` / `_r10_family_cap_dropped` 已存在(v4 Round-A 已 verify),无需补。

**验收**:Sprint 0 末跑 SQL `SELECT DISTINCT jsonb_object_keys(metrics) FROM alphas WHERE created_at > NOW()-interval '1 day'` 应见 3 新 key;Sprint 末 6/PARTIAL counterfactual UNION SQL 跑得通。

### 6.0.7 Sprint 0 Spike — production baseline(0.25 人日)
承自 v4.0 §6.0.6,无变更。

### 6.1 A1 — R12 LLM_MODE=assistant + sentinel guard(9.5 人日,Sprint 1)

**v5 修正(F3 + F4 + F7)**:

| v5 fix | 改动 |
|---|---|
| F3 zombie key 修字 + 加 KEY | `resolve_mode_and_enforce_sentinel` drain step 改成 5 keys:`g5_pending_offspring` / `__pending_hypothesis` / `__g5_consumed_offspring` / `__r1b_consumed_pending_hypothesis`(**修字**)/ `__BANDIT_CONFIG_KEY`(新加);**显式不清 `brain_role_snapshot`**(影响 BRAIN role)|
| F4 audit Alembic 补 ADD COLUMN | `j1a2b3c4d5e6_feature_flag_audit_sentinel`:`ADD COLUMN task_id INTEGER REFERENCES mining_tasks(id) ON DELETE SET NULL` + `ADD COLUMN sentinel_trigger_for VARCHAR(64)` + **`ADD COLUMN restored_at TIMESTAMP NULL`** + **`ADD COLUMN restored_by VARCHAR(64)`**;`action` 字段保留 VARCHAR(20) free-form,加新值 `'sentinel_set'` / `'sentinel_restored'`(backward-compatible)|
| F7 audit Timeline 防混杂 | `backend/services/feature_flag_service.py:751 list_audit` 加 param `include_sentinel: bool = False`,默认 filter `WHERE sentinel_trigger_for IS NULL`;前端 ops `/ops/feature-flags` Timeline 加 toggle "show sentinel-triggered rows" |
| restore_sentinel | `feature_flag_service.restore_sentinel()` 查 `WHERE sentinel_trigger_for='ENABLE_LLM_ASSISTANT_MODE' AND restored_at IS NULL`(F4 后该 column 存在)→ UPSERT each prior_value → stamp `restored_at = NOW(), restored_by = current_user` |

其余承自 v4.0 §6.1。

### 6.2-6.4 A2/A3/A4 承自 v4.0,无变更

### 6.5 R13-spike + 6.6 G9-spike 承自 v4.0,无变更

### 6.7 R10-calib spike(1.5 人日,Sprint 2)

**v5 修正(F2)**:`alphas` 表无 `daily_pnl` 列,改 **Python-side `CorrelationService` aggregate**:

```python
# Sprint 2 PR1 spike
from backend.services.correlation_service import CorrelationService
from backend.models import Alpha

# Top-100 PASS alpha by region × pillar
pass_alphas = await Alpha.query.filter(
    quality_status='PASS',
    created_at > now() - interval('30 days')
).order_by(Alpha.sharpe.desc()).limit(100)

# CorrelationService.batch_pairwise_correlations() async 拉 BRAIN PnL series
corr_matrix = await CorrelationService.batch_pairwise_correlations(
    [a.id for a in pass_alphas],
    metric='daily_pnl'
)

# 分析:同 family_signature 内 pairwise corr 分布
within_family_p95 = ...
within_family_p99 = ...
print(f"FAMILY_BAN_MIN_PAIRWISE_CORR calibration: p95={within_family_p95:.3f}, p99={within_family_p99:.3f}")
```

输出校准 `FAMILY_BAN_MIN_PAIRWISE_CORR`,目标 p95-p99 中位。

**前置 BRAIN sim 配额**:N=100 alpha × pairwise = ~5,000 pair × ~0.5s/pair = ~40 min BRAIN call;Sprint 2 day 1 内 background 跑,Sprint 2 末出结果。

### 6.8-6.9 B1 R11 + B2 R13 承自 v4.0,无变更

### 6.10 B3 — R10-v2 + evaluation.py:2844 stamp-only(2.5 人日,Sprint 2)

**v5 修正(F5)**:**`family_classifier.py:apply_family_cap` 已是 stamp-only,无需重构**。真正改动:

| 文件 | 改动 |
|---|---|
| `backend/agents/graph/nodes/evaluation.py:2844` | `_a.quality_status = "FAIL"` 改成 `_a.metrics["_r10_family_cap_dropped"]=True`(已 stamp,确认行为不变);FAIL 标记延迟到本节点末统一 finalize |
| `backend/family_classifier.py` 加 `apply_family_hard_ban()` | pairwise corr ≥ τ 时 stamp `metrics["_r10v2_hard_banned"]=True`,**不 set FAIL** |
| `backend/agents/graph/nodes/evaluation.py` finalize 段 | 末加 "若 `_r10_family_cap_dropped` 或 `_r10v2_hard_banned`,set quality_status='FAIL'";延迟统一 finalize |

**stamp reader 7 caller 契约**(承自 v4 Round-A 发现):`dag_state.py:670` / `hierarchical_rag.py:822` / persistence / DAG / 测试 — 全部读 stamp 不读 quality_status,契约不变(F5 verification 确认)。

**互验决策 SQL**(承自 v4.0 §6.10,key 已修正):
```sql
WITH r10_decisions AS (
  SELECT
    COUNT(*) FILTER (WHERE metrics->>'_r10_family_cap_dropped' = 'true') AS r10_drops,
    COUNT(*) FILTER (WHERE metrics->>'_r10v2_hard_banned' = 'true') AS r10v2_bans,
    -- false positive = stamp 触发但实际 PASS(双 shadow 期间不真 reject,可观察)
    COUNT(*) FILTER (WHERE metrics->>'_r10_family_cap_dropped' = 'true' AND quality_status='PASS') AS r10_fp,
    COUNT(*) FILTER (WHERE metrics->>'_r10v2_hard_banned' = 'true' AND quality_status='PASS') AS r10v2_fp,
    COUNT(*) FILTER (WHERE quality_status='PASS') AS total_pass
  FROM alphas WHERE created_at > NOW() - INTERVAL '7 days'
)
SELECT *, r10v2_fp::float / GREATEST(r10v2_bans, 1) AS r10v2_fp_rate FROM r10_decisions;
```

### 6.11 B5 — R8-v3 cognitive layer 7-layer(6.5 人日,Sprint 3)
承自 v4.0 §6.11,无变更。

### 6.12 A5.1 — G10 PR1(4 人日,Sprint 3)
承自 v4.0 §6.12,无变更。

### 6.13 A5.2 — G10 PR2(2.5 人日,Sprint 4)
承自 v4.0 §6.13,无变更。

### 6.14 B4.1 — G3-v2 grammar-aware(3 人日,Sprint 4)

**v5 修正(F10)**:**B4 拆 2 PR**。B4.1 在 Sprint 4 ship,仅新增 G3-v2 path,**不动 G3 shadow code**(freeze 约束保留)。

工时:
- B4 G3-v2 本身(grammar_validator + lark grammar 子集 + whole-output retry + fallback) 3.0

**G3 shadow code 完全保留**:flag `ENABLE_AST_ORIGINALITY_GATE` default OFF + 标 `@deprecated_pending_r12_decision`,实际 code path 在 Sprint 5 cleanup(B4.2)。

### 6.14b B4.2 — 完整 retire G3 shadow(3 人日,Sprint 5,**条件性**)

**v5 新增(F10)**:Sprint 5,**R12 decision 之后**:

| R12 decision | B4.2 行为 |
|---|---|
| GO | ship B4.2 完整 retire(frontend 409 行 + alpha_originality.py 427 行 deprecate + 3 测试 rewrite + ops endpoint retire + lifecycle docs);3 人日 |
| NO-GO | **cancel B4.2**;G3 shadow 保留作 author 模式分支;G3-v2 作 supplementary(两者并存)|
| PARTIAL | 看 G3 在 PARTIAL 列表(若 G3 counterfactual margin > +5% restore 则 cancel,否则 retire);3 人日 |

### 6.15 Sprint 4 闭环(1.5 人日,Sprint 4)
承自 v4.0 §6.15,无变更。

---

## 7. Sprint 拆分(v5.0)

### Sprint 0 — 前置 spike + sentinel stamp 补(2026-05-20 ~ 05-22 / **2.45 人日**)
| PR | 人日 |
|---|---|
| PR0 LLM_API_CIRCUIT | 1.0 |
| PR0.5 ENABLE_R8_L0 子 flag(双 entry skip) | 0.7 |
| **PR0.6 sentinel stamp 补(3/6 missing)** | **0.5** ← F6 fix |
| Spike R14 + R12 baseline | 0.25 |

**GO 标准**:PR0/PR0.5 default ON + PR0.6 跑完 SQL `jsonb_object_keys` 见 3 新 key + spike 结果写进 plan。

### Sprint 1 — R12 critical + P0 风险口(2026-05-22 ~ 06-04 / 12.8 人日 / 2 周)
| PR | 人日 |
|---|---|
| A1 R12(F3 zombie + F4 restored_at + F7 audit filter) | 9.5 |
| A2 R14 + race fix | 1.8 |
| A3 flat-F4 cross-region | 2.0 |
| A4 AQR Kelly KB seed + baseline rebase | 1.0 |

worktree:A1 merge first → A2/A3/A4 rebase。Single main worker(F11)+ worktree agent 协作 A2/A3/A4 并行 rebase。

**GO 标准**:全 4 PR ship + R12 sentinel guard cross-flag test 全 PASS + `feature_flag_audit` 加 `task_id` + `sentinel_trigger_for` + **`restored_at`** Alembic ✓ + Sprint 1 末点立刻进 R12 30d obs。

### Sprint 2 — 评估+风控 + 双 spike + 互验(2026-06-05 ~ 06-18 / **11.5 人日** / 2 周)
| PR | 人日 |
|---|---|
| R13-spike daily PnL | 0.5 |
| G9-spike portfolio | 1.5 |
| **R10-calib Python-side(F2 fix)** | **1.5** ← was 0.5 |
| B1 R11 capacity | 2.0 |
| B2 R13 factor_lens shadow | 3.5 |
| **B3 R10-v2 + stamp-only(F5 fix)** | **2.5** ← was 4 |

**GO 标准**:全 6 PR ship + R10/R10-v2 双 stamp ✓ + 互验 7d obs 起步(6/18 ship + 7d = 6/25 期满)。

### Sprint 3 — 学界 SOTA Part 1 + R10 互验决策(**2026-06-26 ~ 07-09** / 10.5 人日 / 2 周)  ← F9 fix
| PR | 人日 |
|---|---|
| R10/R10-v2 互验决策(6/26 起步前 6/25 期满) | 0(in spike output) |
| B5 R8-v3 cognitive layer 7-layer | 6.5 |
| A5.1 G10 PR1 | 4.0 |

**R12 decision point — 2026-07-04 ± 5d**(Sprint 3 中段):
- counterfactual SQL UNION 6 sentinel 全跑得通(F6 fix 后 3/6 stamp 已补)
- 三路径:GO / NO-GO / PARTIAL by margin
- Sprint 5 内容由 decision 结果定

**GO 标准**:全 2 PR ship + 互验决策落地 + **R12 decision 输出 audit trail**(audit 表 sentinel_trigger_for 行 + `restored_at` stamp(若 restore)).

### Sprint 4 — 学界 SOTA Part 2 + B4.1 G3-v2 grammar(2026-07-10 ~ 07-19 / **7 人日** / 1.5 周)  ← F8 fix Ship date
| PR | 人日 |
|---|---|
| A5.2 G10 PR2 | 2.5 |
| **B4.1 G3-v2 grammar(不动 G3 shadow code,F10 fix)** | **3.0** |
| 闭环:baseline × 3 + canary SOP + lifecycle docs | 1.5 |

**GO 标准**:全 3 PR ship + Sprint 4 末点 R12 decision 已落地(7/4 已过 6 天)+ Sprint 5 内容已规划(per decision 路径)。

### Sprint 5 — R12 decision 后 cleanup(2026-07-20+ / **3-9 人日**,条件性)
| R12 decision | Sprint 5 内容 | 人日 |
|---|---|---|
| GO | B4.2 retire G3 + 6 sentinel 永久 cleanup | ~9(3 + 6) |
| NO-GO | cancel B4.2 + 解 freeze 约束 + 6 sentinel restore 完成度验证 + ~0.5 人日 lessons learned memo | ~1 |
| PARTIAL | 选择性 cleanup by margin + lifecycle docs | 3-6 |

---

## 8. 风险 / 反例(承自 v4.0,无大变更)

承自 v4.0 §8,新增 F4 风险:audit 表 ALTER ADD COLUMN 需 production 数据库 Alembic upgrade 成功,**先在 staging 跑** + Alembic guard(`inspector.has_column`)防 idempotent re-run。

---

## 9. 验收 / 退役标准(承自 v4.0,v5 fix 项加入)

### 9.1 Phase 4 整体 ship 完成标准

| L | 标准 |
|---|---|
| L1 代码 | 14 PR 全 master,unit + integration + cross-flag test 全 PASS,baseline × 3 rebase |
| L2 flag | 10 主 flag + ~14 sub-config 全双文件注册 |
| L3 operational | 9 ops endpoint LIVE + 前端 Monitor 页 + flag_lifecycle.md 更新 + canary SOP §1 inventory 更新 + audit Timeline 加 sentinel toggle(F7)|

### 9.2 freeze 约束(v5 修正)

| Sprint | 6 sentinel code path 状态 |
|---|---|
| Sprint 0 | LIVE,无变更 |
| Sprint 1 | flag default OFF + 标 `@deprecated_pending_r12_decision`;不动 code path |
| Sprint 2-3 | 同上 |
| Sprint 4 | **G3 仍 freeze**(B4.1 仅新增 G3-v2 path,**不动 G3 shadow code**)|
| **R12 decision point 7/4 ± 5d** | freeze 约束按 decision 结果解 |
| **Sprint 5** | 按 decision 路径条件性 cleanup |

### 9.3 Phase 5 触发条件(承自 v4.0)

---

## 10. v1.0 → v5.0 演化总结

| 版本 | 设计哲学 | 人日 | Sprint | review 状态 |
|---|---|---|---|---|
| v1.0 | 浅吸收 | 26 | 3 | 3 轮 v1 review 6/5/4,19 项 fix → v2.0 |
| v2.0 | 防御 | 23 | 4 | 接受 v1 fix,但 user 推翻 → v3.0 |
| v3.0 | 激进无包袱 | 32 | 4 | 3 轮 v3 review 6.5/4.5/3.5,19 项 MUST → v4.0 |
| v4.0 | 激进 + freeze 约束 + 工程现实 | 48 | 5 | v4 Round-A review 6/10,5 fix 漏洞 + 2 P0 + 4 日期矛盾 → v5.0 |
| **v5.0** | **v4.0 fix-only,无架构变化** | **48** | **5** | **本版,综合 v4 Round-A 26 项 fix(v3 19 + v4 5 + P0 2),ship-candidate final** |

**v4 → v5 关键修正路径**:
- F1/F12 R8 路径错 → 双 entry sentinel skip(`query_hierarchical` + `query()` legacy)
- F2 R10-calib SQL 跑不通 → Python-side `CorrelationService.batch_pairwise_correlations`
- F3 zombie key 修字 + 加 `__BANDIT_CONFIG_KEY`
- F4 audit Alembic 补 `restored_at` + `restored_by` ADD COLUMN
- F5 family_classifier 真 caller 1 → 改 evaluation.py:2844 stamp-only,B3 工时 4→2.5
- F6 (P0) sentinel stamp 补 3/6(R1b/G8/R9),Sprint 0 加 0.5 人日
- F7 (P0) audit Timeline 加 sentinel filter
- F8 R12 decision date 7/15 错 → 真实 7/4 ± 5d
- F9 Sprint 3 起步 6/19 → 推 6/26(等 R10 互验 7d 期满)
- F10 B4 拆 2 PR:B4.1 G3-v2 Sprint 4 不动 G3 shadow / B4.2 retire G3 推 Sprint 5(R12 decision 之后)
- F11 worker 模型明示:1 主 worker + worktree agent 协作

---

## 11. 关联文档

- v1.0~v4.0 历史 4 版本均归档,各版本承载不同 review round 共识
- v4 Round-A review report(agent transcripts)— 临时文件,本 plan §0 已综合
- [`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)
- [`flag_lifecycle.md`](flag_lifecycle.md)
- [`production_canary_sop_2026_05_18.md`](production_canary_sop_2026_05_18.md)

---

*v5.0 是 post-v4-review ship-candidate final。所有 26 项 fix(v3 19 + v4 5 + P0 2)in-plan resolved。**ship-ready,可直接开 Sprint 0**(2026-05-20 ~ 05-22,2.45 人日)。Phase 4 ship 预期 2026-07-19(Sprint 4 末)+ Sprint 5 R12 decision 后 cleanup(7/20+,条件性 1-9 人日)。*
