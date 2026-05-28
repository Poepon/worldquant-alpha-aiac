# Optimization Closure — Plan v1
**Date**: 2026-05-28
**Status**: design — pending GO/NO-GO at A→B and B→C gates
**Context**: 4 mining sessions (3740-3743) shipped 82 alpha / 1 BRAIN candidate (15621) /
1 manually-optimized + submitted variant (15720). Demonstrated that **adding outer
neutralization to a near-gate alpha lifted sharpe 1.87→2.18** with one settings flip.
Question: can this be a continuous closed loop?

This plan defines the staged path to make optimization a first-class pipeline
step — and the data gates at each stage that decide whether to advance or stop.

---

## 0. TL;DR

| Stage | Investment | What it does | GO-to-next gate |
|---|---|---|---|
| **A** | 2-3 days | Beat-scheduled settings sweep on near-gate alphas → backlog (human submit) | Conversion >20% on 14-day cohort |
| **B** | 3-4 days | + Expression rewrites + budget allocator + auto-submit on safe winners | Auto-submit pass-rate >50% over 14 days |
| **C** | 1-2 weeks | + Full GA + pipeline-hook trigger + RAG feedback loop | mining PASS-rate improves measurably |

**STOP signals are equally binding**: A's conversion <10% → selection is the real wall (per `reference_competitive_analysis_v3_2026_05_26`), optimization isn't worth the BRAIN budget. Don't escalate without data.

---

## 1. Why this might be worth doing (and might not)

### Real backlog snapshot (2026-05-28)

| State | delay-1 | delay-0 | Notes |
|---|---|---|---|
| submitted ever | — | — | **12 total** across both delays |
| can_submit + unsubmitted (backlog) | — | — | **121** — already addressed by `ops/submit-backlog` page (orthogonal lever) |
| near-gate [hard_gate−0.5, hard_gate) | **1230** | 2 | Optimization target pool |

The delay-1 pool of 1230 near-gate alphas is the theoretical jackpot. **Upper-bound math**: even a 5% conversion rate yields ~60 new submittable alphas — 5× the lifetime submitted count.

### Real reasons to be skeptical (counter-arguments)

1. **`competitive_analysis_v3` (2026-05-26)** showed AIAC is **selection-limited, not discovery-limited**. The 1230 pool may convert at <5% because the underlying signals are too similar to already-submitted alphas (self-corr ≥ 0.7 wall).
2. **Settings-sweep alpha lift is small in expectation**. The 15621 case (+0.31 sharpe via neut=INDUSTRY) is one sample — could be high-end of the distribution.
3. **BRAIN quota is finite** (1000 sim/day). Optimization steals from mining budget; if mining produces 2.3 alpha/session and optimization produces 0.3 alpha/cycle, the swap loses.
4. **`project_depth_levers_refuted` (2026-05-25)** explicitly rejected depth-axis investments after adversarial review. Optimization is a depth lever (more sims per signal).

**These are why Stage A's GO gate is data-driven, not aspirational.**

---

## 2. Existing parts (don't re-invent)

| Module | What it has | Status |
|---|---|---|
| `backend/optimization_chain.py` | `generate_local_rewrites`, `generate_settings_variants`, `run_optimization_chain`, 4 mutator families, priority logic | ✅ ready, used by legacy `mining_agent._run_optimization_chain` only |
| `backend/genetic_optimizer.py` | `run_genetic_optimization`, island model (4×12×5=240 sim), multi-fidelity grid, `OptimizationConfig` | ✅ ready, 0 production callers |
| `backend/marginal_analysis.py` + `audit_iqc_marginal_for_alpha` | SUBMIT/NEUTRAL/SKIP recommendation, IQC marginal scorecard | ✅ ready, used by ops backlog page |
| `evaluation.py:should_optimize` + `EVAL_SCORE_OPTIMIZE` | Per-alpha "should optimize" signal | ✅ computed, **0 consumers** (signal is dangling) |
| `BrainAdapter._acquire_sim_slot` / `_release_sim_slot` + Redis counter | Role-aware sim slot allocation | ✅ ready |
| `routers/ops.py:/submit-backlog` | Human-facing submit queue + scan trigger | ✅ ready (memory `project_ops_audit_r11fix_backlog_drain_2026_05_28`) |

