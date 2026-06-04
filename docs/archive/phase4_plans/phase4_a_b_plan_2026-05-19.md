# Phase 4 A+B 落地方案 — 工业 + 学界共识吸收

> **版本**:v1.0(draft,**未 review**)
> **日期**:2026-05-19
> **承前**:
> - [`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)— §8 5 个 gap + §9 优先级建议
> - [`master_implementation_plan_2026-05-17.md`](master_implementation_plan_2026-05-17.md)— Phase 0/1/2/3 已 close
> - [`flag_lifecycle.md`](flag_lifecycle.md)— 双文件 flag 注册 + Tier 1→2→3 promotion
> **scope**:A 5 项强相关 + B 5 项适度吸收 = 10 PR / **26 人日** / 3 sprint
> **明确不含**:G9 portfolio + execution(AlphaCrafter 路线)— 与 AIAC "BRAIN 提交终止" 定位冲突,推迟到定位扩展决策后
> **review path**:plan 落到 v1.0 后,推 3 轮 fresh agent review(per [[project_phase15_plan_ready_2026_05_17]] 模式)

---

## 1. 摘要

| 维度 | 数值 |
|---|---|
| PR 总数 | 10(A 5 + B 5) |
| 人日估算 | 26 人日(含 unit + integration test + Alembic + ops endpoint) |
| Sprint 拆分 | 3 个 sprint(7 + 8 + 11 人日) |
| 新增 ENABLE_* flag | 7 个(R11/R12/R13/R14/G3-v2/G10/R10-v2)+ 复用 3 个(flat-F4 借 ENABLE_FLAT_CONTINUOUS / R8-v3 借 ENABLE_HIERARCHICAL_RAG / AQR seed 无 flag) |
| Alembic migration | 5 个(R11/R13/R14/G10/R10-v2)— 各自带 head 编号 |
| 新增 ops endpoint | 6 个(/ops/r11/capacity-stats /ops/r13/factor-residuals /ops/r14/stop-loss-events /ops/g10/logic-distill /ops/r10-v2/family-bans /ops/r8-v3/cognitive-layer-stats) |
| Phase A 真效果原则 | per [[feedback_按效果选择]] — 5 项 A 必须 PR 内即真改 mining 决策;5 项 B 允许 shadow 起步但 GO gate 必须有 hard-gate 升级路径 |

---

## 2. 设计原则(沿用 AIAC 既有 6 模式)

| # | 原则 | 来源 / 依据 |
|---|---|---|
| 1 | **双文件 flag 注册** — `config.py` 加 ENABLE_* + `feature_flag_service.py` SUPPORTED_FLAGS 注册;未注册被静默忽略 | [[feedback_enable_flag_double_file]] (v1.4 R1a hotfix 520a0d9) |
| 2 | **三阶段 rollout** — shadow(log only)→ soft(warn metric / PROVISIONAL)→ hard(reject)。每阶段 ≥7d obs + GO gate 数据 | [[feedback_light_wiring_deferred_gate]] |
| 3 | **dedicated log table** — cross-node audit 数据走独立表,不复用 alpha.metrics JSONB(避免 1/50 INSERT 吞吐问题) | [[feedback_r1a_dedicated_log_table]] (50× 吞吐) |
| 4 | **Phase A 真效果** — 不默认 observation-only;A 5 项必须 PR 内即产 PASS alpha / 真省 cost | [[feedback_按效果选择]] |
| 5 | **soft-fail 全链** — flag OFF byte-for-byte legacy;flag ON 但 LLM/DB 异常 → 降级到 legacy,**不 block round** | A+ CircuitBreaker [[project_a_plus_circuit_breaker_2026_05_19]] |
| 6 | **L1/L2/L3 ship-state framework** — 每 PR 完成 = 代码 + flag + operational(ops endpoint + monitor + 文档)三层全齐 | [[feedback_ship_state_three_layers]] |

---

## 3. 依赖图

```
        ┌──────────────────────────────────────────────────────────┐
        │                  PR 依赖图(critical path 红色)         │
        └──────────────────────────────────────────────────────────┘

Sprint 1 (P0 风险口闭合 / 7 人日)
├─ A1 R12 LLM_MODE=assistant ───────────────────── 独立,改 node_code_gen
├─ A2 R14 task_stop_loss ───────────────────────── 独立,新建 stop_loss_service
├─ A3 flat-F4 cross-region 平衡 ────────────────── 独立,改 mining_session POST
└─ A4 AQR Kelly/Xiu KB seed ────────────────────── 依赖 R8 hierarchical RAG (已 LIVE)

Sprint 2 (评估 + 风控补强 / 8 人日)
├─ B1 R11 alpha_capacity_estimator ─────────────── 独立,改 alpha_scoring.py
├─ B2 R13 factor_decomposition ──────────────── 关键:check BRAIN sim 返回 daily PnL 可行性(blocker?)
│       └─ 阻塞条件:BRAIN simulate API 不返回 daily PnL → 退到 IS bucket residual
└─ B3 R10-v2 hard forbidden region ─────────────── 依赖 R10 已 LIVE (已 LIVE 2026-05-18)

Sprint 3 (学界 SOTA 演化 / 11 人日)
├─ B4 G3-v2 grammar-aware ─────────────────────── 依赖 G3 shadow LIVE (已 LIVE 2026-05-19)
│       └─ 触发条件:G3 calibrate ≥30 pairs(目前 11)+ shadow → soft 升级时一并做
├─ B5 R8-v3 cognitive layer ──────────────────── 依赖 R8 hierarchical RAG LIVE (已 LIVE 2026-05-18)
└─ A5 G10 logic-as-asset 反向蒸馏 ──────────────── 依赖 R8 KB 写入路径 + 累计 PASS alpha ≥ 50
```

**Critical path**:Sprint 1 A1 R12 是 *定位拉正* 的关键(把 LLM 从 expression-author 拉回 assistant),其他可并行。Sprint 2 B2 R13 是唯一有外部依赖风险(BRAIN sim API 返回结构)的 PR,建议 Sprint 2 内 PR1 先做 spike(0.5 人日)确认可行。

---

## 4. PR 拆分

### 4.1 A1 — R12 `LLM_MODE=assistant` dual-mode(3 人日)

**Source 启示**:8 家工业派共识 — Citadel(Griffin)/ Two Sigma(LLM-for-alt-data)/ Bridgewater AIA(LLM-as-rules-refiner)— **LLM 是 research assistant 不是 expression-author**。AIAC v1 让 LLM 直出 expression 与共识反向。

**ENABLE_* flag**:
```python
# config.py
LLM_MODE: str = "author"  # "author" | "assistant"
ENABLE_LLM_ASSISTANT_MODE: bool = False  # 翻 ON 时 LLM_MODE 强制 assistant
```
**双文件注册**:`feature_flag_service.py` SUPPORTED_FLAGS 加 `ENABLE_LLM_ASSISTANT_MODE`。

**Alembic**:无(无新表)。

**代码改动**:

| 文件:大致行 | 改动 |
|---|---|
| `backend/agents/graph/nodes/generation.py:node_code_gen` | 分支:assistant 模式 LLM 只生 hypothesis_text + reasoning,**expression 走 GA + template** |
| `backend/agents/prompts/prompts.yaml` | 新 `code_gen_assistant_mode` prompt — LLM "describe in plain English" 而非 "output expression" |
| `backend/genetic_optimizer.py` | 暴露 `synthesize_from_hypothesis(hypothesis_text, rag_seeds) -> expression` 给 assistant 路径调用 |
| `backend/agents/graph/state.py` | `MiningState` 加 `llm_mode_used` 字段持久化 |
| `backend/services/llm_mode_service.py` | **新**:`resolve_mode(task, settings) -> "author"|"assistant"`,task.config 可 override 全局 settings(灰度灵活) |

**三阶段 rollout**:本 PR 不走 shadow → 直接 author / assistant 二选一(flag flip 即生效),但 *默认 OFF / author 不变*。Phase A GO gate:某 task 用 assistant 跑 1 周后 PASS rate vs author 对照 ≥80% 即推 default 翻 assistant。

**Phase A 真效果**:assistant 模式必须产 PASS alpha(per [[feedback_按效果选择]]),否则 flag 不进 default。

**ops endpoint**:`/ops/llm-mode/comparison` — 对比 last 7d author vs assistant 的 PASS rate / cost / sharpe 分布。

**验收**:
- 单元测试:assistant 模式 LLM 返回 hypothesis text → GA synthesize 出 valid expression(≥10 个 fixture)
- 集成测试:1 个 mini task 跑 5 round assistant 模式 → ≥1 PASS alpha
- 回归:flag OFF byte-for-byte 等于现行 author 行为(baseline 0 漂移)

---

### 4.2 A2 — R14 `task_stop_loss`(1.5 人日)

**Source**:Millennium 5%/7.5% pod-level hard stop-loss。AIAC task 可无限烧 round 直到 budget 耗尽,无 reward-driven pause。

**ENABLE_* flag**:
```python
ENABLE_TASK_STOP_LOSS: bool = False
TASK_STOP_LOSS_EMA_ALPHA: float = 0.3
TASK_STOP_LOSS_MIN_ROUNDS: int = 5       # warmup
TASK_STOP_LOSS_PASS_RATE_FLOOR: float = 0.05  # < 5% → pause
TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS: int = 3  # 3 round PASS=0 → pause
```

**Alembic**:`backend/alembic/versions/x4f8a2b1c3d5e_task_stop_loss_events.py`
```sql
CREATE TABLE task_stop_loss_events (
  id BIGSERIAL PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES mining_tasks(id) ON DELETE CASCADE,
  triggered_at TIMESTAMP DEFAULT NOW(),
  trigger_reason VARCHAR(40) NOT NULL,  -- pass_rate_floor | consecutive_zero | manual_override
  ema_pass_rate FLOAT,
  consecutive_zero_rounds INT,
  rounds_completed INT,
  ema_window_pass_count INT,
  meta_data JSONB DEFAULT '{}'
);
CREATE INDEX ix_task_stop_loss_task_id ON task_stop_loss_events(task_id);
```

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/services/task_stop_loss_service.py` | **新**:`check_should_pause(task, round_metrics) -> StopLossDecision`;读 task.config["stop_loss_ema"] |
| `backend/tasks/mining_tasks.py:_run_one_round_inline` 末 | round end 调 stop_loss_service;触发 → INSERT event + task.status="PAUSED" + 退出 round loop |
| `backend/models/task.py` MiningTask | 添 hybrid_property `last_stop_loss_event` |

**Phase A 真效果**:flag ON 即真 pause task — 不是 shadow 只 log。

**ops endpoint**:`/ops/task-stop-loss/recent` — list 最近 7d 触发事件 + reason 分布。

**前端**:TaskDetail 加红色 banner "stopped at round N (reason: pass_rate_floor)" + 一键 resume(读 task.config["stop_loss_user_override"]=True 后绕过本轮 check)。

**验收**:
- 单元测试:5 round consecutive zero → trigger;5 round 1 PASS → 不 trigger;EMA 计算正确性
- 集成测试:模拟 task 跑 8 round 故意 0 PASS → 第 3 round 触发 pause,DB INSERT,task.status=PAUSED
- 回归:flag OFF task 永不 pause(byte-equivalent)

---

### 4.3 A3 — flat-F4 cross-region 平衡(2 人日)

**Source**:Millennium 320 pods multi-strategy / Citadel 5 业务线并行。AIAC 当前 region 严重偏 USA。

**ENABLE_* flag**:复用 `ENABLE_FLAT_CONTINUOUS`(已 LIVE)+ 新 `FLAT_CROSS_REGION_QUOTA`:
```python
FLAT_CROSS_REGION_QUOTA: dict = {
    "USA": 0.30,
    "CHN": 0.20,
    "JPN": 0.15,
    "EUR": 0.20,
    "HKG": 0.15,
}
FLAT_CROSS_REGION_ENFORCE: bool = False  # ON = POST 拒绝 quota 越界;OFF = 仅 warn
```

**Alembic**:无。

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/routers/ops.py:start_flat_session` | POST body 加 region 参数验证 → 查 `mining_tasks` last 30d 同 region active task share → 越界 reject 400 / warn |
| `backend/services/flat_session_service.py` | **新**:`compute_region_quota(last_n_days=30) -> Dict[region, share]` |
| `backend/agents/services/strategy_service.py` 中 dataset picking | bandit weight 加 region balance penalty(已偏的 region 降权) |

**Phase A 真效果**:POST 时即 reject 越界(`ENFORCE=True`),flag OFF 仅 warn,但 default 推 ENFORCE=True 跑 1 周。

**ops endpoint**:`/ops/flat-region/distribution` — last 30d active task by region + 与 quota 对比 + 越界报警 chip。

**验收**:
- 单元测试:`compute_region_quota` 在 mock data 上返正确比例
- 集成测试:POST 5 个 USA task → 第 6 个 USA task 被 reject(quota 0.30 超)
- 回归:flag OFF 行为不变

---

### 4.4 A4 — AQR Kelly/Xiu paper KB seed(0.5 人日)

**Source**:AQR Bryan Kelly Yale 学术 IP 流 — 5 篇 SSRN paper 直接 ingest R8 KB。

**ENABLE_* flag**:无(一次性 seed,运行后 KB 数据持久化)。

**Alembic**:无。

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/data/aqr_kelly_seed.json` | **新**:5 篇 paper 摘要 + 5 个核心 hypothesis snippet(autoencoder / large-deep / expected-return-LLM / factor-ML / financial-ML) |
| `scripts/seed_aqr_kelly_paper.py` | **新**:读 json → 调 `external_knowledge.import_paper_pattern(paper_id, hypothesis, expression_template, pillar=PILLAR_INFERENCE)` → UPSERT KB |
| `docs/aqr_kelly_seed_2026-05-19.md` | **新**:5 篇 paper 来源链接 + ingest 决定 |

**Phase A 真效果**:跑完后 KB +5-15 entries(每篇 paper 抽 1-3 hypothesis pattern),R8 retrieval L0 / L1 命中即生效。

**验收**:
- 跑完 script:KB count +5 或更多(per paper),meta_data 含 `qlib_origin: "aqr_kelly_2022"` etc(per [[feedback_forward_compat_metadata_hook]])
- R8 retrieval test:query "expected return prediction" → 命中至少 1 个 AQR seed entry

---

### 4.5 A5 — G10 logic-as-asset 反向蒸馏(4 人日)

**Source**:AlphaLogics(2603.20247, 2026-03-10)— 5 agent reverse-mine。AIAC KB 单向 ingest 缺反向蒸馏闭环。

**ENABLE_* flag**:
```python
ENABLE_LOGIC_DISTILL: bool = False
LOGIC_DISTILL_CADENCE_HOURS: int = 168       # weekly Sunday 03:00 SH
LOGIC_DISTILL_MIN_PASS_COUNT: int = 10       # 至少 10 PASS alpha 才 trigger
LOGIC_DISTILL_TOP_K_LOGIC: int = 5           # 每周抽 5 条 logic
LOGIC_DISTILL_MODEL: str = "claude-haiku-4-5-20251001"
```

**双文件注册**:同。

**Alembic**:`backend/alembic/versions/y5g9b3c2d4e6f_logic_distill_log.py`
```sql
CREATE TABLE distilled_logic (
  id BIGSERIAL PRIMARY KEY,
  distilled_at TIMESTAMP DEFAULT NOW(),
  source_alpha_ids INTEGER[] NOT NULL,  -- 来源 PASS alpha id 列表
  logic_text TEXT NOT NULL,             -- "高 momentum + 低 volatility 在 sector 内分层"
  pillar VARCHAR(40),
  region VARCHAR(8),
  confidence FLOAT,
  used_in_prompt_count INT DEFAULT 0,
  meta_data JSONB DEFAULT '{}'
);
CREATE INDEX ix_distilled_logic_pillar_region ON distilled_logic(pillar, region);
```

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/services/logic_distill_service.py` | **新**:`distill_last_week_pass_alphas() -> List[DistilledLogic]` — fetch last 7d PASS alpha → group by pillar → LLM batch call(`prompts.yaml` 新 `logic_distill_template`)抽 logic → UPSERT |
| `backend/tasks/scheduled_tasks.py` | celery beat 加 `weekly_logic_distill` Sunday 03:00 SH |
| `backend/agents/services/rag_service.py:_get_success_patterns_enhanced` | 末加 fall-through 取 distilled_logic top-K → 注入 PromptContext.distilled_logics(prompt 一段独立 block) |
| `backend/agents/prompts/prompts.yaml` | hypothesis prompt 加 `distilled_logic_block` 段(soft 形式,LLM 仅参考) |

**Phase A 真效果**:flag ON 后第 1 个周末 cron 触发,LLM 抽 logic → 下周 mining round 真注入 prompt;不允许 default observation-only。

**ops endpoint**:`/ops/g10/logic-distill` — list 最近 30d distill 输出 + 每条 used_in_prompt_count + LLM cost(走 G2 cost telemetry)。

**验收**:
- 单元测试:`distill_last_week_pass_alphas` 在 fixture 15 PASS alpha 上抽出 ≥3 logic 条目,meta_data 含 source_alpha_ids
- 集成测试:flag ON 跑 1 周 mock data → DB 新 distilled_logic 行 + 下周 hypothesis prompt 真注入(snapshot test)
- 回归:flag OFF cron task no-op

---

### 4.6 B1 — R11 `alpha_capacity_estimator`(2 人日)

**Source**:RenTec Medallion $10B cap / Bridgewater AIA $5B 软上限 — 高 sharpe 低 capacity alpha 应降权。

**ENABLE_* flag**:
```python
ENABLE_CAPACITY_SCORE: bool = False
CAPACITY_SCORE_WEIGHT: float = 0.10           # 进 composite_score 第 6 维
CAPACITY_LOG_BUCKETS: list = [1e6, 1e7, 1e8, 1e9, 1e10]  # log-scale buckets
```

**Alembic**:`backend/alembic/versions/z6c8d4e3f5a7b_alpha_capacity_metadata.py`
```sql
ALTER TABLE alphas ADD COLUMN capacity_usd_estimate FLOAT;
CREATE INDEX ix_alphas_capacity_usd ON alphas(capacity_usd_estimate) WHERE capacity_usd_estimate IS NOT NULL;
```

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/services/capacity_estimator.py` | **新**:`estimate(alpha) -> float` = ADV_universe × universe_size × (1 - turnover_decay_factor)(粗估,不需精确) |
| `backend/alpha_scoring.py:calculate_composite_score` | 加第 6 维 capacity_norm(log-scale 5 桶 normalize 到 [0,1]),weight `CAPACITY_SCORE_WEIGHT` |
| `backend/agents/graph/nodes/evaluation.py` | PASS alpha persist 前 stamp `alpha.capacity_usd_estimate` |
| `backend/data/region_universe_adv.json` | **新**:静态 region/universe ADV snapshot(20 行,operator 手维护) |

**Phase A**:flag ON 即真改 score(per [[feedback_按效果选择]])。Default OFF,7d obs 后 default → ON。

**ops endpoint**:`/ops/r11/capacity-stats` — last 7d PASS alpha capacity 分布(log-scale histogram)+ 与 sharpe 散点。

**验收**:
- 单元测试:`estimate` 在 mock alpha 上返合理范围(USA TOP3000 ≥$1B,中小 universe < $100M)
- 集成测试:flag ON / OFF 跑同一 task,PASS alpha 数同但排序变(高 capacity sharpe-1.6 排在低 capacity sharpe-1.8 之前)
- 回归:flag OFF byte-for-byte

---

### 4.7 B2 — R13 `factor_decomposition_neutralizer`(4 人日)

**Source**:Two Sigma 18-factor lens / AQR autoencoder asset pricing。AIAC evaluation 只看 sharpe/fitness/turnover/self-corr,无 style factor neutralization。

**前置 spike**(0.5 人日,Sprint 2 第 1 天):验证 BRAIN simulate API 是否返回 daily PnL 时序。

- **若是** → 走 OLS 路径(下述)
- **若否** → 退到 *bucket-level residual*:同 region/universe/period 的 alpha pool 内,target alpha sharpe 减去 pool median sharpe 作 residual

**ENABLE_* flag**:
```python
ENABLE_FACTOR_LENS: bool = False
FACTOR_LENS_MODE: str = "shadow"    # shadow | soft | hard
FACTOR_LENS_FACTORS: list = ["size", "value", "momentum", "quality", "low_vol"]
FACTOR_LENS_RESIDUAL_SHARPE_MIN: float = 0.5    # hard 模式 < τ → FAIL
FACTOR_LENS_OLS_LOOKBACK_DAYS: int = 504        # ~2y daily
```

**Alembic**:`backend/alembic/versions/a7d9e5f4b6c8d_factor_lens_residuals.py`
```sql
CREATE TABLE factor_lens_residuals (
  id BIGSERIAL PRIMARY KEY,
  alpha_id INTEGER NOT NULL REFERENCES alphas(id) ON DELETE CASCADE,
  computed_at TIMESTAMP DEFAULT NOW(),
  residual_sharpe FLOAT NOT NULL,
  factor_exposures JSONB NOT NULL,  -- {"size": 0.12, "value": -0.34, ...}
  r_squared FLOAT,                  -- 风格暴露解释了多少 raw return
  ols_n_days INT,
  mode_used VARCHAR(20)             -- "ols_daily" | "bucket_median"
);
CREATE INDEX ix_factor_lens_alpha_id ON factor_lens_residuals(alpha_id);
```

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/services/factor_lens_service.py` | **新**:`decompose(alpha, daily_pnl_series, factor_returns) -> Residual`(OLS 路径)/ `decompose_bucket(alpha, pool) -> Residual`(bucket 路径,降级) |
| `backend/data/factor_returns_snapshot/{usa,chn,jpn,eur,hkg}.parquet` | **新**:5 region × 5 factor × 2y daily returns 静态 snapshot(operator 手维护,每月刷一次) |
| `backend/agents/graph/nodes/evaluation.py` | PASS alpha 后调 factor_lens(soft-fail);shadow 模式仅 stamp / soft 模式 `quality_status="PASS_PROVISIONAL"` if residual<τ / hard 模式 FAIL |

**三阶段 rollout**(per [[feedback_light_wiring_deferred_gate]]):
1. **shadow**:default OFF;flip ON → log + stamp,无 quality_status 改动
2. **soft**:7d obs ≥30 alpha 有 residual → flip MODE=soft,τ calibrate(scripts/calibrate_r13_threshold.py)
3. **hard**:再 7d obs PASS_PROVISIONAL 中 ≥80% 真 BRAIN can_submit=True → flip MODE=hard

**ops endpoint**:`/ops/r13/factor-residuals` — 6 维(per factor exposure + residual sharpe)分布 + per region threshold + 与 BRAIN can_submit 的关联性。

**验收**:
- 单元测试:OLS 路径在 fixture daily_pnl 上正确分解
- 集成测试:shadow → soft → hard 全链跑通(分别 fixture)
- 回归:flag OFF 完全无影响

---

### 4.8 B3 — R10-v2 hard forbidden region(2 人日)

**Source**:FactorMiner(2602.14670, 2026-02)Experience Memory ℳ — family-level dynamic hard ban N rounds。AIAC R10 是 top-k=2 软限,无硬封禁。

**ENABLE_* flag**:
```python
ENABLE_FAMILY_BAN: bool = False
FAMILY_BAN_MIN_PAIRWISE_CORR: float = 0.85         # 同 family 内 expression 互相关 ≥0.85 触发
FAMILY_BAN_DURATION_ROUNDS: int = 5                # 封禁 N round
FAMILY_BAN_REQUIRE_MIN_SAMPLES: int = 3            # family ≥3 个 alpha 才计算
```

**Alembic**:`backend/alembic/versions/b8e0f6a5c7d9e_family_bans.py`
```sql
CREATE TABLE family_bans (
  id BIGSERIAL PRIMARY KEY,
  family_signature VARCHAR(64) NOT NULL,
  banned_at TIMESTAMP DEFAULT NOW(),
  banned_until_round INT NOT NULL,  -- task.round_count + DURATION
  task_id INTEGER NOT NULL REFERENCES mining_tasks(id) ON DELETE CASCADE,
  trigger_pairwise_corr FLOAT,
  trigger_alpha_ids INTEGER[],
  UNIQUE (task_id, family_signature, banned_at)
);
CREATE INDEX ix_family_bans_task_signature ON family_bans(task_id, family_signature);
```

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/agents/services/family_classifier.py:apply_family_cap` | 末加:计算同 family 内 pairwise self_corr ≥ τ 时 INSERT family_ban + 后续 N round 内同 signature alpha 全 FAIL |
| `backend/agents/graph/nodes/code_gen.py` 起 | 加 ban check:候选 alpha family_signature 命中 active ban → 跳过 |
| `backend/agents/graph/nodes/validation.py` | 同上 check |

**Phase A**:flag ON 即真 ban — 不是 shadow 仅 log。

**ops endpoint**:`/ops/r10-v2/family-bans` — active ban list + 每 ban 影响的 task / 拒绝候选数。

**验收**:
- 单元测试:fixture 5 同 family alpha pairwise ≥0.9 → 1 ban INSERT;下 round 同 family 候选被 reject
- 集成测试:1 mini task 故意生 high-corr family → 触发 ban,N round 后自动 expire
- 回归:flag OFF byte-for-byte

---

### 4.9 B4 — G3-v2 grammar-aware generation(3 人日)

**Source**:AlphaCFG(2601.22119, 2026-01)CFG-guided MCTS — 事前 grammar 约束。AIAC G3 是事后 AST gate(shadow LIVE)。

**触发条件**:G3 shadow 阶段累计 ≥30 calibrate pairs(当前 11)且 G3 升 hard gate 时一并做。可能 Sprint 3 起步时 G3 还在 shadow,B4 顺延。

**ENABLE_* flag**:
```python
ENABLE_GRAMMAR_AWARE_GEN: bool = False
GRAMMAR_MAX_TOKEN_RETRIES: int = 5            # CFG reject 后 LLM 最多重试 N 次
GRAMMAR_CFG_GRAMMAR_PATH: str = "backend/data/alpha_dsl_cfg.lark"
```

**Alembic**:无。

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/data/alpha_dsl_cfg.lark` | **新**:Lark grammar 定义 BRAIN expression 合法 token 序列(operator + arity + arg type 约束) |
| `backend/services/grammar_validator.py` | **新**:`is_valid_token_sequence(partial_expression) -> bool` + `next_legal_tokens(partial) -> Set[token]` |
| `backend/agents/graph/nodes/code_gen.py:_call_llm` | LLM streaming output → 每 token 调 grammar_validator;非法 token → 触发 self-correct prompt(token-level retry) |
| `backend/agents/graph/nodes/validation.py` | 后置 `alpha_semantic_validator` 保留(深度 semantics 检查),CFG 仅前置 syntax |

**Phase A**:flag ON 即 LLM streaming output 走 CFG 约束 — 直接节约 retry cost。

**ops endpoint**:`/ops/g3-v2/cfg-rejects` — last 7d LLM token reject count + per-operator hit rate + 节约的 retry cost(走 G2 cost telemetry)。

**验收**:
- 单元测试:`grammar_validator` 在 fixture 表达式上正确通过 / 拒绝(20 个 +/- case)
- 集成测试:flag ON 跑 mini task → LLM token reject 真触发 retry + 最终生成 valid expression
- 回归:flag OFF byte-for-byte

---

### 4.10 B5 — R8-v3 cognitive layer 调度(4 人日)

**Source**:CogAlpha(2511.18850, 2025-11)7 层 cognitive agent(Market Structure → Extreme Risk → Price-Volume → Price-Vol → Multi-Scale Complexity → Stability-Gating → Geometric/Fusion)+ 5-mode paraphrase。

**复用 flag**:`ENABLE_HIERARCHICAL_RAG`(已 LIVE)+ 新维度:
```python
ENABLE_COGNITIVE_LAYER_PROMPT: bool = False
COGNITIVE_LAYER_LIST: list = [
    "market_structure", "extreme_risk", "price_volume",
    "price_volatility", "multi_scale", "stability_gating", "geometric_fusion"
]
COGNITIVE_LAYER_SELECT_MODE: str = "bandit"   # bandit | round_robin | deficit_aware
COGNITIVE_PARAPHRASE_MODES: list = ["light", "moderate", "creative", "divergent", "concrete"]
```

**Alembic**:无(复用 alpha.metrics JSONB stamp + r8_query_log)。

**代码改动**:

| 文件 | 改动 |
|---|---|
| `backend/agents/prompts/cognitive_layers.yaml` | **新**:7 层各自的 system prompt 头部(每层 200-400 字描述聚焦方向) |
| `backend/services/cognitive_layer_service.py` | **新**:`select_layer(task, recent_pillar_deficit, bandit_state) -> str`(bandit / round_robin / deficit_aware 三策略) |
| `backend/agents/graph/nodes/hypothesis.py:node_hypothesis` | prompt 渲染前选层 → 把 layer system prompt 注入 PromptContext.cognitive_system_prompt(prepend) |
| `backend/agents/services/g5_crossover_service.py` | G5 crossover prompt 加 5-mode paraphrase 选项 |

**Phase A**:flag ON 即真改 LLM prompt(每个 hypothesis call 用 1/7 系统 prompt)。Default OFF。

**ops endpoint**:`/ops/r8-v3/cognitive-layer-stats` — last 7d 每 layer 调用次数 + 每 layer 产 PASS alpha 数 + deficit 状态。

**验收**:
- 单元测试:`select_layer` 在三策略下分别正确(bandit Beta-Bernoulli prior;round_robin 7 round 后均匀;deficit-aware 命中 pillar deficit max)
- 集成测试:flag ON 跑 14 round → 7 层各被选中至少 1 次(round_robin)/ ≥2 次(bandit 探索期)
- 回归:flag OFF byte-for-byte

---

## 5. Sprint 拆分

### Sprint 1 — P0 风险口闭合(2026-05-20 ~ 05-26 / 7 人日)

| PR | 人日 | 验收 |
|---|---|---|
| A1 R12 LLM_MODE=assistant | 3 | mini task 跑 5 round assistant 模式 ≥1 PASS |
| A2 R14 task_stop_loss | 1.5 | 8 round 0-PASS 模拟触发 pause |
| A3 flat-F4 cross-region | 2 | POST 5 USA task → 第 6 reject |
| A4 AQR Kelly KB seed | 0.5 | KB +5 entries + R8 retrieval 测试命中 |

**Sprint 1 GO 标准**:全 4 PR ship master + baseline 0 漂移 + R12 default 翻 assistant + R14 default OFF(operator 24h obs 后 flip ON)+ flat-F4 ENFORCE 翻 ON。

---

### Sprint 2 — 评估 + 风控补强(2026-05-27 ~ 06-05 / 8 人日)

| PR | 人日 | 备注 |
|---|---|---|
| B2 spike R13 BRAIN sim daily PnL | 0.5 | 决定 OLS 还是 bucket 路径 |
| B1 R11 alpha_capacity_estimator | 2 | composite_score 第 6 维 |
| B2 R13 factor_decomposition shadow | 3.5 | 走 spike 决定的路径 |
| B3 R10-v2 hard forbidden region | 2 | 复用 family_classifier |

**Sprint 2 GO 标准**:全 4 PR ship master + R11 default ON(7d obs PASS)+ R13 shadow ON 累 ≥30 alpha residual 数据 + R10-v2 default ON。

---

### Sprint 3 — 学界 SOTA 演化(2026-06-06 ~ 06-19 / 11 人日)

| PR | 人日 | 触发条件 |
|---|---|---|
| B4 G3-v2 grammar-aware | 3 | G3 shadow → soft 升级时一并做 |
| B5 R8-v3 cognitive layer | 4 | R8 LIVE 已满足 |
| A5 G10 logic-as-asset 反向蒸馏 | 4 | PASS alpha 累 ≥50 已满足 |

**Sprint 3 GO 标准**:全 3 PR ship master + B5 cognitive layer default ON(round_robin 起步)+ G10 weekly cron LIVE + B4 跟随 G3 hard gate 一起翻 ON。

---

## 6. 风险 / 反例

### 6.1 已识别风险

| # | 风险 | 缓解 |
|---|---|---|
| 1 | R12 assistant 模式 LLM 抽象 hypothesis → GA synthesize expression PASS rate 不如 author | A1 GO gate 严卡 PASS rate ≥80% author baseline,不达标 default 不翻 |
| 2 | R13 BRAIN sim 不返回 daily PnL → OLS 路径无法做 | B2 PR1 0.5 人日 spike,提前发现 → 退到 bucket 路径(已设计) |
| 3 | R10-v2 family ban 误伤异质 alpha(family_signature 粒度过粗) | min_samples=3 + duration=5 round soft expire;ops endpoint 可手动 unban |
| 4 | A4 AQR paper 抽 hypothesis 质量低 → KB pollute | 人工 review 5 篇抽取的 ≥15 个 hypothesis snippet 再 commit script |
| 5 | B5 cognitive layer LLM cost 增加(每 round 7 选 1) | 复用 G2 cost telemetry 监控;cost > $0.10/call 触发告警 |
| 6 | A5 logic distill LLM 生成 logic 与 hypothesis prompt 冲突 | 限 used_in_prompt_count 上限 + 每 logic 显示置信度;observation week 1-2 |

### 6.2 明确反例(per v2 §9.5)

- **G9 portfolio + execution** — 不做,定位边界外
- **TLRS RL 全栈** — 不做,BRAIN sim 限额下不划算
- **RenTec signal-leak jitter** — 排到 P3 quick win,本 plan 不含
- **D.E. Shaw 单点 ML czar** — 不做,与多 agent 设计冲突
- **Jane Street OCaml 重写** — 不做,Python+FastAPI 既有 stack 不动

---

## 7. 验收 / 退役标准

### 7.1 Phase 4 整体 ship 完成标准

| L | 标准 |
|---|---|
| L1 代码 | 10 PR 全 master,unit + integration test 全 PASS,baseline 0 漂移 |
| L2 flag | 7 个新 flag 双文件注册 + production override 翻 ON(per [[feedback_no_reflex_flag_cleanup]] 不要默认关掉)|
| L3 operational | 6 个 ops endpoint LIVE + 前端 Monitor 页(R11/R13/R14 至少各 1 个 chart 进 Dashboard)+ 文档 commit |

### 7.2 Phase 5 触发条件

Phase 4 ship 完成 + ≥30d production obs + 满足下列任一:

- AIAC 在 BRAIN 排名(consultant tier)进入 top 100
- AlphaCrafter / FactorMoE 启发的 portfolio 路线被业务决策正式提出(定位扩展)
- 出现新 SOTA 学界论文且与 AIAC 现有机制 ≥3 项 conflict / 升级

---

## 8. 后续 review path

**v1.0 → v1.1 触发**:plan 落到 v1.0 后,推 3 轮 fresh agent review(per [[project_phase15_plan_ready_2026_05_17]] 三轮模式):

1. **Round 1**:general-purpose agent 全文 review → MUST + SHOULD list,inline fix
2. **Round 2**:Plan agent 重 review → 漏的 edge case / dependency / acceptance gaps
3. **Round 3**:Code-reviewer agent 针对 5 个 Alembic + ENABLE_* flag 设计做 sanity check
4. **v1.3 ship-ready** 后开 Sprint 1

**实施推下 session**(per 既有 plan 风格,plan 与 ship 不同 session)。

---

## 9. 关联文档

- [`competitive_analysis_v2_2026-05-19.md`](competitive_analysis_v2_2026-05-19.md)— §8 5 gap + §9 优先级建议
- [`master_implementation_plan_2026-05-17.md`](master_implementation_plan_2026-05-17.md)— Phase 0/1/2/3 status
- [`flag_lifecycle.md`](flag_lifecycle.md)— Tier 1/2/3 promotion + 双文件注册
- [`phase15_task_schema_refactor_plan.md`](phase15_task_schema_refactor_plan.md)— plan doc 风格参考
- [`production_canary_sop_2026_05_18.md`](production_canary_sop_2026_05_18.md)— canary 流程 + rollback trigger

---

*本 plan v1.0 是 draft 未 review。下一步:推 3 轮 fresh agent review 升 v1.3,然后开 Sprint 1。整 Phase 4 ship 完成预期 2026-06-19 ± 5d。*
