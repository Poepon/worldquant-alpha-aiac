# 优化闭环 — Plan v1
**日期**:2026-05-28
**状态**:设计阶段 — 待 A→B 和 B→C 的 GO/NO-GO 数据 gate
**背景**:本次 session 跑了 4 个 mining session(3740-3743)出 82 个 alpha / 1 个 BRAIN 候选(15621)/ 1 个手动优化并提交的变异(15720)。实证 **对一个近门 alpha 加一层外层 neutralization 就把 sharpe 从 1.87 拉到 2.18**——一次 settings 翻转。问题:这件事能不能做成持续闭环?

本 plan 定义把"优化"做成 pipeline 一等公民的分阶段路径——以及每个阶段决定推进还是停止的数据 gate。

---

## 0. TL;DR

| 阶段 | 投入 | 做什么 | 推进下一档的 gate |
|---|---|---|---|
| **A** | 2-3 天 | beat 周期触发 near-gate alpha 的 settings sweep → 写入 backlog(人工 submit) | 14 天 cohort 的转化率 >20% |
| **B** | 3-4 天 | + 表达式 rewrites + budget allocator + 安全 winner 的 auto-submit | 14 天 auto-submit 的 BRAIN 通过率 >50% |
| **C** | 1-2 周 | + 全 GA + pipeline-hook 触发 + RAG 反喂 | mining PASS-rate 可测量地上升 |

**STOP 信号同样硬性**:Stage A 转化率 <10% → selection 才是真墙(per `reference_competitive_analysis_v3_2026_05_26`),优化不值 BRAIN 配额。**没数据撑就不升档**。

---

## 1. 真的值得做吗(以及为什么可能不值)

### 真实积压快照(2026-05-28)

| 状态 | delay-1 | delay-0 | 备注 |
|---|---|---|---|
| 历史提交总数 | — | — | **12 个**(跨两个 delay) |
| can_submit 但未提交(backlog) | — | — | **121** — 已被 `ops/submit-backlog` 页面处理(正交杠杆) |
| 近门 [hard_gate−0.5, hard_gate) | **1230** | 2 | 优化目标池 |

delay-1 的 1230 个近门 alpha 是理论金矿。**上限数学**:即使只有 5% 转化,就有 ~60 个新可提交 alpha——是历史提交总数的 5 倍。

### 真正怀疑这件事的理由(反方论证)

1. **`competitive_analysis_v3`(2026-05-26)** 显示 AIAC 是 **selection-limited 而非 discovery-limited**。1230 池转化率可能 <5%,因为底层信号和已提交 alpha 太相似(self-corr ≥ 0.7 撞墙)。
2. **settings-sweep 的 alpha 提升期望值很小**。15621 这个 case(+0.31 sharpe via neut=INDUSTRY)是单样本——可能是分布的高位。
3. **BRAIN 配额有限**(1000 sim/day)。优化抢 mining 的预算;如果 mining 是 2.3 alpha/session 而优化是 0.3 alpha/cycle,这个交换是亏的。
4. **`project_depth_levers_refuted`(2026-05-25)** 在对抗审查后明确否决了深度轴投入。优化是深度杠杆(同一个信号更多 sim)。

**所以 Stage A 的 GO gate 是数据驱动的,不是愿景驱动的。**

---

## 2. 现有零件清单(别重发明)

| 模块 | 它有什么 | 状态 |
|---|---|---|
| `backend/optimization_chain.py` | `generate_local_rewrites` / `generate_settings_variants` / `run_optimization_chain` / 4 类 mutator / 优先级逻辑 | ✅ ready,只被 legacy `mining_agent._run_optimization_chain` 用 |
| `backend/genetic_optimizer.py` | `run_genetic_optimization` / island model(4×12×5=240 sim)/ 多保真度网格 / `OptimizationConfig` | ✅ ready,0 个生产调用者 |
| `backend/marginal_analysis.py` + `audit_iqc_marginal_for_alpha` | SUBMIT/NEUTRAL/SKIP 推荐 / IQC 边际打分卡 | ✅ ready,被 ops backlog 页用 |
| `evaluation.py:should_optimize` + `EVAL_SCORE_OPTIMIZE` | 每 alpha 的"应优化"信号 | ✅ 已计算,**0 消费者**(信号悬空) |
| `BrainAdapter._acquire_sim_slot` / `_release_sim_slot` + Redis 计数器 | role-aware sim 槽分配 | ✅ ready |
| `routers/ops.py:/submit-backlog` | 人工 submit 队列 + 扫描触发 | ✅ ready(memory `project_ops_audit_r11fix_backlog_drain_2026_05_28`) |