What's missing — gathered into the layered architecture below.

---

## 3. Layered architecture (4 layers — A builds all 4, B/C swap/add)

```
┌─ Layer 4: Trigger ─────────────────────────────────────────────┐
│  A: beat every 6h                                              │
│  B: A + BrainBudgetAllocator (mining vs opt split)             │
│  C: B + pipeline-hook (consumer pushes near-miss to opt_q)     │
├─ Layer 3: Orchestrator (signature INVARIANT A→C) ──────────────┤
│  OptimizationService.run_one_cycle(candidate, budget) →        │
│     VariantGenerator.generate(alpha)                           │
│     Simulator.run_batch(variants, budget)                      │
│     WinnerSelector.pick(sim_results)                           │
│     Persister.save(winners, parent_alpha_id, opt_run_id)       │
│     SubmitPolicy.decide(persisted) → action                    │
│     KnowledgeFeedback.on_winner(alpha)   ← C only; A/B no-op   │
├─ Layer 2: VariantGenerator (SWAP point A→B→C) ─────────────────┤
│  A: SettingsSweepGenerator (decay/window/neut, ~11 variants)   │
│  B: CompositeGenerator(Settings, ExpressionRewrites) ~30       │
│  C: GeneticOptimizerGenerator (full GA, 240 sim) — tier-routed │
├─ Layer 1: Shared primitives (built in A, untouched B/C) ───────┤
│  - select_near_gate_candidates(delay, limit, exclude_hashes)   │
│  - OptimizationRun (DDL below)                                 │
│  - SimBudget counter (per-cycle/per-day; A logs even if uncapped) │
│  - SelfCorrCache (computed for ALL winners A→C, consumed B+)   │
└────────────────────────────────────────────────────────────────┘
```

**Why this works**: Layer 3's orchestrator signature is *frozen* on day 1 — A/B/C only **swap concrete implementations** behind protocols. The 5 "anti-patterns" in §7 are exactly the violations of this discipline.

---

## 4. Protocol signatures (build these in A)

```python
# backend/services/optimization/protocols.py
from typing import Protocol, List, Optional, Literal
from dataclasses import dataclass

@dataclass
class Variant:
    expression: str
    settings: dict        # region/universe/delay/decay/neutralization/truncation
    tag: str              # human-readable: "neut=INDUSTRY" / "window=45"
    generator_name: str   # "settings_sweep" / "expression_rewrite" / "ga"
    generation: int = 0   # GA generations; settings=0

@dataclass
class VariantSimResult:
    variant: Variant
    sim_response: dict    # full BRAIN response
    sharpe: Optional[float]
    fitness: Optional[float]
    turnover: Optional[float]
    margin: Optional[float]
    brain_alpha_id: Optional[str]
    checks_passed: bool   # all BRAIN gates passed
    self_corr: Optional[float]   # computed by Simulator, cached for SubmitPolicy
    error: Optional[str] = None

class VariantGenerator(Protocol):
    name: str             # for telemetry + audit trail
    async def generate(self, alpha) -> List[Variant]: ...
    # alpha is a backend.models.Alpha row; reads expression + settings

class Simulator(Protocol):
    async def run_batch(
        self, variants: List[Variant], budget: int
    ) -> List[VariantSimResult]: ...
    # MUST update SimBudget counter even when uncapped

class WinnerSelector(Protocol):
    def pick(
        self, results: List[VariantSimResult], delay: int
    ) -> List[VariantSimResult]: ...
    # uses settings.eval_thresholds(delay) — already delay-aware after b8a9560

class Persister(Protocol):
    async def save(
        self, winners: List[VariantSimResult],
        parent_alpha_id: int, opt_run_id: int
    ) -> List[int]: ...
    # returns new local alpha PKs

class SubmitPolicy(Protocol):
    async def decide(
        self, persisted_pks: List[int]
    ) -> List[Literal["submit", "queue", "skip"]]: ...

class KnowledgeFeedback(Protocol):
    async def on_winner(self, alpha) -> None: ...   # no-op in A/B; RAG hook in C
```

