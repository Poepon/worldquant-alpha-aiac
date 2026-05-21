# Phase 4 A+B 落地方案 v4.0 — post-3-round-v3-review ship-ready

> **版本**:v4.0(综合 v3-A/B/C 三轮 review,**ship-candidate**)
> **日期**:2026-05-19
> **取代**:[`phase4_a_b_plan_v3_2026-05-19.md`](phase4_a_b_plan_v3_2026-05-19.md)(v3.0 评分 v3-A 6.5 / v3-B 4.5 / v3-C 3.5,共 19 项 MUST fix)
> **scope**:13 PR / **~48 人日**(v3 32 → +50%)/ **5 sprint**(v3 4 → +1)
> **设计哲学**:沿用 [[feedback_no_historical_baggage]] 收益导向,但 [[feedback_no_re_ask_when_review_clear]] 教训 — *工程现实必须算清楚*
> **承诺约束**:
> - R12 critical path 保留,但加 **Sprint 1-4 freeze 约束**(6 sentinel 机制 code path 禁止 delete)
> - "1-click restore" 改 "audited restore"(单事务 + 完整 prior-state)
> - 全局 single-tenant flag 模型(明示选择)— 不上 task-scope 的 25 callsite 重构
> - ship date:**2026-07-12**(v3.0 错估的 6/19 → 实际 7/12,R12 decision point 7/15±5d)

---

## 1. 摘要

| 维度 | v1.0 | v2.0 | v3.0 | **v4.0** |
|---|---|---|---|---|
| PR 总数 | 10 | 11 | 12 | **13**(+R8 L0 子 flag PR0.5)|
| 人日 | 26 | 23 | 32 | **~48** |
| Sprint 数 | 3 | 4 | 4 | **5** |
| Ship date | 6/19 | 6/19 | 6/19(错估)| **7/12** |
| R12 decision point | n/a | 6/19 | 6/19(错估)| **7/15 ± 5d** |
| freeze 约束 | 无 | 无 | 无 | **Sprint 1-4 6 sentinel path 禁 delete** |

---

## 2. 设计原则

承自 v3.0 的 8 原则不变。**新增第 9 原则**(post-v3-review):

| # | 原则 | 来源 |
|---|---|---|
| 9 | **激进推翻配 freeze 约束** — 推翻已 ship 机制时,*deprecate path code 在 decision point 之前禁止 delete*(标 `@deprecated_pending_X_decision`);只允许 flag default OFF,实际 cleanup 推 decision point 之后的下一 sprint;**audit schema 必须能 ground-truth 重建 prior state**(`prior_override_value` + `prior_action` + `restored_at`)| v3-B Round 致命发现 #7 + #2 |

---

## 3. 决策矩阵(v3 review 综合 19 MUST fix)

### 3.1 v3-A MUST fix 6 项

| # | v3-A 发现 | v4.0 处理 |
|---|---|---|
| A-1 | R12 sentinel global vs task-scope 语义混乱 | **明示选择全局 single-tenant** — production 当前 33 flag 全局 ON,task-scope 重构 ~25 callsite 不值。R12 ON 全局 disrupt 已 ship 6 机制是接受风险(per [[feedback_no_historical_baggage]]) |
| A-2 | R8 L0-only sentinel 当前代码不可实施 | **PR0.5**(Sprint 0 内,0.5 人日)— `ENABLE_R8_L0` 子 flag + `query_hierarchical` 加 L0 skip 逻辑 |
| A-3 | R10 互验 SQL key 写错 | SQL 改 `_r10_family_cap_dropped` 真实 stamp;R10-v2 PR 同时新增 `_r10v2_hard_banned` stamp |
| A-4 | `FAMILY_BAN_MIN_PAIRWISE_CORR=0.85` 未 calibrate | **Sprint 2 前 0.5 人日 spike** 跑历史 alpha pairwise corr 分布,calibrate τ |
| A-5 | R12 zombie code drain 未指定 | `resolve_mode_and_enforce_sentinel` 加 drain step — POST 时清空 `task.config["g5_pending_offspring"]` / `__pending_hypothesis` / `__g5_consumed_offspring` / `__r1b_consumed_hypothesis` + audit INSERT(被清 keys + values forensic)|
| A-6 | B4 retire G3 frontend Monitor 同步缺失 | B4 PR 内加 frontend `G3OriginalityMonitor.jsx` 409 行 + `OpsLayout.jsx` 路由 + `api.js` endpoint tear-down 子任务 |

### 3.2 v3-B MUST fix 7 项

