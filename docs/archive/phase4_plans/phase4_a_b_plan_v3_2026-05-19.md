# Phase 4 A+B 落地方案 v3.0 — 无历史包袱重写

> **版本**:v3.0(post-philosophy-pivot,**ship-candidate**)
> **日期**:2026-05-19
> **取代**:[`phase4_a_b_plan_v2_2026-05-19.md`](phase4_a_b_plan_v2_2026-05-19.md)(v2.0 防御性 23 人日)
> **承前 review**:[`phase4_a_b_plan_2026-05-19.md`](phase4_a_b_plan_2026-05-19.md)(v1.0)+ 3 轮 review
> **scope**:12 PR(11 + G9 spike)/ ~32 人日 / 4 sprint
> **设计哲学(user 指令)**:**不要历史包袱,所有功能只要收益高,哪怕推翻重做也允许**
> **5 项 v2.0 → v3.0 推翻**:
> 1. **R12 升回 Sprint 1 critical path**(v2.0 是 Sprint 2 task-level opt-in)— 工业 8 家共识收益 > 6 LIVE 机制沉没成本;R12 ON 时 R1b mutate / G5 crossover / G8 forest / R8 L0 / G3 / R9 cache 6 机制自动 OFF(sentinel guard);承担 30d obs PASS rate 可能跌的风险
> 2. **R10-v2 进 Sprint 2 同 R10 互验**(v2.0 是 Sprint 3 末等 R10 ≥14d obs)— R10 soft cap + R10-v2 hard ban 各自 stamp metrics,7d 后看 PASS rate / 误伤率胜出者保留;不必序列等
> 3. **G10 还原完整 4 人日 logic-as-asset loop**(v2.0 砍到 0.5 人日 R8 KB subtype)— AlphaLogics 核心是 logic 作为可优化一等公民;独立 distilled_logic 表 + weekly cron + hypothesis prompt 独立 block
> 4. **G9 spike track 启动**(v2.0 binary 不做)— Sprint 1 末 1 人日评估 BRAIN portfolio API + 自建 portfolio simulator;spike PASS → Phase 5 完整 G9(AlphaCrafter 2026 NeurIPS 路线)
> 5. **B4 G3-v2 直接替换 G3 shadow**(v2.0 等 G3 hard gate)— G3 shadow 1 天 11 pairs 数据样本不足,直接相信 AlphaCFG paper;retire G3 shadow path

---

## 1. 摘要

| 维度 | v1.0 | v2.0 | **v3.0** |
|---|---|---|---|
| PR 总数 | 10 | 11 | **12**(+G9 spike) |
| 人日 | 26 | 23 | **~32** |
| 设计哲学 | 浅吸收 | 防御 / 历史包袱 | **激进采用工业 + 学界 SOTA** |
| 短期 PASS rate 风险 | low | low | **接受短期可能跌**(R12 obs 期) |
| Sprint 数 | 3 | 4 | **4**(密度增) |

---

## 2. 设计原则(v3.0 修正)

承自 v2.0 的 7 原则不变(双文件 flag / 三阶段 / dedicated table / Phase A 真效果 / soft-fail / L1/L2/L3 / cross-flag matrix)。

**新增第 8 原则**:

| # | 原则 | 来源 |
|---|---|---|
| 8 | **无历史包袱决策** — 已 ship 机制不是不可碰的资产;若新机制收益 > 既有机制总和,接受 deprecate / 替换 / 互验。obs 流程为决策服务,不是反过来 | v3.0 user 指令(2026-05-19) |

---

## 3. PR 总依赖图(v3.0)

