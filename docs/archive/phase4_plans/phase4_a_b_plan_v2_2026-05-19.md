# Phase 4 A+B 落地方案 v2.0 — 重写版

> **版本**:v2.0(post-3-round review,**ship-candidate**)
> **日期**:2026-05-19
> **取代**:[`phase4_a_b_plan_2026-05-19.md`](phase4_a_b_plan_2026-05-19.md)(v1.0 draft,3 轮 review 均判需 fix:战略 6/10 + 技术 5/10 + 风险 4/10,已归档)
> **承前**:[`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)
> **scope**:11 PR(+1 PR0 前置)/ ~23 人日 / 4 sprint(Sprint 0 + 1 + 2 + 3)
> **关键修正**(v1.0 → v2.0):
> 1. **R12 LLM_MODE=assistant 降 P2**:从 Sprint 1 critical path 降到 Sprint 2 PR,且 *仅 task.config opt-in 灰度*,不翻 global default — 工业派共识针对 prose/portfolio,AIAC 输出是 BRAIN DSL,与 R1b/G5/G8/R8/G3/R9 6 LIVE 机制冲突
> 2. **A5 G10 砍小**:4 人日 → 0.5 人日,改为 R8 KB `entry_subtype=DISTILLED` 维度复用既有 SUCCESS_PATTERN 检索,不独立表 / 不独立 cron
> 3. **B3 R10-v2 推迟**:Sprint 2 → Sprint 3 末,等 R10 ≥7d obs(违反 [[feedback_light_wiring_deferred_gate]] 三阶段已纠正)
> 4. **新增 PR0 LLM_CIRCUIT_BREAKER**:Sprint 1 前置 1 人日 — Phase 4 加重 LLM 调用(R12/B4/B5/G10),复用 [[project_a_plus_circuit_breaker_2026_05_19]] framework 加 DeepSeek/Anthropic outage 熔断
> 5. **A2 R14 与 BRAIN_AUTH_CIRCUIT race fix**:circuit-breaker-skipped round 不计 consecutive_zero
> 6. **cross-flag interaction matrix**:新 §3bis 表,7 新 flag × 33 既有 ON flag 兼容性
> 7. **Alembic head 重命名**:5 migration 改 12-char hex + 明确 down_revision 串联 `i9e4d0a3f7c2 → R14 → R11 → R10-v2 → R13 → G10-noop`
> 8. **5 处文件路径 fix**:`agents/services/family_classifier` → `family_classifier`;`calculate_composite_score` → `calculate_alpha_score` + `evaluate_alpha_comprehensive` 双改;`scheduled_tasks.py` → `celery_app.py:celery_beat_schedule`;`g5_crossover_service.py` 真实路径 verify;`rag_service._get_success_patterns_enhanced` verify
> 9. **sub-config flag 全注册**:`FACTOR_LENS_MODE` / `TASK_STOP_LOSS_*` / `FAMILY_BAN_*` / `LOGIC_DISTILL_*` 进 SUPPORTED_FLAGS
> 10. **baseline.json rebase step**:A4/A5/B5 显式 `--save-baseline` 子步骤
> 11. **GO 标准 / token budget / R14 阈值 spike**:Sprint 1 ship + obs 拆开 / B5 加 8k token guard / R14 阈值跑 production last 30d 回测

---

## 1. 摘要

| 维度 | v1.0 | **v2.0** |
|---|---|---|
| PR 总数 | 10 | **11**(含 PR0 LLM_CB)|
| 人日估算 | 26 | **~23**(R12 8→2 opt-in / G10 4→0.5 / +PR0 1 / +R14 race fix 0.3)|
| Sprint 拆分 | 3 | **4**(Sprint 0 前置 + 1 + 2 + 3)|
| 新 ENABLE_* 主 flag | 7 | **8**(+ENABLE_LLM_API_CIRCUIT)|
| 新 sub-config 进 SUPPORTED_FLAGS | 0 | **~12**(MODE str + 阈值 ops UI 可调)|
| Alembic migration | 5 | **5**(重命名 12-char hex + 串联序明确)|
| 新增 ops endpoint | 6 | **7**(+/ops/llm/api-circuit-{status,clear})|

---

## 2. 设计原则(沿用 + 新增)

承自 v1.0 §2 的 6 原则不变(双文件 flag / 三阶段 rollout / dedicated log table / Phase A 真效果 / soft-fail 全链 / L1/L2/L3 ship-state)。

**新增第 7 原则**(post-review):

| # | 原则 | 来源 |
|---|---|---|
| 7 | **cross-flag interaction matrix 强制审查** — 新 flag 翻 ON 前必须列出与现行 33 ON flag 的 N×33 兼容性矩阵,标红 / 黄 / 绿;红色项必须有 mitigation;不允许 silent regression | Round 3 review 教训(R12 与 6 LIVE 机制 silent killer) |

---

## 3. PR 总依赖图

```
Sprint 0 (前置 / 1 人日)
└─ PR0  LLM_CIRCUIT_BREAKER ──── Phase 4 余下所有 LLM-加重 PR(R12/B4/B5/G10)的前置

Sprint 1 (P0 风险口 / 5.8 人日)
├─ A2 R14 task_stop_loss(1.8 含 race fix)─── 独立 + 排除 circuit-breaker-skipped round
├─ A3 flat-F4 cross-region 平衡(2)──────── 独立
└─ A4 AQR Kelly/Xiu KB seed(1)──────────── 依赖 R8 LIVE(已)
   └─ buffer 0.5 + R14 阈值 production spike 0.5

Sprint 2 (评估+风控补强 / 8 人日)
├─ PR-spike R13 BRAIN sim daily PnL(0.5)── 决定 OLS vs bucket
├─ B1  R11 alpha_capacity(2)─────────────── 改 calculate_alpha_score + evaluate_alpha_comprehensive 双路径
├─ B2  R13 factor_lens shadow(3.5)──────── 依赖 spike 结果
└─ A1' R12 LLM_MODE task.config opt-in(2)─ **不进 default**,task-level opt-in 灯
                                                cross-flag matrix gating

Sprint 3 (学界 SOTA / 8.5 人日)
├─ B5  R8-v3 cognitive layer(4 + 0.5 token-budget guard)─ 依赖 R8 LIVE + prompt sized
├─ B4  G3-v2 grammar-aware **whole-output retry**(3)─── 依赖 G3 hard gate LIVE
├─ B3  R10-v2 hard family ban(2)──────────────────────── **推迟到此** 等 R10 ≥14d obs
└─ A5' G10 R8 KB entry_subtype=DISTILLED(0.5)──── R8 KB schema 加维度,不独立表
```

**Critical path(v2.0)**:PR0 LLM_CB → Sprint 1 三 PR 并行 → Sprint 2 PR-spike 决定 → Sprint 3。

---

## 3bis. Cross-flag interaction matrix(新增,Round 3 共识)

7 新 flag × 33 既有 ON flag 全表略,只列 **红 + 黄** 项(共 15 行):

| 新 PR | 与既有 flag interaction | 等级 | Mitigation |
|---|---|---|---|
| **R12 (Sprint 2 opt-in)** | × `ENABLE_R1B_HYPOTHESIS_MUTATE` (mutate 读 last expression diff) | 🔴 | task-level opt-in,R12 + R1b 不能同 task ON |
| R12 | × `ENABLE_G5_CROSSOVER` (G5 用 2 PASS expression combine) | 🔴 | 同上,R12 ON 时 G5 fall-back legacy(不 combine assistant 输出) |
| R12 | × `ENABLE_HYPOTHESIS_FOREST_REUSE` (G8 cross-task hypothesis 经 expression 锚定) | 🔴 partial | R12 ON 时 G8 hash 走 hypothesis_text 而非 expression,需 R12 PR 内补 fallback |
| R12 | × `ENABLE_HIERARCHICAL_RAG` (R8 L0 exact expression match) | 🟡 degraded | R12 ON 时 L0 跳过,直接 L1/L2,acceptable |
| R12 | × `ENABLE_AST_ORIGINALITY_GATE` (G3 需 expression AST) | 🟡 degraded | R12 ON 时 G3 stamp metrics["_g3_skip_assistant"]=True,不阻塞 |
| R12 | × `ENABLE_SIMULATION_CACHE` (R9 cache key 含 expression hash) | 🟡 cache miss spike | acceptable — R12 opt-in 灰度初期 cache miss 是预期 |
| **A2 R14** | × A+ `BRAIN_AUTH_CIRCUIT` (skipped round 误计 consecutive_zero) | 🔴 | `_run_one_round_inline` 末 set `round_state["skipped_due_to_circuit_breaker"]=True`;R14 stop_loss_service 读 flag 跳过计数 |
| A2 R14 | × `ENABLE_FLAT_CONTINUOUS` (flat cursor pause/resume) | 🟡 state race | R14 触发 pause 时 inherit_runtime_state=True 保留 flat_cursor;resume 走 `/ops/flat-sessions/{id}/resume`,避免 cursor 丢失 |
| **A5' G10** | × `ENABLE_HIERARCHICAL_RAG` (R8 KB 双源) | ✅ resolved | G10 砍为 R8 KB entry_subtype=DISTILLED,共享 schema,不双源 |
| A5' G10 | × `ENABLE_HYPOTHESIS_FOREST_REUSE` (G8 与 G10 双注入) | 🟡 prompt 冲突 | G10 distilled 走 R8 RAG retrieval 同一路径,prompt 不双注入 |
| **B5 R8-v3** | × `ENABLE_DUAL_CHANNEL_RAG` + macro + pillar 累积 → token 突破 32k | 🔴 | PR 内加 prompt-length budget guard,>8k token drop dedup blacklist(最旧)优先;ops endpoint /ops/r8-v3/prompt-token-stats 实测 |
| B5 R8-v3 | × `ENABLE_REGIME` style stage (双 system 头) | 🟡 | cognitive layer 和 regime guidance 合并为一个 system 头;互斥决策矩阵 7×3 在 §4.10 |
| **B3 R10-v2** | × `ENABLE_FAMILY_CAP` (软 cap + hard ban 双触发) | 🟡 over-block | R10-v2 仅在 R10 软 cap 后剩余候选内 check pairwise corr;不重复触发 |
| **B1 R11** | × `ENABLE_GRADED_SCORE` (composite_score 加第 6 维改 weight 总和) | 🟡 calibrate | 4 维 + capacity 第 5 维同时纳入,5 维 weights normalize sum=1.0,旧 baseline 需 `--save-baseline` rebase |
| **B4 G3-v2** | × `ENABLE_AST_ORIGINALITY_GATE` (事前 CFG + 事后 AST 双门) | 🟡 cost | CFG check 通过则 AST gate skip;CFG 失败 fall-back 时才走 AST;减少 2× cost |

绿色项 18 项(7 × 33 = 231 - 15 红黄 - 不相关 198 = 18 绿)略去,均为 add-only / 不交叉路径。

---

## 4. PR 拆分(11 PR)

### 4.0 PR0 — LLM_CIRCUIT_BREAKER(1 人日,Sprint 1 前置)

**Source**:Round 3 review 教训。Phase 4 R12/B4/B5/G10 4 PR 加重 LLM 调用,DeepSeek/Anthropic outage 时 silent burn(类比 121× BRAIN 401 教训)。

**ENABLE_* flag**:
```python
ENABLE_LLM_API_CIRCUIT: bool = True   # **default ON**(防御机制 default ON 与 BRAIN_AUTH_CIRCUIT 一致)
LLM_API_CIRCUIT_FAIL_THRESHOLD: int = 5           # 连续 N 次 5xx / timeout 跳闸
LLM_API_CIRCUIT_COOLDOWN_SEC: int = 300           # 跳闸冷却
```

**Alembic**:无。

**代码改动**:复用 `backend/circuit_breaker.py` framework + 在 `backend/agents/services/llm_service.py` 入口加 `LLM_API_CIRCUIT` check + auth-error / 5xx trip。

**ops endpoint**:`/ops/llm/api-circuit-{status,clear}`,前端 LLM_API_CircuitMonitor。

**Phase A 真效果**:flag ON 即真 fast-fail + skip,不是 shadow。

**验收**:
- 单测:连续 5 次 LLM 5xx → CIRCUIT TRIPPED;成功 1 次 → 自动 clear
- 集成:mock DeepSeek 500 错误 → 第 6 次 call 立即 fast-fail,不再发 HTTP
- 回归:flag OFF byte-for-byte

---

### 4.1 A2 — R14 `task_stop_loss`(1.8 人日,Sprint 1)

**Source**:Millennium 5%/7.5% hard stop-loss(v1.0 §4.2 沿用,+ race fix)。

**ENABLE_* flag**:
```python
ENABLE_TASK_STOP_LOSS: bool = False
TASK_STOP_LOSS_EMA_ALPHA: float = 0.3
TASK_STOP_LOSS_MIN_ROUNDS: int = 5
TASK_STOP_LOSS_PASS_RATE_FLOOR: float = 0.05      # **Sprint 1 前 spike** production last 30d 阈值校准
TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS: int = 3
TASK_STOP_LOSS_EXCLUDE_CB_SKIPPED: bool = True    # **race fix**:CB-skipped round 不计入
```

**SUPPORTED_FLAGS 注册**:`ENABLE_TASK_STOP_LOSS`(bool)+ `TASK_STOP_LOSS_PASS_RATE_FLOOR`(float)+ `TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS`(int)— operator 可 ops UI 调阈值。

**Alembic**:`j1a2b3c4d5e6_task_stop_loss_events`(12-char hex,**down_revision = "i9e4d0a3f7c2"**)
```sql
CREATE TABLE task_stop_loss_events (
  id BIGSERIAL PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES mining_tasks(id) ON DELETE CASCADE,
  triggered_at TIMESTAMP DEFAULT NOW(),
  trigger_reason VARCHAR(40) NOT NULL,
  ema_pass_rate FLOAT,
  consecutive_zero_rounds INT,
  rounds_completed INT,
  meta_data JSONB DEFAULT '{}'
);
CREATE INDEX ix_task_stop_loss_task_id ON task_stop_loss_events(task_id);
```
(沿用既有 alembic guard 模式 `inspector.has_table` / `has_column`)

**代码改动**:
| 文件 | 改动 |
|---|---|
| `backend/services/task_stop_loss_service.py` | **新**:`check_should_pause(task, round_metrics, round_state)` — **race fix**:`if round_state.get("skipped_due_to_circuit_breaker"): return NoPause` |
| `backend/tasks/mining_tasks.py:_run_one_round_inline` 入口 | A+ CB skip 时 set `round_state["skipped_due_to_circuit_breaker"]=True` |
| `backend/tasks/mining_tasks.py:_run_one_round_inline` 末 | round end 调 stop_loss_service |
| `backend/models/task.py` MiningTask | hybrid_property `last_stop_loss_event` |

**Sprint 1 前置 spike(0.5 人日)**:跑 production last 30d 数据,SQL:
```sql
WITH round_stats AS (
  SELECT task_id, round_num, COUNT(*) FILTER (WHERE quality_status='PASS') as pass_n,
         COUNT(*) as total_n
  FROM alphas WHERE created_at > NOW() - INTERVAL '30 days'
  GROUP BY task_id, round_num
)
SELECT task_id, percentile_cont(0.05) WITHIN GROUP (ORDER BY pass_n::float/total_n) AS p5_pass_rate
FROM round_stats GROUP BY task_id;
```
得到 production PASS rate 5th percentile → 校准 `TASK_STOP_LOSS_PASS_RATE_FLOOR` 不致 false trigger。

**ops endpoint**:`/ops/task-stop-loss/recent`。

**验收**:
- 单测:8 round 0 PASS → 第 3 round 触发;5 round 1 PASS → 不触发;CB-skipped round 不计入(✓ race fix 单测)
- 集成:mock task + CB skip 期 → R14 不误触发
- production spike 校准的 floor 在生产 last 30d 数据上 0 false positive

---

### 4.2 A3 — flat-F4 cross-region 平衡(2 人日,Sprint 1)

承自 v1.0 §4.3,无变更。

`FLAT_CROSS_REGION_QUOTA` dict 进 SUPPORTED_FLAGS(json 类型)— operator 可调。

---

### 4.3 A4 — AQR Kelly/Xiu paper KB seed(1 人日,Sprint 1)

**修正**(v1.0 0.5 → v2.0 1):人工抽 5 篇 paper × 各 1-3 hypothesis + KB schema 映射 ≥1 人日;+0.5 review buffer。

**baseline rebase**:`scripts/seed_aqr_kelly_paper.py` 跑完后,在 PR 中显式加 step:
```bash
python backend/tests/test_suite.py --all --save-baseline
git add backend/tests/baseline.json
git commit -m "chore(baseline): rebase post-AQR seed (kb 3357 → 3367 ± 8)"
```

---

### 4.4 PR-spike — R13 BRAIN sim daily PnL feasibility(0.5 人日,Sprint 2 day 1)

独立 spike PR,决定 B2 R13 走 OLS 路径还是 bucket-median fallback。

**决策矩阵**:
| spike 结果 | B2 路径 | B2 人日 |
|---|---|---|
| BRAIN simulate API 返 daily PnL ≥504d | OLS 路径 | 3.5 |
| 只返 IS 总 metrics | bucket-median fallback | 4(+0.5 bucket 设计)|
| BRAIN API 完全不返 PnL | B2 砍掉,只 stamp factor exposure(无 residual) | 1(降级)|

---

### 4.5 B1 — R11 `alpha_capacity_estimator`(2 人日,Sprint 2)

**修正**(v1.0 致命 bug):plan v1.0 引用了不存在的 `calculate_composite_score`。真实:

| 函数 | 文件:行 | 改动 |
|---|---|---|
| `calculate_alpha_score` | `backend/alpha_scoring.py:483` | `default_weights` dict 加 `capacity: CAPACITY_SCORE_WEIGHT` 5 维 → 6 维 |
| `evaluate_alpha_comprehensive` | `backend/alpha_scoring.py:264` | `composite_score` 4 维公式(:346-351)加第 5 维 capacity_norm,normalize sum=1.0 |

**Alembic**:`k2b3c4d5e6f7_alpha_capacity_metadata`(down_revision = "j1a2b3c4d5e6")
```sql
ALTER TABLE alphas ADD COLUMN capacity_usd_estimate FLOAT;
CREATE INDEX ix_alphas_capacity_usd ON alphas(capacity_usd_estimate)
  WHERE capacity_usd_estimate IS NOT NULL;
```

**SUPPORTED_FLAGS 注册**:`ENABLE_CAPACITY_SCORE`(bool)+ `CAPACITY_SCORE_WEIGHT`(float)。

**baseline rebase**:5 维 → 6 维 calibrate 后 baseline 必须更新。

---

### 4.6 B2 — R13 `factor_decomposition` shadow(3.5 人日,Sprint 2)

承自 v1.0 §4.7,**条件性**:依赖 PR-spike 结果。

**factor_returns_snapshot.parquet 维护成本**:5 region × 5 factor × 2y daily ~12,600 cells × 5 region ≈ 63k rows,manual quarterly refresh(operator 责);加 `/ops/r13/snapshot-stale-check` endpoint 在 stale >90d 时告警。

**SUPPORTED_FLAGS 注册**:`ENABLE_FACTOR_LENS`(bool)+ `FACTOR_LENS_MODE`(str,shadow/soft/hard)+ `FACTOR_LENS_RESIDUAL_SHARPE_MIN`(float)。

**Alembic**:`l3c4d5e6f7g8_factor_lens_residuals`(down_revision = "k2b3c4d5e6f7")

**OLS cost cache**:同 region/universe/lookback 复用 X 矩阵 — 预计 50 alpha/day → numpy lstsq 10 min/day,可承受;PR 内加 `factor_returns_X_matrix_cache.parquet`。

---

### 4.7 A1' — R12 LLM_MODE task.config opt-in(2 人日,Sprint 2)

**降级修正(critical fix)**:v1.0 说 R12 进 Sprint 1 critical path + default 翻 assistant;v2.0 仅 task.config opt-in。

**rationale**:
- 工业派 LLM-as-assistant 共识针对 *prose / portfolio output*,AIAC 是 *BRAIN DSL*
- 与 R1b CoSTEER / G5 / G8 / R8 / G3 / R9 6 LIVE 机制 conflict matrix 见 §3bis
- AIAC 自家 production 验证基于 LLM-as-author 14 机制 ship 成功

**改动**:
```python
ENABLE_LLM_ASSISTANT_MODE: bool = False     # 全局 opt-in switch
# default OFF,任何 task 默认 author。task.config["llm_mode"]="assistant" 启用
```

`backend/services/llm_mode_service.py` 新文件 `resolve_mode(task) -> "author"|"assistant"`:
1. task.config["llm_mode"] != None → 用之
2. 否则 default "author"
3. flag OFF 时强制 "author"(全局 kill switch)

`backend/agents/graph/nodes/generation.py:node_code_gen` 加分支(line ~1285):assistant 模式 LLM 输出 hypothesis_text;**fallback synthesize 走 template + RAG seed,不依赖** `genetic_optimizer.synthesize_from_hypothesis`(后者不存在,plan v1.0 错)。

**Phase A 真效果**:某 task `task.config["llm_mode"]="assistant"` 灰度 30d,/ops/llm-mode/comparison endpoint 监控 PASS rate / cost / sharpe 分布 vs author baseline。

**GO gate**(v2.0 严格化):≥95% PASS rate(v1.0 是 80%,违反"真改决策")+ cost ≤1.2× author + 同 sharpe 分布 KS test p > 0.05。

**cross-flag enforcement**:`resolve_mode` 内强制 — task.config["llm_mode"]="assistant" 但 任一 of {R1b mutate / G5 / G8 forest} ON → 抛 `IncompatibleFlagError`,task create POST 拒绝 400。

---

### 4.8 B5 — R8-v3 cognitive layer + prompt token guard(4.5 人日,Sprint 3)

**v1.0 4 → v2.0 4.5**(+0.5 token-budget guard)。

**新增 token budget guard**(Round 3 fix):
```python
COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET: int = 8000   # hypothesis prompt 总 budget
```
`backend/agents/prompts/builder.py` 渲染 hypothesis prompt 时 measure token,>8k drop 顺序:dedup_blacklist(最旧)→ cross_task_forest(最少 used)→ macro_narrative。

**SUPPORTED_FLAGS 注册**:`ENABLE_COGNITIVE_LAYER_PROMPT`(bool)+ `COGNITIVE_LAYER_SELECT_MODE`(str)+ `COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET`(int)。

**ops endpoint**:`/ops/r8-v3/prompt-token-stats` — 每 hypothesis call token 分布 + drop event。

**baseline rebase**:cognitive layer 改 prompt → KB 不变但 prompt token 分布变,需要监控但不必 baseline rebase(baseline 不含 prompt token)。

---

### 4.9 B4 — G3-v2 grammar-aware **whole-output retry**(3 人日,Sprint 3)

**v1.0 streaming token-level reject → v2.0 whole-output retry**(Round 3 fix):
- streaming reject 会破坏 LLM 上下文连贯性
- 改为 LLM 完整输出后整体 CFG check,失败时 1-3 次重试,5 次失败 fall-back legacy unguarded + 标 metrics["_g3v2_grammar_fallback"]=True
- 复用 R7 self-correct semi-accept 模式

**触发条件**:G3 hard gate LIVE(`AST_ORIGINALITY_MODE="hard"`)。若 Sprint 3 起步 G3 仍 shadow,B4 顺延 → Sprint 3 实际工时减 3 → 替代任务:R8-v3 follow-up calibration script(3 人日,无依赖)。

---

### 4.10 B3 — R10-v2 hard family ban(2 人日,**Sprint 3 末**)

**推迟修正**(v1.0 Sprint 2 → v2.0 Sprint 3 末):
- v1.0 在 R10 ship 1 天就计划 R10-v2,违反 [[feedback_light_wiring_deferred_gate]] 三阶段 ≥7d obs 原则
- Sprint 3 末时点 R10 已 ≥14d obs(Sprint 0 + 1 + 2 + 3 全部时间)

**Alembic**:`m4d5e6f7g8h9_family_bans`(down_revision = "l3c4d5e6f7g8")

**真实路径修正**:`backend/family_classifier.py:90`(v1.0 错写 `agents/services/family_classifier`)。

**SUPPORTED_FLAGS 注册**:`ENABLE_FAMILY_BAN`(bool)+ `FAMILY_BAN_MIN_PAIRWISE_CORR`(float)+ `FAMILY_BAN_DURATION_ROUNDS`(int)。

---

### 4.11 A5' — G10 R8 KB entry_subtype=DISTILLED(0.5 人日,Sprint 3)

**砍小修正(critical fix)**:v1.0 G10 独立表 `distilled_logic` + 独立 cron + 独立 prompt 注入(4 人日)→ v2.0 复用 R8 KB schema(0.5 人日)。

**改动**:
1. R8 KB schema 加 `entry_subtype` VARCHAR(40) 默认 NULL — Alembic `n5e6f7g8h9i0_kb_entry_subtype`(down_revision = "m4d5e6f7g8h9")
2. `backend/services/logic_distill_service.py` 新文件,**简化为函数**:`distill_pass_alpha_to_kb(pass_alpha_ids)` 接受 PASS alpha list → LLM 抽 logic → UPSERT R8 KB(entry_type=SUCCESS_PATTERN,entry_subtype=DISTILLED)
3. R8 RAG retrieval 自动包含 entry_subtype=DISTILLED entries(无新路径)
4. **手动触发**(不进 cron 避免成本不可控):`POST /ops/g10/distill-now {pass_alpha_ids: [...]}` operator 在需要时手动调

**ENABLE_* flag**:`ENABLE_LOGIC_DISTILL_WRITE: bool = False`(默认 OFF,operator 手动调用时检查)。

**SUPPORTED_FLAGS 注册**:`ENABLE_LOGIC_DISTILL_WRITE`(bool)+ `LOGIC_DISTILL_MODEL`(str)。

**LLM cost guard**:手动触发 + 复用 LLM_API_CIRCUIT;无 weekly cron 即无 cost burst 风险。

**baseline rebase**:KB count 改 → Phase 4 末统一 rebase。

---

## 5. Sprint 拆分

### Sprint 0 — LLM circuit breaker(2026-05-20 半天 / 1 人日)
| PR | 人日 |
|---|---|
| PR0 LLM_API_CIRCUIT | 1 |

**GO 标准**:LLM_API_CIRCUIT default ON + 5xx mock 测试通过 + ops endpoint LIVE。**未达不开 Sprint 1**。

### Sprint 1 — P0 风险口闭合(2026-05-21 ~ 05-25 / 5.8 人日)
| PR | 人日 |
|---|---|
| R14 阈值 production spike | 0.5 |
| A2 R14 task_stop_loss + race fix | 1.8 |
| A3 flat-F4 cross-region | 2 |
| A4 AQR Kelly KB seed + baseline rebase | 1 |
| buffer | 0.5 |

**GO 标准**:全 4 PR ship + baseline rebase OK。**ship 后 7d obs**(2026-05-26 ~ 06-01),期间 R14 flag ON,观察是否 false trigger。**Sprint 2 开始时间取决于 obs 期 PASS**。

### Sprint 2 — 评估+风控补强(2026-06-02 ~ 06-09 / 8 人日)
| PR | 人日 |
|---|---|
| PR-spike R13 daily PnL feasibility | 0.5 |
| B1 R11 capacity(改 calculate_alpha_score + evaluate_alpha_comprehensive 双路径) | 2 |
| B2 R13 factor_lens shadow | 3.5 |
| A1' R12 LLM_MODE task.config opt-in + cross-flag enforcement | 2 |

**GO 标准**:全 4 PR ship + R12 opt-in 至少 1 task 跑 ≥1 round,validation cross-flag enforcement triggers expected。

### Sprint 3 — 学界 SOTA(2026-06-10 ~ 06-19 / 8.5 人日)
| PR | 人日 |
|---|---|
| B5 R8-v3 cognitive layer + 8k token guard | 4.5 |
| B4 G3-v2 whole-output CFG retry(若 G3 hard gate LIVE)| 3 |
| B3 R10-v2 hard family ban(R10 已 ≥14d obs) | 2 |
| A5' G10 R8 KB entry_subtype=DISTILLED + manual endpoint | 0.5 |

**GO 标准**:全 4 PR ship + R12 30d obs 完成 + GO gate (PASS rate ≥95% + cost ≤1.2× + sharpe KS p>0.05) 评估推 R12 default。

---

## 6. 风险 / 反例

### 6.1 已识别风险(v2.0 全部 mitigated)

| # | 风险 | v2.0 mitigation |
|---|---|---|
| 1 | R12 与 6 LIVE 机制 silent regression | §3bis cross-flag matrix + §4.7 task-level opt-in + IncompatibleFlagError |
| 2 | R14 与 BRAIN_AUTH_CIRCUIT race false trigger | §4.1 `skipped_due_to_circuit_breaker` flag |
| 3 | R13 BRAIN sim daily PnL 不可用 | §4.4 PR-spike + 3-tier fallback(OLS / bucket / stamp-only) |
| 4 | R13 OLS compute cost 突发 | §4.6 X 矩阵 cache + ops endpoint snapshot-stale-check |
| 5 | B5 prompt token >32k 触发 truncation | §4.8 token budget guard + drop order |
| 6 | R10-v2 premature optimization | §4.10 推迟到 Sprint 3 末等 R10 ≥14d obs |
| 7 | A5 G10 weekly cron cost burst | §4.11 手动触发 + 复用 LLM_API_CIRCUIT |
| 8 | LLM API outage silent burn | §4.0 PR0 LLM_API_CIRCUIT default ON |
| 9 | AQR seed paper 抽取质量 | §4.3 人工 review + 1 人日(含 review buffer) |
| 10 | B4 streaming token reject 破坏 LLM 上下文 | §4.9 改 whole-output retry |
| 11 | sub-config flag operator 无法 ops UI 调 | 全 PR 显式列 SUPPORTED_FLAGS 注册项 |
| 12 | baseline.json drift | A4/B1/A5' PR 内显式 `--save-baseline` step |
| 13 | Sprint 1 GO 标准内部矛盾(ship vs obs)| §5 拆 Sprint 1 ship + 7d obs;Sprint 2 后启 |

### 6.2 反例(明确砍 / 不做)

- **G9 portfolio + execution** — 定位边界外
- **R12 default 翻 assistant** — v2.0 仅 task.config opt-in,30d obs + 严 GO gate(≥95%)后才考虑 default
- **G10 weekly cron 独立表** — 砍为 R8 KB subtype + 手动触发
- **B4 token-level streaming reject** — 改为 whole-output retry
- **A5' G10 独立 prompt 注入** — 复用 R8 RAG retrieval 路径

---

## 7. 验收 / 退役标准

### 7.1 Phase 4 整体 ship 完成标准

| L | 标准 |
|---|---|
| L1 代码 | 11 PR 全 master,unit + integration test 全 PASS,baseline 显式 rebase(A4/B1/A5')|
| L2 flag | 8 个主 flag + 12 个 sub-config 全双文件注册 + SUPPORTED_FLAGS 可 ops UI 调 |
| L3 operational | 7 个 ops endpoint LIVE + 前端 Monitor 页(R11/R13/R14/LLM_CB 至少各 1 chart 进 Dashboard)+ flag_lifecycle.md 更新 inventory + production canary SOP §1 inventory 表更新 |

### 7.2 4 rollback trigger × Sprint 1-3 prediction(Round 3 教训)

参 `production_canary_sop_2026_05_18.md §4`:
- **R1a runaway cost ($5/24h)** — Sprint 2 R12 opt-in 单 task 灰度,不批量化,**预期不触发**(v2.0 修正后)
- **LLM Judge cost spike ($10/24h)** — Sprint 3 B5 加 system prompt 升 R5 judge cost — 监控,可能触发
- **R8 elevation runaway (>50%)** — A5' G10 manual 触发 + R8 KB 同源,**预期不触发**(v2.0 修正后)
- **Failed task rate (>1.20×)** — A2 R14 race fix 后,**预期不触发**(v2.0 修正后)

### 7.3 Phase 5 触发条件

Phase 4 ship + ≥30d production obs + 满足任一:
- AIAC 在 BRAIN(consultant tier)排名进入 top 100
- R12 assistant 模式 GO gate(≥95% PASS rate)PASS → R12 default 翻 ON 决策
- AlphaCrafter / FactorMoE 启发的 portfolio 路线被业务决策正式提出(定位扩展)

---

## 8. v1.0 → v2.0 changelog

| # | v1.0 问题(Round) | v2.0 修正 |
|---|---|---|
| 1 | R12 critical-path 误判 + cross-flag killer(R1/R3 共识)| 降 P2 task-level opt-in,Sprint 2 PR |
| 2 | 依赖图漏 33 ON flag conflict matrix(R1/R3)| 新 §3bis 15 行红黄 matrix |
| 3 | Alembic head 13 字符 + 链未串联(R2)| 重命名 12-char hex + down_revision 链 j→k→l→m→n |
| 4 | `calculate_composite_score` 不存在(R2)| 改 `calculate_alpha_score` + `evaluate_alpha_comprehensive` 双路径 |
| 5 | `agents/services/family_classifier` 等 5 处路径错(R2)| 改 `backend/family_classifier.py` 等真实路径 |
| 6 | R12 GA synthesize 3 人日严重低估(R2)| 砍 synth 路径,改 template + RAG seed,2 人日 |
| 7 | A2 R14 与 BRAIN_AUTH_CIRCUIT race false trigger(R3)| `skipped_due_to_circuit_breaker` flag |
| 8 | LLM_CIRCUIT_BREAKER 缺失(R3)| 新 PR0 Sprint 0 1 人日前置 |
| 9 | A5 G10 与 R8 KB SUCCESS_PATTERN 功能重复(R3)| 砍为 R8 KB entry_subtype=DISTILLED,0.5 人日 |
| 10 | B3 R10-v2 premature optimization(R3)| 推迟到 Sprint 3 末(R10 ≥14d obs)|
| 11 | baseline rebase step 缺失(R3)| A4/B1/A5' PR 内显式步骤 |
| 12 | B5 prompt token 32k 风险(R3)| 8k token budget guard |
| 13 | B4 streaming reject 破坏 LLM 上下文(R3)| 改 whole-output retry |
| 14 | R14 阈值未校准(R1)| Sprint 1 前 0.5 人日 production 30d spike |
| 15 | Sprint 1 GO ship + obs 同 sprint 矛盾(R1)| §5 拆 Sprint 1 ship + 7d obs + Sprint 2 启 |
| 16 | sub-config flag operator 无法 ops UI 调(R2)| 全 PR 显式 SUPPORTED_FLAGS 注册列 |
| 17 | A1 R12 GO gate ≥80% 违反"真改决策"(R3)| 严格化 ≥95% + cost ≤1.2× + KS test |
| 18 | A4 AQR seed 0.5 人日偏乐观(R1)| 改 1 人日含 review buffer |
| 19 | G9 binary "不做"(R1)| §7.3 watch list + 季度复评(BRAIN portfolio API 公告)|

---

## 9. 关联文档

- [`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)
- [`phase4_a_b_plan_2026-05-19.md`](phase4_a_b_plan_2026-05-19.md)(v1.0 历史 snapshot,已归档)
- [`flag_lifecycle.md`](flag_lifecycle.md)
- [`production_canary_sop_2026_05_18.md`](production_canary_sop_2026_05_18.md)
- [`master_implementation_plan_2026-05-17.md`](master_implementation_plan_2026-05-17.md)
- [3 round review reports](../C:/Users/ADMINI~1/AppData/Local/Temp/claude/...) — agent transcripts 在 task output 临时文件

---

*v2.0 是 post-3-round-review ship-candidate。下一步:operator 决策接受 v2.0 → 开 Sprint 0(PR0 LLM_CB)。整 Phase 4 ship 预期完成 2026-06-19 ± 5d。*