缺什么——汇总到下面的分层架构里。

---

## 3. 分层架构(4 层 — A 全建,B/C 只 swap/add)

```
┌─ Layer 4: 触发 ───────────────────────────────────────────────┐
│  A: beat 每 6h                                                 │
│  B: A + BrainBudgetAllocator(mining vs opt 二分)              │
│  C: B + pipeline-hook(consumer 把 near-miss 推 opt_q)         │
├─ Layer 3: 编排器(签名 A→C 不变)──────────────────────────────┤
│  OptimizationService.run_one_cycle(candidate, budget) →        │
│     VariantGenerator.generate(alpha)                           │
│     Simulator.run_batch(variants, budget)                      │
│     WinnerSelector.pick(sim_results)                           │
│     Persister.save(winners, parent_alpha_id, opt_run_id)       │
│     SubmitPolicy.decide(persisted) → action                    │
│     KnowledgeFeedback.on_winner(alpha)   ← C 才接;A/B 是 no-op │
├─ Layer 2: VariantGenerator(A→B→C 的 SWAP 点)─────────────────┤
│  A: SettingsSweepGenerator(decay/window/neut,~11 变异)        │
│  B: CompositeGenerator(Settings, ExpressionRewrites)~30        │
│  C: GeneticOptimizerGenerator(全 GA,240 sim)— tier 路由      │
├─ Layer 1: 共享原语(A 建好,B/C 不动)────────────────────────┤
│  - select_near_gate_candidates(delay, limit, exclude_hashes)   │
│  - OptimizationRun(DDL 在下面)                                │
│  - SimBudget 计数器(per-cycle/per-day;A 不限也要记)          │
│  - SelfCorrCache(每个 winner 都算,A→C;B+ 才消费)            │
└────────────────────────────────────────────────────────────────┘
```

**这套为什么走得通**:Layer 3 编排器签名第一天就*冻结*——A/B/C 只**在 protocol 后面 swap 具体实现**。§7 的 5 个 anti-pattern 就是违反这个纪律的具体表现。

---

## 4. Protocol 签名(A 时期就建好)

```python
# backend/services/optimization/protocols.py
from typing import Protocol, List, Optional, Literal
from dataclasses import dataclass

@dataclass
class Variant:
    expression: str
    settings: dict        # region/universe/delay/decay/neutralization/truncation
    tag: str              # 人类可读: "neut=INDUSTRY" / "window=45"
    generator_name: str   # "settings_sweep" / "expression_rewrite" / "ga"
    generation: int = 0   # GA 代数;settings 恒 0

@dataclass
class VariantSimResult:
    variant: Variant
    sim_response: dict    # 完整 BRAIN 响应
    sharpe: Optional[float]
    fitness: Optional[float]
    turnover: Optional[float]
    margin: Optional[float]
    brain_alpha_id: Optional[str]
    checks_passed: bool   # 所有 BRAIN gate 通过
    self_corr: Optional[float]   # Simulator 算,给 SubmitPolicy cache
    error: Optional[str] = None

class VariantGenerator(Protocol):
    name: str             # 给 telemetry + audit trail 用
    async def generate(self, alpha) -> List[Variant]: ...
    # alpha 是 backend.models.Alpha 行;读 expression + settings

class Simulator(Protocol):
    async def run_batch(
        self, variants: List[Variant], budget: int
    ) -> List[VariantSimResult]: ...
    # 即使不限额也必须更新 SimBudget 计数器

class WinnerSelector(Protocol):
    def pick(
        self, results: List[VariantSimResult], delay: int
    ) -> List[VariantSimResult]: ...
    # 用 settings.eval_thresholds(delay) — 已是 delay-aware(b8a9560)

class Persister(Protocol):
    async def save(
        self, winners: List[VariantSimResult],
        parent_alpha_id: int, opt_run_id: int
    ) -> List[int]: ...
    # 返回新落库的本地 alpha PK 列表

class SubmitPolicy(Protocol):
    async def decide(
        self, persisted_pks: List[int]
    ) -> List[Literal["submit", "queue", "skip"]]: ...

class KnowledgeFeedback(Protocol):
    async def on_winner(self, alpha) -> None: ...   # A/B 无操作;C 接 RAG
```