```
Sprint 0 (前置 / 1 人日)
└─ PR0  LLM_API_CIRCUIT ──── 防 LLM outage silent burn

Sprint 1 (P0 + R12 critical path / 11.8 人日,7d 紧密)
├─ A1  R12 LLM_MODE=assistant **critical path**(6,含 6 机制 sentinel)
│       └─ R12 ON 时自动 OFF: R1b mutate / G5 crossover / G8 forest reuse / R8 L0 / G3 / R9
├─ A2  R14 task_stop_loss + race fix(1.8)
├─ A3  flat-F4 cross-region(2)
├─ A4  AQR Kelly KB seed + baseline rebase(1)
└─ G9-spike portfolio simulator feasibility(1)

Sprint 2 (评估+风控补强 + R10-v2 互验 / 8 人日)
├─ PR-spike R13 BRAIN sim daily PnL(0.5)
├─ B1  R11 alpha_capacity(2)
├─ B2  R13 factor_lens shadow(3.5)
└─ B3  R10-v2 hard family ban **互验 R10**(2)

Sprint 3 (学界 SOTA / 11.5 人日)
├─ B5  R8-v3 cognitive layer + 8k token guard(4.5)
├─ B4  G3-v2 grammar-aware whole-output retry **替换 G3 shadow**(3,retire G3 shadow path)
└─ A5  G10 logic-as-asset 完整 loop(4)
```

**Critical path(v3.0)**:Sprint 0 PR0 → Sprint 1 R12 critical + 6 机制 sentinel guard + G9 spike → Sprint 2 互验 → Sprint 3。

---

## 3bis. Cross-flag interaction matrix(v3.0,关键变更)

v2.0 的 15 行 matrix 中,**R12 的 6 行 🔴/🟡 全部由 v3.0 sentinel guard 解决**:R12 ON 时这 6 个 flag 自动 OFF,DB 写入 audit log,**不允许任何 task 同时 ON R12 + 这 6 个之一**(POST 拒 400)。

v3.0 新增 cross-flag handling:

| 新 PR | 与既有 flag interaction | v3.0 处理 |
|---|---|---|
| **R12 critical** | × R1b mutate / G5 / G8 / R8 L0 / G3 / R9 | **sentinel guard**:R12 ON → 6 flag 自动 OFF 入 audit log;不允许任 task 双 ON |
| **R10-v2 互验** | × `ENABLE_FAMILY_CAP`(R10 软 cap) | **互验模式**:R10 + R10-v2 各自 stamp metrics["_r10_soft_cap_dropped"] / ["_r10v2_hard_banned"];7d obs 后 SQL JOIN 看胜出者(PASS rate × 误伤率)|
| **G10 完整 loop** | × `ENABLE_HIERARCHICAL_RAG`(R8 KB)| **prompt block 隔离**:R8 retrieval 块 + G10 distilled_logic 块独立渲染(`build_distilled_logic_block` 单独段),hypothesis prompt 加 token budget guard(B5 共享)|
| **B4 G3-v2 替换 G3** | × `ENABLE_AST_ORIGINALITY_GATE` | **retire path**:G3 shadow flag 标 deprecated,G3-v2 接管;OFF G3 + ON G3-v2 为新 default,旧 G3 stamp 路径删 |
| **G9 spike** | 无 LIVE flag | spike-only,不上 production code 路径 |

其他 v2.0 标红黄项(A2 R14 race / B5 token / B1 R11 weights)沿用 v2.0 §3bis 处理,不变。

---

## 4. PR 拆分(12 PR)

### 4.0 PR0 — LLM_API_CIRCUIT(1 人日,Sprint 0)

承自 v2.0 §4.0,无变更。default ON,防 DeepSeek/Anthropic outage silent burn。

复用 `backend/circuit_breaker.py` framework + `backend/agents/services/llm_service.py` 入口加 check。

---

### 4.1 A1 — R12 LLM_MODE=assistant **critical path**(6 人日,Sprint 1)

**v2.0 → v3.0 关键差异**:

| | v2.0 | **v3.0** |
|---|---|---|
| 定位 | Sprint 2 task-level opt-in | **Sprint 1 critical path** |
| default | OFF + 仅 task.config opt-in | OFF 起步,但 7d obs 后若收益验证可推 default(规则化升级) |
| 与 6 LIVE 机制 | task.config 互斥(用户选) | **sentinel guard**(R12 ON → 6 flag 自动 OFF + audit log) |
| GO gate | ≥95% PASS rate | **≥90% PASS rate + cost ≤1.2× + sharpe KS test p>0.05**(略松,允许 obs 期数据驱动判断)|