| # | v3-B 发现 | v4.0 处理 |
|---|---|---|
| B-1 | RUNNING task 黑洞(`resolve_mode_and_enforce_sentinel` 只 POST 路径)| 状态机三态:**new POST**(走 sentinel guard 路径)/ **RUNNING task**(grandfather,round-end hook 切 + audit)/ **PAUSED task**(resume 时走 POST 路径)|
| B-2 | audit schema 缺 `prior_override_value` | **复用既有 `feature_flag_audit` 表 + ADD COLUMN `task_id`**(per v3-A 设计要点 1)— 既有表已含 `old_value` / `new_value` / `action`,逻辑天然 prior state 重建 |
| B-3 | task.config["overrides"] 凭空字段 | **删除该字段**,改全局 single-tenant(同 A-1)— sentinel ON 走 `feature_flag_service.set(flag_name, False)` 全局 override,read 路径 `Settings.__getattribute__` 自动生效 |
| B-4 | R9 simulation cache key 在 assistant 模式崩塌 | `SimulationCache` 表 ADD COLUMN `source_mode VARCHAR(16) DEFAULT 'author'`;assistant 期写入 stamp 'assistant_synth';read 默认 filter `source_mode='author'`,restore 后不污染 |
| B-5 | G5/R1b pending residue zombie | 同 A-5,drain step 加 audit forensic |
| B-6 | PARTIAL 判定标准缺失 | Sprint 5 R12 decision point SQL:`WITH r12 AS (...) SELECT counterfactual_margin = (r12_pass_rate - author_pass_rate) FROM ... GROUP BY sentinel_flag`;rule:margin > +5% → restore;margin in [-5%, +5%] → PARTIAL by cost (R8 L0 + R9 restore);margin ≤ -5% → permanent deprecate |
| B-7 | **Sprint 3 B4 retire G3 path 与 Sprint 3 末 R12 decision 冲突** | **B4 推到 Sprint 4(R12 decision 之后),Sprint 3 不动 G3 code 路径**;v4.0 §6 freeze 约束 |

### 3.3 v3-C MUST fix 6 项(工时)

| # | v3-C 发现 | v4.0 处理 |
|---|---|---|
| C-1 | 总 32 → 真实 ~46-52 人日 | **v4.0 调到 48 人日**(取中位)|
| C-2 | Sprint 1 11.8 人日 7d 不可执行 | Sprint 1 拉到 **2 周**(5/22-6/4),12-13 人日 |
| C-3 | R10-v2 互验需 family_classifier 重构(stamp-only mode)| B3 工时 2 → **4 人日**;明示重构 11 caller 契约 |
| C-4 | B4 retire G3 真成本 6 人日 | B4 工时 3 → **6 人日** + 移 Sprint 4 |
| C-5 | B5 R8-v3 7 layer 4.5 → 6.5 人日 | B5 工时 4.5 → **6.5 人日** |
| C-6 | G10 similarity 算法未定 + 4 人日不够 | A5 工时 4 → **6.5 人日**;**明示 similarity = token Jaccard**(复用 `alpha_originality.py` subtree pattern,无新依赖) |

### 3.4 共识 SHOULD(选择性采纳)

| # | review | v4.0 |
|---|---|---|
| v3-A SHOULD #7 | R10 互验 mode 改 double-shadow(都仅 stamp 不真 reject)| **采纳** — 互验期 7d,两机制 stamp-only,真 reject 推 R12 decision 之后 |
| v3-A SHOULD #6 | R12 GO gate 引用 production baseline | Sprint 0 加 0.25 人日 spike `SELECT COUNT(*) FILTER(WHERE quality_status='PASS')::float / COUNT(*) FROM alphas WHERE created_at > NOW()-interval '30 days'` 写进 plan |
| v3-B 高风险 #1 | KS test 改 bootstrap effect size + 80% CI overlap | **采纳** — GO gate 改 effect size > -10% PASS rate + 80% CI 不跨 0;stratified by region |
| v3-A SHOULD #5 | G9 spike PASS/FAIL 标准 | PASS = "BRAIN 有 `/portfolio/*` endpoint AND consultant tier 可调通 OR 自建 simulator ≤4 人日估算" |
| v3-A SHOULD #3 | G10 LLM cost guard fallback | **采纳** — fallback to 上周残余 active logic + staleness flag in prompt |
| v3-A 设计要点 #1 | audit 表复用 `feature_flag_audit` 加 task_id | **采纳**(已纳 B-2)|
| v3-C 隐含成本 | frontend tear-down + fixture rewrite + family_classifier 重构 + baseline rebase × 3 + lifecycle docs + token budget 联调 | 全分摊到对应 PR 工时内 |

---

## 4. PR 依赖图(v4.0)