---

## 5. OptimizationRun DDL(Stage A 的 Alembic — 不可妥协)

```sql
CREATE TABLE optimization_runs (
    id                  SERIAL PRIMARY KEY,
    parent_alpha_id     INTEGER NOT NULL REFERENCES alphas(id),
    generator_name      VARCHAR(64) NOT NULL,            -- "settings_sweep" / "composite" / "ga"
    trigger_source      VARCHAR(32) NOT NULL,            -- "beat" / "pipeline_hook" / "manual"
    n_variants          INTEGER NOT NULL DEFAULT 0,
    n_winners           INTEGER NOT NULL DEFAULT 0,
    n_submitted         INTEGER NOT NULL DEFAULT 0,      -- SubmitPolicy 决定的
    sim_budget_used     INTEGER NOT NULL DEFAULT 0,      -- 实花 BRAIN sim 数
    sim_budget_granted  INTEGER NOT NULL,                -- budget allocator 的预算
    cycle_started_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    cycle_finished_at   TIMESTAMP,
    error               TEXT,                             -- 非空 = cycle 中断
    metadata            JSONB DEFAULT '{}'::jsonb         -- generator 私有数据
);
CREATE INDEX ix_opt_runs_parent ON optimization_runs(parent_alpha_id);
CREATE INDEX ix_opt_runs_started ON optimization_runs(cycle_started_at DESC);

-- alphas 表:加链接(winner 指回它的 cycle)
ALTER TABLE alphas ADD COLUMN optimization_run_id INTEGER
    REFERENCES optimization_runs(id);
CREATE INDEX ix_alphas_opt_run ON alphas(optimization_run_id) WHERE optimization_run_id IS NOT NULL;
```

**为什么用表而不是 metadata JSONB**:转化率查询是 GO-gate 信号——
`SELECT n_winners::float / NULLIF(n_variants, 0) FROM optimization_runs WHERE generator_name = 'settings_sweep' AND cycle_started_at > NOW() - INTERVAL '14 days'` 一条 SQL 搞定;同样的查询打散到 alphas 的 JSONB 里就既不可读也不可索引。

---

## 6. 各阶段详细 spec

### Stage A — MVP(2-3 天)

**代码**:
- `backend/services/optimization/service.py`(OptimizationService 类 + 4 层接线)
- `backend/services/optimization/generators/settings_sweep.py`(我手工实证过的 11 变异 generator)
- `backend/services/optimization/simulator.py`(并发 sim,用 `_acquire_sim_slot`,op_timeout 600s,记账 budget)
- `backend/services/optimization/winner_selector.py`(用 `settings.eval_thresholds(delay)`)
- `backend/services/optimization/persister.py`(写 `alphas` + `optimization_run`;计算并存 `_self_corr`)
- `backend/services/optimization/submit_policy.py`(Stage A:永远返回 "queue")
- `backend/tasks/optimization_tasks.py`(beat 调度的 `run_optimization_cycle`)
- Alembic migration:`optimization_runs` 表 + `alphas.optimization_run_id`
- flag:`ENABLE_OPTIMIZATION_LOOP`(默认 OFF)
- config:`OPT_BEAT_INTERVAL_HOURS=6` / `OPT_CANDIDATES_PER_CYCLE=10` / `OPT_DAILY_SIM_BUDGET=400`(A 不限但记录)
- telemetry:`GET /ops/optimization/cycles`(近 50 个 cycle + 转化率汇总)

**预算**:10 候选 × 11 变异 = 110 sim/cycle × 4 cycle/天 = 440 sim/天(~44% BRAIN 配额;mining 还有 560)。

**SubmitPolicy**:永远 "queue" → winner 落 `submit-backlog` 页,用户手动 submit。