**ENABLE_* flag**:
```python
ENABLE_LLM_ASSISTANT_MODE: bool = False     # critical path Sprint 1 起步 OFF
LLM_ASSISTANT_SENTINEL_FLAGS: list = [      # ON 时强制 OFF 的 6 个 flag
    "ENABLE_R1B_HYPOTHESIS_MUTATE",
    "ENABLE_G5_CROSSOVER",
    "ENABLE_HYPOTHESIS_FOREST_REUSE",
    "ENABLE_HIERARCHICAL_RAG",       # 仅 L0;L1/L2/L3 保留
    "ENABLE_AST_ORIGINALITY_GATE",
    "ENABLE_SIMULATION_CACHE",
]
```

**SUPPORTED_FLAGS 注册**:`ENABLE_LLM_ASSISTANT_MODE`(bool)。

**Alembic**:`j1a2b3c4d5e6_llm_assistant_audit`(down_revision = "i9e4d0a3f7c2")
```sql
CREATE TABLE llm_assistant_sentinel_audit (
  id BIGSERIAL PRIMARY KEY,
  triggered_at TIMESTAMP DEFAULT NOW(),
  task_id INTEGER REFERENCES mining_tasks(id) ON DELETE CASCADE,
  flag_disabled VARCHAR(64) NOT NULL,
  reason VARCHAR(40) NOT NULL DEFAULT 'sentinel_guard',
  meta_data JSONB DEFAULT '{}'
);
CREATE INDEX ix_llm_assistant_audit_task ON llm_assistant_sentinel_audit(task_id);
```

**代码改动**:
| 文件 | 改动 |
|---|---|
| `backend/services/llm_mode_service.py` | **新**:`resolve_mode_and_enforce_sentinel(task, settings) -> (mode, disabled_flags)` |
| `backend/agents/graph/nodes/generation.py:node_code_gen`(~line 1285)| 分支:assistant 模式 LLM 输出 hypothesis_text + reasoning;**fallback synthesize 走 template + RAG seed**(不调用不存在的 `genetic_optimizer.synthesize_from_hypothesis`)|
| `backend/agents/prompts/prompts.yaml` | 新 `code_gen_assistant_mode` prompt |
| `backend/agents/graph/state.py` | `MiningState` 加 `llm_mode_used` + `sentinel_disabled_flags` |
| `backend/routers/tasks.py:create_task` POST | task create 时调 `resolve_mode_and_enforce_sentinel`,任 6 sentinel flag ON 时 task.config["overrides"] 强制 disable + INSERT audit |
| `backend/services/feature_flag_service.py` | `set` ENABLE_LLM_ASSISTANT_MODE=True 时,**写入 SENTINEL_DISABLED audit + 全局 6 flag override OFF**(operator 主动确认) |

**Phase A 真效果**:flag ON 后任新 task 默认 assistant 模式,实际产 PASS alpha(不是 shadow 仅 log)。

**ops endpoint**:`/ops/llm-mode/comparison` — last 30d author baseline vs assistant 的 PASS rate / cost / sharpe / KS test。

**风险承担**(user 指令明示接受):
- 30d obs 期 PASS rate 可能跌 → 这是 data,不是 failure
- 若 GO gate 不达标(<90% PASS),R12 OFF + restore 6 sentinel flag(audit 表查 + ops endpoint 一键 restore)
- 真实收益验证 > AIAC 自家 14 LIVE 机制探索的 6 个之一(R1b / G5 / G8 / R8 L0 / G3 / R9)

**验收**:
- 单测:`resolve_mode_and_enforce_sentinel` 在 6 flag 任一 ON 时正确 INSERT audit + override OFF
- 集成:R12 ON → 新 task 跑 5 round assistant 模式 → ≥3 PASS alpha + sentinel audit DB 有 6 行
- 回归:R12 OFF byte-for-byte = author 行为

---

### 4.2 A2 — R14 task_stop_loss + race fix(1.8 人日,Sprint 1)