```
Sprint 0 (前置 spike / 1.75 人日)
├─ PR0    LLM_API_CIRCUIT default ON                     1.0
├─ PR0.5  ENABLE_R8_L0 子 flag(L0 selective skip)        0.5  ← v3-A fix
└─ Spike  R14 PASS_RATE_FLOOR + R12 author baseline      0.25  ← v3-A SHOULD

Sprint 1 (R12 critical + P0 风险口 / 12.8 人日 / 2 周)
├─ A1   R12 LLM_MODE=assistant + sentinel guard           9.5  ← v3-C fix
│       worktree 1: A1 merge first(改 feature_flag_service.py + config.py)
├─ A2   R14 task_stop_loss + race fix                     1.8
├─ A3   flat-F4 cross-region                              2.0
├─ A4   AQR Kelly KB seed + baseline rebase               1.0
└─ (worktree 2/3/4 rebase 顺序:A2 → A3 → A4 都 rebase 在 A1 之后,避免 feature_flag_service.py 冲突)

Sprint 2 (评估+风控补强 + 双 spike / 11.0 人日 / ~2 周)
├─ R13-spike  BRAIN sim daily PnL feasibility             0.5
├─ G9-spike   portfolio simulator feasibility             1.5  ← v3-C 时序 fix
├─ R10-calib  pairwise corr 阈值 calibrate                0.5  ← v3-A fix
├─ B1   R11 alpha_capacity                                2.0
├─ B2   R13 factor_lens shadow                            3.5
└─ B3   R10-v2 + family_classifier stamp-only 重构 + 互验  4.0  ← v3-C fix

Sprint 3 (学界 SOTA Part 1 + R10/R10-v2 决策 / 10.5 人日 / ~2 周)
├─ R10/R10-v2 互验 7d obs 决策(Sprint 3 起步前完成)
├─ B5   R8-v3 cognitive layer 7-layer 全实现             6.5  ← v3-C fix
└─ A5.1 G10 logic-as-asset PR1(distill 写表 + cron + ops endpoint) 4.0

Sprint 4 (学界 SOTA Part 2 + B4 retire G3 + 闭环 / 10.0 人日 / ~2 周)
├─ A5.2 G10 PR2(prompt 注入 + refine chain logic)        2.5
├─ B4   G3-v2 grammar-aware + 完整 retire G3 shadow      6.0  ← v3-B fix(推 Sprint 4)
└─ Doc  baseline rebase × 3 + canary SOP + flag_lifecycle.md + token budget 联调  1.5

Sprint 末 (2026-07-15 ± 5d) — R12 30d obs decision point
├─ counterfactual margin SQL 跑出 6 sentinel 各自 PARTIAL 判定
├─ 决策 GO / NO-GO / PARTIAL by counterfactual margin
└─ freeze 约束解除 → Sprint 5 cleanup 永久 deprecate 路径(此 plan 不含 Sprint 5)
```

**Critical path**:PR0/PR0.5/Spike → Sprint 1 A1 R12 sentinel guard(merge 首)→ Sprint 2 R10 互验 → Sprint 3 决策 → Sprint 4 retire G3 + 闭环 → Sprint 末 R12 decision。

---

## 5. Cross-flag interaction matrix(v4.0 更新)

v3.0 §3bis 15 行 matrix 仍有效,**v4.0 更新 R12 行**:

| 新 PR | 与既有 flag interaction | v4.0 处理 |
|---|---|---|
| R12 critical | × R1b mutate / G5 / G8 / G3 | **全局 sentinel disable**(set DB override OFF)+ audit 用既有 `feature_flag_audit` 表;不动 6 机制 code path(freeze 到 Sprint 5+)|
| R12 | × R8 (L0-only) | **PR0.5 加 `ENABLE_R8_L0` 子 flag**;sentinel disable L0 only,L1/L2/L3 保留 |
| R12 | × R9 cache | `SimulationCache` 加 `source_mode` 列;assistant 期 stamp,restore 后不污染 |
| R12 | × **正在跑的 RUNNING task** | grandfather + round-end hook 切;clear pending residue keys + audit |
| R10-v2 互验 | × R10 family-cap | **double-shadow**:两机制 stamp-only 不真 reject,7d obs 后 counterfactual SQL 决策 |
| B4 G3-v2 retire | × G3 shadow | **推到 Sprint 4(R12 decision 之后)** — Sprint 1-3 G3 code 不动 |
| A5 G10 | × R8 KB | 独立表 + 独立 prompt block + token budget guard 联调 B5 |

---

## 6. PR 拆分(13 PR)

### 6.0 PR0 — LLM_API_CIRCUIT(1 人日,Sprint 0)

承自 v3.0 §4.0,无变更。

### 6.0.5 PR0.5 — `ENABLE_R8_L0` 子 flag(0.5 人日,Sprint 0)