**14 天观察指标**:
- 全 cycle 的 `n_variants_total`
- `n_winners / n_variants`(转化率)
- `n_winners_human_submitted / n_winners`(人工转化率)
- `n_winners_actually_pass_brain / n_winners_human_submitted`(gate 通过率)

**GO 到 B**:winner 转化率 >20% AND 累计 ≥30 个 cycle(≥330 个变异 sim 过)。
**STOP**:转化率 <10% — selection 墙被实证确认,放弃优化路径。
**部分通过**(10-20%):持等,调 SettingsSweepGenerator 参数再判,不直接升档。

---

### Stage B — 表达式 rewrites + auto-submit(3-4 天,纯加法)

**代码变更**:
- `backend/services/optimization/generators/expression_rewrite.py`(包 `generate_local_rewrites` 来自 optimization_chain.py)
- `backend/services/optimization/generators/composite.py`(组合多个 generator)
- `backend/services/optimization/submit_policy.py`(把"永远 queue"换成:如果 `self_corr<0.7` AND 所有 BRAIN check 通过 → "submit";否则 "queue")
- `backend/services/optimization/budget_allocator.py`(从 Redis 计数器读当日 mining sim 花费,把剩余分给 opt)
- config:`OPT_AUTO_SUBMIT=true` / `OPT_AUTO_SUBMIT_SELF_CORR_MAX=0.65`(比 BRAIN gate 0.7 紧,留安全 margin)
- telemetry 增:每 cycle 的 `n_auto_submitted` + `n_auto_submitted_actually_landed`

**预算**:10 候选 × 30 变异 = 300 sim/cycle × 4 = 1200/天 → **超配额**。三选一:
- 降到 2 cycle/天(600 sim,留 400 给 mining)—— 推荐
- 候选降到 5(15 sim × 2 cycle = 300/天)
- 等 CONSULTANT 模式(80 槽,无配额问题)

**Layer 1 / Layer 3 编排签名 0 改;Layer 2 加 generator,Layer 4 加 allocator。Layer 3 唯一变的是 SubmitPolicy 的具体实现 swap。**

**GO 到 C**:14 天 auto-submit 的 BRAIN 通过率 >50%(policy 自动 submit 的 winner 在 BRAIN 上真的落地)AND 总提交数 ≥ 优化前 baseline 的 3 倍。
**STOP**:通过率 <30% → SubmitPolicy 太宽松,要么收紧、要么回退 auto-submit、维持 B-with-queue 状态。

---

### Stage C — 全 GA + pipeline-hook + RAG(1-2 周,纯加法)

**代码变更**:
- `backend/services/optimization/generators/genetic.py`(包 `run_genetic_optimization`;tier 路由:浅近门 → composite;深近门 score>0.6 → GA)
- `backend/agents/pipeline/runner.py`:加 `opt_q`(async queue)+ 在 consumer 的 persist 阶段加 hook —— 当 `should_optimize` 为 True 时推 near-miss `SimResult`
- `backend/services/optimization/feedback.py`(KnowledgeFeedback 实现):winner 出现时把 `(expression, hypothesis, mutation_path, before/after_sharpe)` 写入 `r8_patterns` 表给 RAG L1 检索用
- `backend/services/optimization/submit_policy.py`:接入 `marginal_analysis` 做 3 路 SUBMIT/NEUTRAL/SKIP
- config:`OPT_PIPELINE_HOOK=true` / `OPT_GA_BUDGET_PER_RUN=240` / `OPT_KNOWLEDGE_FEEDBACK=true`

**预算**:重。分层:
- composite-generator 候选:同 Stage B(300/cycle)
- GA 候选:240 sim × 1-2 alpha/天 = 240-480/天
- 加 pipeline-hook 突发(由 `opt_q` maxsize 约束)
- **CONSULTANT 80 槽是稳定运行的硬要求**

**Pipeline hook 安全性**:hook 中的 consumer 非阻塞推 `opt_q`(满了就 drop);另一个 `pipeline_opt_consumer` 通过 OptimizationService 排空它。Heartbeat supervisor(`2f3dd58`)继续生效——`opt_q` **不算**进度 beat(只有 persist + push 算),所以一个卡住的 opt 不会假装让父 pipeline 显得还活着。