承自 v2.0 §4.1,无变更。`skipped_due_to_circuit_breaker` flag fix race;`TASK_STOP_LOSS_PASS_RATE_FLOOR` 由 Sprint 1 前 spike 校准。

---

### 4.3 A3 — flat-F4 cross-region(2 人日,Sprint 1)

承自 v2.0 §4.2,无变更。`FLAT_CROSS_REGION_QUOTA` 进 SUPPORTED_FLAGS(json)。

---

### 4.4 A4 — AQR Kelly KB seed + baseline rebase(1 人日,Sprint 1)

承自 v2.0 §4.3,无变更。1 人日含 paper review + `--save-baseline` step。

---

### 4.5 G9-spike — portfolio simulator feasibility(1 人日,Sprint 1 末)

**v3.0 新增**(v1.0/v2.0 binary 排除 → v3.0 spike track)。

**spike 目标**:
1. **BRAIN portfolio API exists?** — fetch `platform.worldquantbrain.com` 文档 / SDK,查 `/api/v1/portfolio/*` 或 `/api/v1/multi-alpha-combine` 等 endpoint
2. **若无 → 自建 internal portfolio simulator 可行性** — 评估 AIAC 已有 alpha PASS pool 是否可在 BRAIN 外做组合 backtest(用 BRAIN 返回的 IS sharpe / pnl 时序,做 ensemble 加权)
3. **regime-conditioned weighting** — 评估是否能用 P2-C `ENABLE_REGIME` 已有 regime stage(inference/thresholds/style)做组合权重 conditioning

**ops endpoint**:无(spike 不上 production)。

**产出**:`docs/g9_portfolio_spike_report_2026-05-19.md`,内容:
- BRAIN portfolio API 调研结论(yes/no + endpoint URL list)
- 自建 simulator 可行性(yes/no + 估算人日)
- regime conditioning 接入难度
- **Phase 5 G9 GO 推荐**(GO / NO-GO / PARTIAL spike with conditions)

**决策点**:spike PASS → Phase 5 完整 G9 进 plan(4-6 人日 portfolio engine + 3 人日 regime weighting + 2 人日 Screener Agent);spike FAIL(BRAIN 完全不开 + 自建成本过高)→ 真正 binary 排除 G9(此时有数据支撑)。

**承担**:1 人日 spike 失败的 sunk cost,换 G9 决策 data-driven 而非 dogmatic 排除。

---

### 4.6 PR-spike — R13 BRAIN sim daily PnL feasibility(0.5 人日,Sprint 2 day 1)

承自 v2.0 §4.4,无变更。3-tier fallback(OLS / bucket / stamp-only)。

---

### 4.7 B1 — R11 alpha_capacity_estimator(2 人日,Sprint 2)

承自 v2.0 §4.5,无变更。改 `calculate_alpha_score` + `evaluate_alpha_comprehensive` 双路径;Alembic head `k2b3c4d5e6f7`。

---

### 4.8 B2 — R13 factor_lens shadow(3.5 人日,Sprint 2)

承自 v2.0 §4.6,无变更。条件依赖 PR-spike;Alembic head `l3c4d5e6f7g8`。

---

### 4.9 B3 — R10-v2 hard family ban **互验 R10**(2 人日,Sprint 2)

**v2.0 → v3.0 关键差异**:

| | v2.0 | **v3.0** |
|---|---|---|
| Sprint | Sprint 3 末(等 R10 ≥14d obs)| **Sprint 2**(R10 + R10-v2 同时 ON 互验)|
| 决策机制 | R10 obs 后 R10-v2 启动 | 7d 互验 + SQL JOIN 看胜出者保留 |