---

## 5. OptimizationRun DDL (Alembic in A — non-negotiable)

```sql
CREATE TABLE optimization_runs (
    id                  SERIAL PRIMARY KEY,
    parent_alpha_id     INTEGER NOT NULL REFERENCES alphas(id),
    generator_name      VARCHAR(64) NOT NULL,            -- "settings_sweep" / "composite" / "ga"
    trigger_source      VARCHAR(32) NOT NULL,            -- "beat" / "pipeline_hook" / "manual"
    n_variants          INTEGER NOT NULL DEFAULT 0,
    n_winners           INTEGER NOT NULL DEFAULT 0,
    n_submitted         INTEGER NOT NULL DEFAULT 0,      -- by SubmitPolicy
    sim_budget_used     INTEGER NOT NULL DEFAULT 0,      -- BRAIN sims spent
    sim_budget_granted  INTEGER NOT NULL,                -- budget allocator's decision
    cycle_started_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    cycle_finished_at   TIMESTAMP,
    error               TEXT,                             -- non-null = cycle aborted
    metadata            JSONB DEFAULT '{}'::jsonb         -- generator-specific
);
CREATE INDEX ix_opt_runs_parent ON optimization_runs(parent_alpha_id);
CREATE INDEX ix_opt_runs_started ON optimization_runs(cycle_started_at DESC);

-- alphas table: add the link (winners point back at their cycle)
ALTER TABLE alphas ADD COLUMN optimization_run_id INTEGER
    REFERENCES optimization_runs(id);
CREATE INDEX ix_alphas_opt_run ON alphas(optimization_run_id) WHERE optimization_run_id IS NOT NULL;
```

**Why a table, not metadata JSONB**: conversion-rate query is the GO-gate signal —
`SELECT n_winners::float / NULLIF(n_variants, 0) FROM optimization_runs WHERE generator_name = 'settings_sweep' AND cycle_started_at > NOW() - INTERVAL '14 days'` is one statement; same query against scattered JSONB across alphas would be unreadable + uncacheable.

---

## 6. Stage specs

### Stage A — MVP (2-3 days)

**Code**:
- `backend/services/optimization/service.py` (OptimizationService class + 4-layer wiring)
- `backend/services/optimization/generators/settings_sweep.py` (the 11-variant generator I prototyped manually)
- `backend/services/optimization/simulator.py` (concurrent sim with `_acquire_sim_slot`, op_timeout 600s, budget tracking)
- `backend/services/optimization/winner_selector.py` (uses `settings.eval_thresholds(delay)`)
- `backend/services/optimization/persister.py` (writes `alphas` + `optimization_run`; computes + stores `_self_corr`)
- `backend/services/optimization/submit_policy.py` (Stage A: always returns "queue")
- `backend/tasks/optimization_tasks.py` (beat-scheduled `run_optimization_cycle`)
- Alembic migration for `optimization_runs` table + `alphas.optimization_run_id`
- Flag: `ENABLE_OPTIMIZATION_LOOP` (default OFF)
- Config: `OPT_BEAT_INTERVAL_HOURS=6`, `OPT_CANDIDATES_PER_CYCLE=10`, `OPT_DAILY_SIM_BUDGET=400` (uncapped in A but logged)
- Telemetry: `GET /ops/optimization/cycles` (last 50 cycles + conversion summary)

**Budget**: 10 candidates × 11 variants = 110 sim/cycle × 4 cycles/day = 440 sim/day (~44% of BRAIN quota; mining still gets 560).