**新增**(v3-A MUST #2 fix):

```python
ENABLE_R8_L0: bool = True   # default ON;R12 sentinel ON 时全局 set False
```

**代码改动**:
- `backend/services/hierarchical_rag.py:query_hierarchical` orchestrator 加 L0 skip 逻辑(`if not settings.ENABLE_R8_L0: skip layer 0`)
- `backend/services/feature_flag_service.py` SUPPORTED_FLAGS 注册
- 单元测试:L0 skip + L1/L2/L3 fall-through 行为不变

### 6.0.6 Sprint 0 Spike — production baseline(0.25 人日)

跑 SQL:
```sql
SELECT
  COUNT(*) FILTER (WHERE quality_status='PASS')::float / COUNT(*) AS author_pass_rate_30d,
  percentile_cont(0.05) WITHIN GROUP (ORDER BY pass_n::float/total_n) AS r14_pass_rate_floor_p5
FROM (
  SELECT task_id, round_num,
    COUNT(*) FILTER (WHERE quality_status='PASS') as pass_n,
    COUNT(*) as total_n
  FROM alphas WHERE created_at > NOW() - INTERVAL '30 days'
  GROUP BY task_id, round_num
) round_stats;
```

输出写进 plan §6.1 R12 GO gate 段 + R14 PASS_RATE_FLOOR 校准。

### 6.1 A1 — R12 LLM_MODE=assistant + sentinel guard(9.5 人日,Sprint 1)

**v4.0 fix list**:

| v3 MUST | v4.0 fix |
|---|---|
| A-1 全局 vs task-scope | **明示选择全局 single-tenant** — single tenant 是 AIAC 现状(33 flag 全 process-global) |
| B-1 RUNNING task 黑洞 | 状态机三态(new POST / RUNNING grandfather + round-end / PAUSED resume) |
| B-2 audit schema 缺 prior_value | **复用 `feature_flag_audit` 表 + ADD COLUMN `task_id`**(单 Alembic ADD COLUMN,不新表) |
| B-3 task.config["overrides"] 凭空 | **删除该字段**,全部走 `feature_flag_service.set()` 全局 override |
| B-4 R9 cache 污染 | `SimulationCache` ADD COLUMN `source_mode` |
| A-5 / B-5 zombie drain | sentinel guard 清 4 keys + audit forensic |
| C 工时 6→9.5 | template 模板库(~1 人日)+ KS→bootstrap GO gate(~0.5)+ 6 stub(~2)+ cross-flag transactional pattern(~0.5)|

**ENABLE_* flag**:
```python
ENABLE_LLM_ASSISTANT_MODE: bool = False
LLM_ASSISTANT_SENTINEL_FLAGS: list = [
    "ENABLE_R1B_HYPOTHESIS_MUTATE",
    "ENABLE_G5_CROSSOVER",
    "ENABLE_HYPOTHESIS_FOREST_REUSE",
    "ENABLE_R8_L0",                  # 替换 v3.0 写的 ENABLE_HIERARCHICAL_RAG
    "ENABLE_AST_ORIGINALITY_GATE",
    "ENABLE_SIMULATION_CACHE",
]
```

**Alembic**:`j1a2b3c4d5e6_llm_assistant_audit_extend`(down_revision = "i9e4d0a3f7c2")
```sql
-- 复用既有 feature_flag_audit + 加 task_id
ALTER TABLE feature_flag_audit ADD COLUMN task_id INTEGER REFERENCES mining_tasks(id) ON DELETE SET NULL;
ALTER TABLE feature_flag_audit ADD COLUMN sentinel_trigger_for VARCHAR(64);  -- 'ENABLE_LLM_ASSISTANT_MODE' for R12 sentinel-triggered rows
CREATE INDEX ix_ffa_sentinel_trigger ON feature_flag_audit(sentinel_trigger_for) WHERE sentinel_trigger_for IS NOT NULL;

-- R9 cache source_mode
ALTER TABLE simulation_cache ADD COLUMN source_mode VARCHAR(16) DEFAULT 'author' NOT NULL;
CREATE INDEX ix_sim_cache_source_mode ON simulation_cache(source_mode);
```

**代码改动**:
| 文件 | 改动 | 工时 |
|---|---|---|
| `backend/services/llm_mode_service.py` | **新**:`resolve_mode_and_enforce_sentinel(task, ctx) -> Mode` 状态机三态 + drain residue keys | 1.5 |
| `backend/agents/graph/nodes/generation.py:node_code_gen` | assistant 模式分支 + template 模板库 fallback | 2.0 |
| `backend/data/assistant_mode_templates/*.yaml` | **新**:5-10 个 hypothesis→expression template(每 pillar 1-2 个,token Jaccard <0.7 dedup)| 1.0 |
| `backend/services/feature_flag_service.py:set` | ENABLE_LLM_ASSISTANT_MODE=True 时联动 6 sentinel flag set(False)单事务 + audit task_id=NULL(global trigger)| 1.0 |
| `backend/services/feature_flag_service.py:restore_sentinel` | **新**:1-click restore — read latest `feature_flag_audit` WHERE sentinel_trigger_for='ENABLE_LLM_ASSISTANT_MODE' AND restored_at IS NULL → revert each | 1.0 |
| `backend/tasks/mining_tasks.py:_run_one_round_inline` | 入口加 `if state.llm_mode_used != resolved_mode: round-end hook 切` | 0.5 |
| `backend/routers/ops.py` | `/ops/llm-mode/comparison` endpoint(bootstrap effect size + 80% CI overlap + stratified by region) | 1.0 |
| 测试 + cross-flag test | 单测 + 集成 + 6 sentinel × 3 task 状态 = 18 case | 1.5 |

**GO gate(v4.0)**:
- bootstrap effect size: PASS rate diff > -10% (R12 vs author baseline,baseline 自 Sprint 0 spike)
- 80% CI 不跨 0
- stratified by region(USA/CHN/JPN/EUR/HKG 各自 PASS)
- cost ≤ 1.2× author baseline

### 6.2 A2 — R14 task_stop_loss + race fix(1.8 人日,Sprint 1)

承自 v3.0 §4.2,无变更。

### 6.3 A3 — flat-F4 cross-region(2 人日,Sprint 1)

承自 v3.0 §4.3,无变更。

### 6.4 A4 — AQR Kelly KB seed + baseline rebase(1 人日,Sprint 1)

承自 v3.0 §4.4,无变更。

### 6.5 R13-spike(0.5 人日,Sprint 2 day 1)

承自 v3.0,无变更。

### 6.6 G9-spike — portfolio simulator(1.5 人日,Sprint 2)

**v3-C fix**:工时 1 → 1.5(BRAIN API 调研 + 自建 simulator 评估 + regime conditioning 估算)。

**PASS criteria**(v3-A SHOULD #5 fix):
- BRAIN 有 `/portfolio/*` endpoint **AND** consultant tier 可调通 **OR**
- 自建 simulator ≤ 4 人日估算 **AND** regime conditioning ≤ 3 人日估算

### 6.7 R10-calib spike(0.5 人日,Sprint 2)

**新增**(v3-A MUST #4 fix):
```sql
WITH alpha_pairs AS (
  SELECT a1.family_signature, a1.id as id1, a2.id as id2,
         corr(a1.daily_pnl, a2.daily_pnl) AS pairwise_corr
  FROM alphas a1
  JOIN alphas a2 ON a1.family_signature = a2.family_signature AND a1.id < a2.id
  WHERE a1.created_at > NOW() - INTERVAL '30 days'
)
SELECT
  percentile_cont(0.95) WITHIN GROUP (ORDER BY pairwise_corr) AS p95_corr,
  percentile_cont(0.99) WITHIN GROUP (ORDER BY pairwise_corr) AS p99_corr,
  COUNT(*) AS total_pairs
FROM alpha_pairs;
```

输出校准 `FAMILY_BAN_MIN_PAIRWISE_CORR` τ:目标拦截 top 5%-1% 高相关 family,避免拦截整族 → τ 设为 p95-p99 中位。

### 6.8 B1 — R11 alpha_capacity(2 人日,Sprint 2)

承自 v2.0 §4.5,改 `calculate_alpha_score` + `evaluate_alpha_comprehensive` 双路径。

### 6.9 B2 — R13 factor_lens shadow(3.5 人日,Sprint 2)

承自 v2.0 §4.6,条件依赖 R13-spike。

### 6.10 B3 — R10-v2 + family_classifier stamp-only 重构 + 互验(4 人日,Sprint 2)

**v4.0 fix list**:

| v3 MUST | v4.0 fix |
|---|---|
| A-3 SQL key 写错 | 改 `_r10_family_cap_dropped` + 新 `_r10v2_hard_banned` |
| A-4 τ 拍脑袋 | Sprint 2 R10-calib spike 校准 |
| C-3 family_classifier 重构 | 重构 stamp-only mode(不 set FAIL),11 caller 契约更新 |
| SHOULD #7 互验 double-shadow | 两机制 stamp-only 不真 reject,7d obs 后 counterfactual SQL |

**代码改动**:
- `backend/family_classifier.py:apply_family_cap`(line 90)重构:set stamp `metrics["_r10_family_cap_dropped"]=True` 但 **不 set `quality_status="FAIL"`**;延迟到 evaluation 末统一 finalize
- 新方法 `apply_family_hard_ban`:pairwise corr ≥ τ 时 stamp `metrics["_r10v2_hard_banned"]=True`,同样不 set FAIL
- 11 caller 契约更新(per v3-C 隐含成本):evaluation.py / persistence.py / 等 — 改读 stamp,不读 quality_status='FAIL'
- 互验决策 SQL(Sprint 3 起步前跑):
```sql
WITH r10_decisions AS (
  SELECT
    COUNT(*) FILTER (WHERE metrics->>'_r10_family_cap_dropped' = 'true') AS r10_drops,
    COUNT(*) FILTER (WHERE metrics->>'_r10v2_hard_banned' = 'true') AS r10v2_bans,
    COUNT(*) FILTER (WHERE metrics->>'_r10_family_cap_dropped' = 'true' AND quality_status='PASS') AS r10_false_positive,
    COUNT(*) FILTER (WHERE metrics->>'_r10v2_hard_banned' = 'true' AND quality_status='PASS') AS r10v2_false_positive,
    COUNT(*) FILTER (WHERE quality_status='PASS') AS total_pass
  FROM alphas WHERE created_at > NOW() - INTERVAL '7 days'
)
SELECT *,
  r10_false_positive::float / GREATEST(r10_drops, 1) AS r10_fp_rate,
  r10v2_false_positive::float / GREATEST(r10v2_bans, 1) AS r10v2_fp_rate
FROM r10_decisions;
```
**胜出标准**:r10v2_fp_rate < r10_fp_rate AND r10v2_bans > 5 → R10-v2 替换;否则 R10 保留。

### 6.11 B5 — R8-v3 cognitive layer 7-layer 全实现(6.5 人日,Sprint 3)

**v3-C 工时 fix**:4.5 → 6.5。

工时 component breakdown:
- 7 cognitive layer × system prompt 200-400 字 + few-shot example(2.1)
- select_layer 3 策略(bandit Beta-Bernoulli / round_robin / deficit_aware)(1.2)
- 5-mode paraphrase(light/moderate/creative/divergent/concrete)接 G5 crossover prompt 工厂(0.8)
- 8k token budget guard + drop order(0.5)
- ops endpoint `/ops/r8-v3/prompt-token-stats` + cognitive layer hit rate(0.7)
- 测试 + cross-flag with R12 sentinel(R8 L0 disable but L1/L2/L3 + R8-v3 共存)(1.2)

### 6.12 A5.1 — G10 logic-as-asset PR1(4 人日,Sprint 3)

**Sprint 3 部分**:distill + 写表 + cron + ops endpoint(不含 prompt 注入,PR2 推 Sprint 4)。

**v3-C 隐含 fix**:similarity 算法明示为 **token Jaccard** — 复用 `alpha_originality.py` subtree pattern,无新依赖(embedding 路径砍掉,避免 OpenAI embedding cost)。

代码:
- `backend/services/logic_distill_service.py:distill_last_week_pass_alphas` 1.5 人日
- celery_app.py celery_beat_schedule 加 `weekly_logic_distill` Sunday 03:00 SH(**修正 v1.0 路径错** `scheduled_tasks.py`)0.3
- Alembic `n5e6f7g8h9i0_distilled_logic` + index 0.3
- 6 sub-config 进 SUPPORTED_FLAGS 0.3
- ops endpoint `/ops/g10/logic-library` 0.7
- LLM cost guard(`LOGIC_DISTILL_MAX_COST_USD_PER_WEEK=5.00`)+ fallback(上周残余 + staleness flag)0.4
- 测试 0.5

### 6.13 A5.2 — G10 prompt 注入 + refine chain(2.5 人日,Sprint 4)

**Sprint 4 部分**:
- `backend/services/logic_distill_service.py:refine_logic_library` 0.7
- `backend/agents/prompts/builder.py:build_distilled_logic_block`(独立 block 段)0.6
- prompts.yaml 加 distilled_logic_block + token budget 联调 B5 0.4
- retrieval 默认 active(`retired_at IS NULL`),chain forensic 0.3
- 测试 + 集成 0.5

### 6.14 B4 — G3-v2 grammar-aware **完整 retire G3**(6 人日,Sprint 4)

**v3-B/v3-C 关键 fix**:推 Sprint 4(R12 decision 之前的 final cleanup window)。

工时 component:
- B4 G3-v2 本身(grammar_validator + lark grammar 子集 + whole-output retry + fallback)3.0
- evaluation.py G3 stamp 60+ 行重写为 G3-v2 0.5
- frontend G3OriginalityMonitor.jsx 409 行删除 + OpsLayout.jsx 路由 + api.js 0.5
- backend/alpha_originality.py 427 行 deprecate 路径 0.3
- scripts/calibrate_g3_threshold.py 261 行 retire 0.2
- 测试 fixture rewrite(test_g3_alpha_originality / wiring / ops 三文件)0.8
- ops endpoint `/ops/g3/originality-stats` retire 或 redirect to G3-v2 0.3
- SUPPORTED_FLAGS group rename + flag_lifecycle.md 更新 0.2
- canary SOP §1 inventory 表更新 0.2

**注意**:B4 在 Sprint 4 ship 后,**G3 deprecate 是 Sprint 4 final cleanup window**,**不**违反 Sprint 1-3 freeze 约束。R1b/G5/G8 6 sentinel 机制仍保留 code path 到 Sprint 5+。

### 6.15 Sprint 4 闭环 PR(1.5 人日)

- baseline rebase × 3(Sprint 1 R12 ship 后 / Sprint 2 R10-v2 ship 后 / Sprint 3-4 ship 后)0.6
- canary SOP §1 inventory 表更新(33 + 8 new flag + 6 sentinel deprecate 标记)0.3
- flag_lifecycle.md 更新(8 新 flag promotion 路径 + 6 sentinel pending decision)0.3
- token budget 联调 B5 + A5(8k limit 整合测试)0.3

---

## 7. Sprint 拆分(v4.0)

### Sprint 0 — 前置 spike(2026-05-20 ~ 05-21 / 1.75 人日)
| PR | 人日 |
|---|---|
| PR0 LLM_API_CIRCUIT | 1.0 |
| PR0.5 ENABLE_R8_L0 子 flag | 0.5 |
| Spike R14 + R12 baseline | 0.25 |

**GO 标准**:PR0/PR0.5 default ON 全 mock 测试通过 + spike 结果写进 plan。

### Sprint 1 — R12 critical + P0 风险口(2026-05-22 ~ 06-04 / 12.8 人日 / 2 周)
| PR | 人日 |
|---|---|
| A1 R12 LLM_MODE + sentinel guard + 9 fix | 9.5 |
| A2 R14 + race fix | 1.8 |
| A3 flat-F4 cross-region | 2.0 |
| A4 AQR seed + baseline rebase | 1.0 |

worktree 顺序:A1 merge first(改 feature_flag_service.py + config.py)→ A2/A3/A4 rebase 并行。

**GO 标准**:全 4 PR ship + R12 sentinel guard 18 cross-flag test 全 PASS + `feature_flag_audit` 加 task_id Alembic ✓ + Sprint 1 末点立刻进 R12 30d obs。

### Sprint 2 — 评估+风控 + 双 spike + R10-v2 互验(2026-06-05 ~ 06-18 / 11.0 人日 / 2 周)
| PR | 人日 |
|---|---|
| R13-spike daily PnL | 0.5 |
| G9-spike portfolio | 1.5 |
| R10-calib pairwise corr | 0.5 |
| B1 R11 capacity | 2.0 |
| B2 R13 factor_lens shadow | 3.5 |
| B3 R10-v2 + family_classifier 重构 + 互验 | 4.0 |

**GO 标准**:全 6 PR ship + R10/R10-v2 双 stamp ✓ + 互验 7d obs 起步(到 6/25)。

### Sprint 3 — 学界 SOTA Part 1 + R10/R10-v2 决策(2026-06-19 ~ 07-02 / 10.5 人日 / 2 周)
| PR | 人日 |
|---|---|
| R10/R10-v2 互验决策(Sprint 3 起步前完成,6/18 ~ 6/25 7d 期满)| 0(in spike output)|
| B5 R8-v3 cognitive layer 7-layer | 6.5 |
| A5.1 G10 PR1(distill + cron + ops endpoint) | 4.0 |

**GO 标准**:全 2 PR ship + 互验决策落地 R10 vs R10-v2 胜出者(loser flag default OFF + 标 deprecated_pending_decision)+ Sprint 4 起步前确认 R12 obs 数据健康。

### Sprint 4 — 学界 SOTA Part 2 + B4 retire G3 + 闭环(2026-07-03 ~ 07-12 / 10.0 人日 / 1.5 周)
| PR | 人日 |
|---|---|
| A5.2 G10 PR2(prompt 注入 + refine chain) | 2.5 |
| B4 G3-v2 + 完整 retire G3 | 6.0 |
| 闭环:baseline × 3 + canary SOP + lifecycle docs + token budget 联调 | 1.5 |

**GO 标准**:全 3 PR ship + Sprint 4 末点 R12 obs 数据满 ~7 周(5/26 ~ 7/15 = 49 days,超过 30d gate)+ ready for decision。

### Sprint 末 — R12 30d decision point(2026-07-15 ± 5d)

R12 30d obs 完整(实际 5/26-7/15 = 50d,远超 30d 门槛):
- 跑 counterfactual margin SQL(per v4.0 §3.2 B-6):
```sql
WITH r12_obs AS (
  SELECT
    AVG(CASE WHEN llm_mode_used='assistant' AND quality_status='PASS' THEN 1.0 ELSE 0.0 END) AS r12_pass_rate,
    AVG(CASE WHEN llm_mode_used='author' AND quality_status='PASS' THEN 1.0 ELSE 0.0 END) AS author_pass_rate
  FROM alphas WHERE created_at > '2026-05-26'  -- R12 ship
),
sentinel_attribution AS (
  -- 对 6 sentinel 各算 counterfactual margin: if this flag had been ON, would PASS rate diff differ?
  -- 用 alpha.metrics 历史 stamp 反推
  SELECT 'ENABLE_R1B_HYPOTHESIS_MUTATE' AS flag, ...  -- 同 5 行
  UNION ALL ...
)
SELECT * FROM r12_obs, sentinel_attribution;
```
- 决策路径:
  - **GO**:effect size > -10% PASS + 80% CI 不跨 0 → R12 default ON + 6 sentinel flag 永久 deprecate(Sprint 5+ delete code path)
  - **NO-GO**:effect size ≤ -10% PASS OR 80% CI 跨 0 → 调 `feature_flag_service.restore_sentinel()` (audit 表反推 + UPSERT)→ 6 sentinel 自动 restore 到 prior state(因为 freeze 约束代码还在)
  - **PARTIAL**:counterfactual margin > +5% 的 sentinel flag restore;[-5%, +5%] PARTIAL by cost(R8 L0 / R9 cache restore)+ 其余 deprecate

---

## 8. 风险 / 反例(v4.0)

### 8.1 接受的风险

| # | 风险 | v4.0 处理 |
|---|---|---|
| 1 | R12 critical path 后 30d obs effect size 可能 ≤ -10% PASS rate | **接受** — 数据驱动 decision;若 NO-GO,1-click restore 路径完整 |
| 2 | R10 + R10-v2 互验期数据偏差 | mitigated:**double-shadow mode**(stamp-only 不真 reject),7d obs 后 counterfactual SQL |
| 3 | G10 完整 loop weekly cron LLM cost burst | mitigated:`LOGIC_DISTILL_MAX_COST_USD_PER_WEEK=5.00` + LLM_API_CIRCUIT + fallback 上周残余 |
| 4 | G9 spike 1.5 人日 sunk cost | **接受** — 换 Phase 5 决策 data-driven |
| 5 | B4 retire G3 在 Sprint 4 ship,Sprint 5+ 才完整 cleanup | **接受** — Sprint 4 是 R12 decision 前的 final retire window,与 freeze 约束不冲突(R1b/G5/G8/R8 L0/R9 5 sentinel 仍 freeze) |
| 6 | R12 ON 后 RUNNING task grandfather 期间 hypothesis chain 跨模式 attribution 失败 | mitigated:round-end hook 切;同 round 内一致;cross-round chain stamp `llm_mode_used` |

### 8.2 不接受的风险(明确反例)

- **不上 task-scope flag 模型** — 25 callsite 重构成本 > 单 tenant 全局 disrupt 风险(R12 ON 影响 production 所有 task,但 production 当前是低吞吐期可接受)
- **不上 embedding-based similarity for G10** — token Jaccard 复用既有 pattern,无新依赖
- **不在 Sprint 1-3 delete 6 sentinel 机制 code path** — freeze 约束硬要求
- **不在 Sprint 1-3 引入新非 sentinel 相关大 PR** — R12 obs 期 confounding 严重(v3-B 高风险 #6),Sprint 2-4 内的 PR(B1/B2/B3/B5/A5/B4)是 *计划内* 但与 R12 obs *逻辑独立*(不动 generation.py / sentinel guard 路径)

---

## 9. 验收 / 退役标准

### 9.1 Phase 4 整体 ship 完成标准

| L | 标准 |
|---|---|
| L1 代码 | 13 PR 全 master,unit + integration + cross-flag test 全 PASS,baseline × 3 rebase |
| L2 flag | 10 主 flag(8 + ENABLE_R8_L0 + ENABLE_LOGIC_DISTILL)+ ~14 sub-config 全双文件注册 + SUPPORTED_FLAGS |
| L3 operational | 9 ops endpoint LIVE + 前端 Monitor 页(R11/R13/R14/LLM_CB/R10-v2/cognitive/G10 各 1 chart)+ flag_lifecycle.md 更新 + canary SOP §1 inventory 更新 |

### 9.2 freeze 约束

| Sprint | 6 sentinel code path 状态 |
|---|---|
| Sprint 0 | LIVE,无变更 |
| Sprint 1 | flag default OFF + 标 `@deprecated_pending_r12_decision`;**不动 code path** |
| Sprint 2-3 | 同上;flag 可手动 ON 但默认 OFF(per sentinel guard 触发 audit)|
| Sprint 4 | **G3 退出 freeze**(B4 ship 完整 retire);其他 5 sentinel 仍 freeze |
| Sprint 末 R12 decision | freeze 约束按 decision result 解 — GO: 全 6 解 freeze 进 Sprint 5+ cleanup;NO-GO: restore + 解 freeze;PARTIAL: 选择性解 |
| Sprint 5+ | 不在本 plan 范围 |

### 9.3 Phase 5 触发条件

Phase 4 ship + ≥30d R12 obs + 满足任一:
- G9 spike PASS → Phase 5 完整 G9
- R12 decision GO → Phase 5 R1b/G5/G8 完整 cleanup
- 新 SOTA paper 与 AIAC ≥3 项 conflict

---

## 10. v1.0 → v4.0 演化总结

| 版本 | 设计哲学 | 人日 | Sprint | review 状态 |
|---|---|---|---|---|
| v1.0 | 浅吸收 | 26 | 3 | 3 轮 review 评 6/5/4,19 项 fix |
| v2.0 | 防御 / 历史包袱 | 23 | 4 | 接受 v1.0 review 19 fix,但被 user 指令推翻 |
| v3.0 | 激进 / 无历史包袱 | 32 | 4 | 3 轮 v3 review 评 6.5/4.5/3.5,19 项 MUST fix |
| **v4.0** | **激进 + freeze 约束 + 工程现实** | **48** | **5** | **本版,综合 v3 三轮 review 全 19 项 MUST + 选择性 SHOULD,ship-candidate** |

**关键修正路径**:
- v3 → v4:全局 single-tenant 明示 + audit schema 复用既有 `feature_flag_audit` + freeze Sprint 1-3 不动 6 sentinel code + B4 retire G3 推 Sprint 4 + R10-v2 互验 double-shadow + family_classifier stamp-only 重构 + R8 L0-only 子 flag + G10 similarity token Jaccard + GO gate bootstrap effect size 替代 KS test + R12 critical 工时 6→9.5 含 template 库 + B5 cognitive 4.5→6.5 + G10 4→6.5 + B4 3→6 含 frontend tear-down

---

## 11. 关联文档

- v1.0 [`phase4_a_b_plan_2026-05-19.md`](phase4_a_b_plan_2026-05-19.md) — 历史
- v2.0 [`phase4_a_b_plan_v2_2026-05-19.md`](phase4_a_b_plan_v2_2026-05-19.md) — 历史
- v3.0 [`phase4_a_b_plan_v3_2026-05-19.md`](phase4_a_b_plan_v3_2026-05-19.md) — 历史 + 3 轮 v3 review reports
- [`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)
- [`flag_lifecycle.md`](flag_lifecycle.md)
- [`production_canary_sop_2026_05_18.md`](production_canary_sop_2026_05_18.md)

---

*v4.0 是 post-3-round-v3-review ship-candidate。下一步:开 Sprint 0(2026-05-20 ~ 05-21,1.75 人日)→ Sprint 1(2026-05-22 ~ 06-04 R12 critical)→ Sprint 末 R12 decision 7/15 ± 5d。整 Phase 4 ship 预期 2026-07-12。*