**互验设计**:
- R10 soft cap 触发 → stamp `metrics["_r10_soft_cap_dropped"]=True` + reason
- R10-v2 hard ban 触发 → stamp `metrics["_r10v2_hard_banned"]=True` + 同 reason
- 都计入 alpha persistence;7d 后跑 SQL:
```sql
WITH stats AS (
  SELECT
    SUM(CASE WHEN metrics->>'_r10_soft_cap_dropped' = 'true' THEN 1 ELSE 0 END) as soft_dropped,
    SUM(CASE WHEN metrics->>'_r10v2_hard_banned' = 'true' THEN 1 ELSE 0 END) as hard_banned,
    SUM(CASE WHEN quality_status = 'PASS' AND
                (metrics->>'_r10_soft_cap_dropped' = 'true' OR
                 metrics->>'_r10v2_hard_banned' = 'true') THEN 1 ELSE 0 END) as could_have_passed
  FROM alphas WHERE created_at > NOW() - INTERVAL '7 days'
)
SELECT * FROM stats;
```
- 胜出标准:`(soft_dropped 误伤率 - hard_banned 误伤率) > 0` OR PASS rate 差 < 5% → R10-v2 替换 R10;否则 R10 保留 R10-v2 retire

**ENABLE_* flag**:同 v2.0 §4.10。

**Alembic**:`m4d5e6f7g8h9_family_bans`(down_revision = "l3c4d5e6f7g8")— 表 schema 同 v2.0。

**风险承担**:互验期(7d)误伤面广 → ops endpoint `/ops/r10-v2/cross-validation` 实时监控 + alert > 30% 误伤率即手动 OFF。

---

### 4.10 B5 — R8-v3 cognitive layer + 8k token guard(4.5 人日,Sprint 3)

承自 v2.0 §4.8,无变更。注:若 R12 critical path ON,R8 L0 已被 sentinel guard 自动 OFF,但 R8 L1/L2/L3 仍 LIVE → B5 cognitive layer 注入 L1 pillar 层级 → R12 + R8-v3 共存。

`COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET = 8000` + drop order 同 v2.0。

---

### 4.11 B4 — G3-v2 grammar-aware **替换 G3 shadow**(3 人日,Sprint 3)

**v2.0 → v3.0 关键差异**:

| | v2.0 | **v3.0** |
|---|---|---|
| 触发 | 等 G3 hard gate LIVE(≥30 calibrate pairs)| **直接替换 G3 shadow** |
| G3 shadow path | 保留 | **retire** — G3-v2 接管 |

**rationale**:G3 shadow 1 天 11 pairs 数据样本不足以触发 hard gate 决策;AlphaCFG paper 已验证 CFG-aware generation 收益;等 calibrate 是流程包袱。

**改动**:
- `ENABLE_AST_ORIGINALITY_GATE` 标 `deprecated_in: phase4_v3`(SUPPORTED_FLAGS group rename 走 [[reference_competitive_analysis_v2_2026_05_19]] retire 模式)
- `backend/agents/graph/nodes/evaluation.py` G3 stamp 路径(metrics["_g3_*"])保留但不再读取;新 G3-v2 stamp 路径接管
- `backend/services/grammar_validator.py` 新 + `backend/data/alpha_dsl_cfg.lark` 新
- LLM whole-output retry(不 streaming token-level):output 后整体 CFG check,失败 1-3 次重试,5 次后 fall-back legacy + 标 metrics["_g3v2_grammar_fallback"]=True

**承担**:G3 shadow 累积的 11 pairs 数据被废弃 → 接受 1 天 sunk cost,换 AlphaCFG 路线直上。

---

### 4.12 A5 — G10 logic-as-asset 完整 loop(4 人日,Sprint 3)

**v2.0 → v3.0 关键差异**:

| | v2.0 | **v3.0** |
|---|---|---|
| 设计 | R8 KB entry_subtype=DISTILLED | **完整独立 loop**:distilled_logic 表 + weekly cron + hypothesis prompt 独立 block |
| 人日 | 0.5 | **4** |
| logic 状态 | 被检索的 entry | **可演化一等公民**(logic library refine loop)|

**rationale**:AlphaLogics 核心是 logic 作为可优化对象,每周 refine logic library。R8 KB subtype 只能复用检索,失去 logic-as-asset 本质 — 不是真正的 AlphaLogics 路线吸收。