**Layer 1 0 改;Layer 3 SubmitPolicy + KnowledgeFeedback 是加法;Layer 4 多一个触发器(pipeline-hook)和 beat 并存。**

---

## 7. 5 个 anti-pattern(A 时期不能踩的坑,踩了 B/C 阻塞)

| Anti-pattern | "A 时感觉没事" | B/C 时返工代价 |
|---|---|---|
| 不建 `optimization_run` 表,直接 `INSERT alphas` | "parent_alpha_id 够追溯了" | 没法算转化率、没法 dedup、没法看 cycle telemetry → B 时回填历史 + Alembic 加表 |
| inline `generate_variants_*()` 调用(不抽 protocol) | "现在就 1 个 generator" | B/C 加 generator 时改 service + 重测全部(0.5 天前期抽 protocol 省 2 天后期返工) |
| 不记 SimBudget(A 不限额) | "暂时不需要" | B 的 allocator 没历史数据 calibrate budget → 拍脑袋默认 → 撞配额 |
| 不算 winner 的 `_self_corr` | "A 不 auto-submit 用不到" | C 的 auto-submit 需要 cached self_corr;没有就要重 sim → ~30% 预算浪费 |
| 持久化路径没预留 `on_winner(alpha)` callback | "C 还远" | C 的 RAG hook 变成跨切关注点改动,牵一发动全身 |

---

## 8. 开工前必决的问题(A 启动前决定)

1. **A 的触发源**:`should_optimize` 信号(已算,语义"这个 alpha 应优化")vs SQL 近门带扫描(具体,但绕开 verdict 系统)?
   - 推荐:**SQL 近门带**(简单 + 可观测)。`should_optimize` 还带 cascade 时代的历史包袱。

2. **优化 vs mining 配额**:硬分(比如 400/600)vs 动态(opt 拿 mining 剩下的)?
   - 推荐:Stage A **硬分**(可预测、好调试)。Stage B allocator 上线后改动态。

3. **dedup key**:只 `expression_hash` 还是 `(expression_hash, parent_alpha_id_family)` 避免重复优化同一个家族链?
   - 推荐:第一天就**两个都记**;A 按 `expression_hash` 查询,B 加家族感知 dedup 无需改 schema。

4. **legacy `mining_agent._run_optimization_chain` 怎么办**:留(并行路径)、删(legacy 已退役)、还是迁移(新 OptimizationService 是它的继任者)?
   - 推荐:**A 时期直接删**。它是 cascade 时代挂在死路径上的 hook,留着是未来的混淆来源。

5. **A 的 beat 间隔**:6h 意味着 ~4 cycle/天。要不要绑定 BRAIN 日配额重置时间(00:00 UTC)?
   - 推荐:6h 基线,由 `_pipeline_heartbeat_timeout()` 同款 backstop 兜底。配额重置最好交给 `quota_guard_pause_at_threshold`(已存在)。

---

## 9. References

- `reference_competitive_analysis_v3_2026_05_26.md` — selection-limited 诊断(STOP gate 的依据)
- `project_marginal_submit_recommendation_2026_05_24.md` — Stage C 集成的 SUBMIT/NEUTRAL/SKIP 打分卡
- `project_ops_audit_r11fix_backlog_drain_2026_05_28.md` — submit-backlog 页面(Stage A queue policy 的去处)
- `project_depth_levers_refuted_breadth_is_answer_2026_05_25.md` — 之前被数据否决的深度轴投资(本深度轴的警示)
- `project_split_producer_first_live_freeze_2026_05_28.md` — pipeline 永冻诊断(Stage C pipeline-hook 必须尊重 heartbeat supervisor)
- `feedback_按效果选择.md` — Stage A 必须改变 BRAIN 实际产出,不能只观察;14 天 gate 就是执行这条
- `b8a9560` — delay-aware `settings.eval_thresholds(delay)`(Stage A WinnerSelector 读这个)
- 实战数据 15621 → 15720(手工实证:settings sweep 单独把 sharpe 从 1.87 拉到 2.18 → BRAIN SUBMITTED)