**SubmitPolicy**: always "queue" → winner lands in `submit-backlog` page, user submits manually.

**14-day observation**:
- `n_variants_total` across all cycles
- `n_winners / n_variants` (the conversion rate)
- `n_winners_human_submitted / n_winners` (manual conversion)
- `n_winners_actually_pass_brain / n_winners_human_submitted` (gate clearance)

**GO to B**: winner conversion >20% AND ≥30 cycles run (≥330 variants tried).
**STOP**: conversion <10% — selection wall confirmed, abandon optimization.
**PARTIAL** (10-20%): hold, tweak SettingsSweepGenerator parameters before escalating.

---

### Stage B — Expression rewrites + auto-submit (3-4 days, additive)

**Code changes**:
- `backend/services/optimization/generators/expression_rewrite.py` (wraps `generate_local_rewrites` from optimization_chain.py)
- `backend/services/optimization/generators/composite.py` (chains generators)
- `backend/services/optimization/submit_policy.py` (replace "always queue" with: if `self_corr<0.7` AND all BRAIN checks pass → "submit"; else "queue")
- `backend/services/optimization/budget_allocator.py` (reads day's mining sim spend from Redis counter, allocates remaining to opt)
- Config: `OPT_AUTO_SUBMIT=true`, `OPT_AUTO_SUBMIT_SELF_CORR_MAX=0.65` (tighter than 0.7 BRAIN gate for safety margin)
- Telemetry add: `n_auto_submitted` + `n_auto_submitted_actually_landed` per cycle

**Budget**: 10 candidates × 30 variants = 300 sim/cycle × 4 = 1200/day → **exceeds quota**. Either:
- Drop to 2 cycles/day (600 sim, leaves 400 for mining) — recommended
- Reduce candidates to 5 (15 sim × 2 cycles = 300/day)
- Wait for CONSULTANT mode (80-slot, no quota issue)

**0 changes to Layer 1, 3 (orchestrator signature); Layer 2 adds, Layer 4 adds allocator. SubmitPolicy implementation swap is the only Layer 3 instance change.**

**GO to C**: auto-submit pass-rate >50% over 14d (winners that the policy auto-submitted actually landed at BRAIN) AND aggregate submit count up ≥3× pre-optimization baseline.
**STOP**: pass-rate <30% → SubmitPolicy is too liberal, tighten OR revert auto-submit, stay at B-with-queue.

---

### Stage C — Full GA + pipeline hook + RAG (1-2 weeks, additive)

**Code changes**:
- `backend/services/optimization/generators/genetic.py` (wraps `run_genetic_optimization`; tier-routed: shallow near-gate → composite, deep near-gate i.e. score>0.6 → GA)
- `backend/agents/pipeline/runner.py`: add `opt_q` (async queue) + hook in consumer's persist stage — push near-miss `SimResult` when `should_optimize` is True
- `backend/services/optimization/feedback.py` (KnowledgeFeedback impl): on winner, write `(expression, hypothesis, mutation_path, before/after_sharpe)` to `r8_patterns` table for RAG L1 retrieval
- `backend/services/optimization/submit_policy.py`: integrate `marginal_analysis` for 3-way SUBMIT/NEUTRAL/SKIP
- Config: `OPT_PIPELINE_HOOK=true`, `OPT_GA_BUDGET_PER_RUN=240`, `OPT_KNOWLEDGE_FEEDBACK=true`

**Budget**: heavy. Tiered:
- Composite-generator candidates: as Stage B (300/cycle)
- GA candidates: 240 sim × 1-2 alpha/day = 240-480/day
- Plus pipeline-hook bursts (bounded by `opt_q` maxsize)
- **CONSULTANT 80-slot required** for sustainable operation

**Pipeline hook safety**: hooked consumer pushes to `opt_q` non-blocking (drop on full); a separate `pipeline_opt_consumer` drains it through OptimizationService. Heartbeat supervisor (`2f3dd58`) still applies — `opt_q` does NOT count as progress beat (only persist + push do), so a stuck opt won't keep the parent pipeline alive falsely.

**0 changes to Layer 1; Layer 3 SubmitPolicy + KnowledgeFeedback are additive; Layer 4 gains a second trigger (pipeline-hook) alongside beat.**

---

## 7. 5 anti-patterns (A-time choices that BLOCK B/C)

| Anti-pattern | "Feels fine in A" | What it costs at B/C |
|---|---|---|
| Direct `INSERT alphas` without `optimization_run_id` | "parent_alpha_id is enough lineage" | No conversion-rate query, no dedup, no cycle telemetry → retroactive backfill + Alembic at B |
| Inline `generate_variants_*()` calls (no protocol) | "we only have 1 generator now" | B/C need rewrite of service + tests (0.5d up-front saves 2d later) |
| Skipping SimBudget counter (A is uncapped) | "no need yet" | B's allocator has no historical signal to calibrate → blind defaults → quota overrun |
| Skipping `_self_corr` computation for winners | "A doesn't auto-submit" | C's auto-submit needs cached self_corr; absent → +1 sim per winner → ~30% budget waste |
| No `on_winner(alpha)` callback in persister | "C is far away" | C's RAG hook becomes a cross-cutting change touching every persistence call site |

---

## 8. Open questions (decide before A starts)

1. **Trigger source for A**: `should_optimize` signal (already computed, semantically "this alpha would benefit") vs SQL near-gate band scan (concrete, but bypasses verdict system)?
   - Recommendation: **SQL near-gate band** (simpler + observable). `should_optimize` carries baggage from the cascade era.

2. **Optimization budget vs mining budget**: hard split (e.g. 400/600) vs dynamic (opt gets whatever mining doesn't use)?
   - Recommendation: **hard split** for A (predictable, easier to debug). Dynamic in B once allocator exists.

3. **Dedup key**: `expression_hash` only, or `(expression_hash, parent_alpha_id_family)` to avoid optimizing the same family chain twice?
   - Recommendation: track BOTH from day 1; A queries by `expression_hash`, B can add family-aware dedup without schema change.

4. **What to do with legacy `mining_agent._run_optimization_chain`**: keep (parallel path), delete (legacy retired), or migrate (the new OptimizationService should be its successor)?
   - Recommendation: **delete in A**. It's the legacy cascade-era hook on a dead path; leaving it is a future-confusion hazard.

5. **A's beat interval**: 6h means ~4 cycles/day. Should it be tied to BRAIN's daily quota reset (00:00 UTC)?
   - Recommendation: 6h baseline, gated by `_pipeline_heartbeat_timeout()`-style backstop. Quota reset is best handled by `quota_guard_pause_at_threshold` (already exists).

---

## 9. References

- `reference_competitive_analysis_v3_2026_05_26.md` — selection-limited diagnosis (the reason for STOP gates)
- `project_marginal_submit_recommendation_2026_05_24.md` — the SUBMIT/NEUTRAL/SKIP scorecard Stage C integrates
- `project_ops_audit_r11fix_backlog_drain_2026_05_28.md` — submit-backlog page (Stage A's destination for queue policy)
- `project_depth_levers_refuted_breadth_is_answer_2026_05_25.md` — prior depth-axis investments that data refuted (caution for this depth axis)
- `project_split_producer_first_live_freeze_2026_05_28.md` — pipeline freeze diagnosis (Stage C pipeline-hook must respect heartbeat supervisor)
- `feedback_按效果选择.md` — Stage A must change BRAIN outcomes, not just observe; the 14-day gate enforces this
- `b8a9560` — delay-aware `settings.eval_thresholds(delay)` (Stage A's WinnerSelector reads this)
- Live data 15621 → 15720 (manual proof: settings sweep alone lifted sharpe 1.87→2.18 → BRAIN SUBMITTED)