**ENABLE_* flag**:
```python
ENABLE_LOGIC_DISTILL: bool = False
LOGIC_DISTILL_CADENCE_HOURS: int = 168       # weekly Sunday 03:00 SH
LOGIC_DISTILL_MIN_PASS_COUNT: int = 10
LOGIC_DISTILL_TOP_K_LOGIC: int = 5
LOGIC_DISTILL_MODEL: str = "claude-haiku-4-5-20251001"
LOGIC_DISTILL_MAX_COST_USD_PER_WEEK: float = 5.00   # Round 3 风险 mitigation
```

**SUPPORTED_FLAGS 注册**:全部 6 个(bool / int / float / str)+ `LOGIC_DISTILL_CADENCE_HOURS` operator 可调。

**Alembic**:`n5e6f7g8h9i0_distilled_logic`(down_revision = "m4d5e6f7g8h9")
```sql
CREATE TABLE distilled_logic (
  id BIGSERIAL PRIMARY KEY,
  distilled_at TIMESTAMP DEFAULT NOW(),
  source_alpha_ids INTEGER[] NOT NULL,
  logic_text TEXT NOT NULL,
  pillar VARCHAR(40),
  region VARCHAR(8),
  confidence FLOAT,
  used_in_prompt_count INT DEFAULT 0,
  refined_from_id BIGINT REFERENCES distilled_logic(id),   -- logic 演化链
  retired_at TIMESTAMP,                                      -- logic library refine: 替换旧条目
  meta_data JSONB DEFAULT '{}'
);
CREATE INDEX ix_distilled_logic_pillar_region ON distilled_logic(pillar, region) WHERE retired_at IS NULL;
CREATE INDEX ix_distilled_logic_refined_chain ON distilled_logic(refined_from_id);
```

**代码改动**:
| 文件 | 改动 |
|---|---|
| `backend/services/logic_distill_service.py` | **新**:`distill_last_week_pass_alphas()` + `refine_logic_library()` — 每周看 distilled_logic 现状,若新 logic 与旧条目 >0.8 similarity → 标旧条目 retired_at + INSERT refined_from_id 链接 |
| `backend/celery_app.py:celery_beat_schedule` | 加 `weekly_logic_distill` Sunday 03:00 SH(**修正 v1.0 路径错**)|
| `backend/agents/prompts/builder.py:build_distilled_logic_block` | **新独立段**(不复用 R8 SUCCESS_PATTERN 路径,因为 logic 是 abstract 不是 expression-bound)|
| `backend/agents/prompts/prompts.yaml` | hypothesis prompt 加 `distilled_logic_block`(token budget 共享 B5 8k guard)|

**LLM cost guard**:weekly cron 内 budget guard:cumulative LLM cost > `LOGIC_DISTILL_MAX_COST_USD_PER_WEEK` 时 abort 本周 distill(剩余 PASS alpha 推下周)。

**ops endpoint**:`/ops/g10/logic-library` — current active logic + 每条 used_in_prompt_count + refine chain depth + 每周 LLM cost。

**baseline rebase**:每周 cron 后 KB 不变(distilled_logic 独立表),但 prompt 注入 → 不必 baseline rebase 直到 Phase 4 末统一。

---

## 5. Sprint 拆分(v3.0)

### Sprint 0 — LLM circuit breaker(2026-05-20 半天 / 1 人日)
| PR | 人日 |
|---|---|
| PR0 LLM_API_CIRCUIT | 1 |

### Sprint 1 — P0 + R12 critical path(2026-05-20 ~ 05-26 / 11.8 人日,7d 紧密)
| PR | 人日 |
|---|---|
| R14 阈值 production spike | 0.5 |
| A1 R12 LLM_MODE=assistant critical + 6 机制 sentinel guard | 6 |
| A2 R14 task_stop_loss + race fix | 1.8 |
| A3 flat-F4 cross-region | 2 |
| A4 AQR Kelly KB seed + baseline rebase | 1 |
| G9-spike portfolio feasibility | 1 |
| buffer | -0.5(密) |

**GO 标准**:全 6 PR ship + R12 sentinel guard 单测全 PASS + G9-spike report committed。**ship 后立刻进 obs 期 R12 监控 (7d)**。

### Sprint 2 — 评估+风控补强 + R10-v2 互验(2026-05-27 ~ 06-04 / 8 人日)
| PR | 人日 |
|---|---|
| PR-spike R13 daily PnL | 0.5 |
| B1 R11 capacity | 2 |
| B2 R13 factor_lens shadow | 3.5 |
| B3 R10-v2 hard family ban 互验 R10 | 2 |

**GO 标准**:全 4 PR ship + R10/R10-v2 互验 stamp 双路径都正常 INSERT。**ship 后 7d 互验 obs 决策 R10 vs R10-v2 胜出者**。

### Sprint 3 — 学界 SOTA(2026-06-05 ~ 06-19 / 11.5 人日)
| PR | 人日 |
|---|---|
| B5 R8-v3 cognitive layer + 8k token guard | 4.5 |
| B4 G3-v2 替换 G3 shadow + retire path | 3 |
| A5 G10 logic-as-asset 完整 loop | 4 |

**GO 标准**:全 3 PR ship + G3 shadow retire path 完成 + G10 weekly cron LIVE。**Sprint 3 末进入 R12 30d obs 末点评估 GO gate**(≥90% PASS rate / cost ≤1.2× / sharpe KS test p>0.05)。

### Sprint 末 — R12 decision point(2026-06-19 ± 5d)

R12 30d obs 完整:
- 若 GO gate PASS → R12 default ON + 6 sentinel flag 永久 deprecate path(v4.0 plan)
- 若 GO gate FAIL → R12 OFF + restore 6 sentinel flag(audit 表一键 restore)
- 若 PARTIAL → 部分 sentinel flag restore(如 R8 L0 / R9 cache 是性能优化无 PASS rate 损失,可保留)

---

## 6. 风险 / 反例(v3.0)

### 6.1 接受的风险(user 指令明示)

| # | 风险 | v3.0 接受度 |
|---|---|---|
| 1 | R12 critical path 后 30d obs PASS rate 可能跌 | **接受** — 数据驱动 decision point;真不行 OFF restore |
| 2 | R10 + R10-v2 互验期误伤面广 | **接受** — ops endpoint 实时监控 + 30% 阈值手动 OFF |
| 3 | G10 独立表与 R8 KB 双源 prompt 注入冲突 | **接受** — token budget guard + 独立 prompt block 段 |
| 4 | G9 spike 1 人日 sunk cost(可能 spike FAIL)| **接受** — 换 G9 决策 data-driven |
| 5 | B4 retire G3 shadow,11 pairs 数据废弃 | **接受** — 信 AlphaCFG paper > 等 calibrate 流程 |
| 6 | A5 G10 weekly cron LLM cost 突发 | mitigated:`LOGIC_DISTILL_MAX_COST_USD_PER_WEEK=5.00` + LLM_API_CIRCUIT |

### 6.2 不再排除(v2.0 历史包袱解除)

- ~~G9 portfolio + execution~~ → v3.0 spike track + Phase 5 GO/NO-GO 决策
- ~~R12 兼容 6 LIVE 机制~~ → v3.0 sentinel guard,无需兼容
- ~~R10-v2 等 R10 obs~~ → v3.0 互验,7d 数据决策

### 6.3 仍排除(真正反例)

- TLRS PPO 全栈(BRAIN sim 限额下 RL 不划算)
- Jane Street 毫秒延迟(BRAIN 日级)
- D.E. Shaw 单点 ML czar
- Point72 Turion theme fund(投资 AI 公司 ≠ 用 AI 找 alpha)

---

## 7. 验收 / 退役标准

### 7.1 Phase 4 整体 ship 完成标准

| L | 标准 |
|---|---|
| L1 代码 | 12 PR 全 master,unit + integration + cross-flag test 全 PASS,baseline 显式 rebase |
| L2 flag | 9 主 flag(8 + sentinel audit 配套)+ ~12 sub-config 全双文件注册 |
| L3 operational | 8 ops endpoint LIVE + 前端 Monitor 页 + flag_lifecycle.md 更新 + canary SOP §1 inventory 表更新 + R12 decision point 文档 |

### 7.2 R12 decision point(Sprint 3 末)

R12 30d obs 后:
| 结果 | Action |
|---|---|
| GO gate PASS | R12 default ON + 6 sentinel flag deprecate(v4.0 plan)|
| GO gate FAIL | R12 OFF + restore 6 flag + 写 lesson learned memory |
| PARTIAL | R8 L0 / R9 cache restore(性能);R1b mutate / G5 / G8 / G3 保留 deprecate |

### 7.3 Phase 5 触发条件

Phase 4 ship + ≥30d obs + 满足任一:
- AIAC BRAIN(consultant tier)排名进入 top 100
- G9 spike PASS → Phase 5 完整 G9
- 出现新 SOTA paper 与 AIAC 现有机制 ≥3 项 conflict

---

## 8. v1.0 → v2.0 → v3.0 changelog

### v2.0 → v3.0(philosophy pivot)

| # | v2.0 决策(防御) | v3.0 推翻(收益高优先) |
|---|---|---|
| 1 | R12 Sprint 2 task opt-in,严 GO gate ≥95%,与 6 机制 task-level 互斥 | **Sprint 1 critical path,sentinel guard 全局 6 flag 自动 OFF,GO gate ≥90%** |
| 2 | R10-v2 推迟 Sprint 3 末,等 R10 ≥14d obs | **Sprint 2 同 R10 互验,7d 决策胜出者** |
| 3 | G10 砍 0.5 人日 R8 KB subtype | **完整 4 人日 logic-as-asset loop + refine chain** |
| 4 | G9 binary 不做 | **G9 spike 1 人日 Sprint 1 末决策点** |
| 5 | B4 G3-v2 等 G3 hard gate(≥30 calibrate pairs)| **Sprint 3 直接替换 G3 shadow,retire path** |

### v1.0 → v2.0(post-3-round-review)

承自 v2.0 §8 的 19 项 review fix,均保留(Alembic 12-char hex / 5 路径 fix / sub-config 注册 / baseline rebase / token budget / race fix 等)。

---

## 9. 决策权衡明确化

### 9.1 v3.0 哲学权衡

**用户指令(2026-05-19)**:"不要历史包袱,所有功能只要收益高,哪怕推翻重做也是允许的。"

| trade-off | v3.0 选择 |
|---|---|
| 已 ship 机制保护 vs 工业 8 家共识 | **后者** — 6 机制 sentinel deprecate |
| 流程 obs 等待 vs 互验决策 | **后者** — R10/R10-v2 互验 |
| 功能 dedup 避免 vs 完整 paper 路线 | **后者** — G10 完整 loop |
| binary 定位排除 vs spike 决策点 | **后者** — G9 spike |
| 数据样本累积 vs paper 直信 | **后者** — B4 替换 G3 shadow |

### 9.2 v3.0 决策保留 review 验证

- **3 轮 fresh review 仍可推**:v3.0 ship-candidate 但比 v2.0 激进,可选再走 1 轮 review 验证 sentinel guard / 互验 schema / G9 spike 范围。**或直接开 Sprint 0**(用户偏好)。

---

## 10. 关联文档

- [`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)
- [`phase4_a_b_plan_2026-05-19.md`](phase4_a_b_plan_2026-05-19.md)(v1.0,历史)
- [`phase4_a_b_plan_v2_2026-05-19.md`](phase4_a_b_plan_v2_2026-05-19.md)(v2.0,被 v3.0 替代)
- [`flag_lifecycle.md`](flag_lifecycle.md)
- [`production_canary_sop_2026_05_18.md`](production_canary_sop_2026_05_18.md)

---

*v3.0 是 post-philosophy-pivot ship-candidate。下一步:operator 决策接受 → 开 Sprint 0(PR0 LLM_CB 1 人日)+ Sprint 1 紧密 11.8 人日。整 Phase 4 ship 预期完成 2026-06-19 ± 5d。R12 decision point 是 Phase 4 末关键节点,GO/NO-GO/PARTIAL 三路径全有 audit + restore 机制。*
