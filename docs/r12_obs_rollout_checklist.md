# R12 Obs Rollout Checklist (Step -1, before the decision evaluator)

> **Purpose**: the concrete operator playbook to get from "14 Phase 4
> flags default-OFF" → "enough production obs data that
> `scripts/r12_decision_evaluator.py` can produce a real GO/NO-GO/PARTIAL
> at 2026-07-04 ± 5d." This is the step BEFORE `docs/sprint5_r12_decision_
> runbook.md`'s Step 0.
>
> **Status**: operator-driven. Nothing here is automated — each flip is a
> deliberate FeatureFlagOverride change watched against a telemetry gate.

## The hard constraint that shapes everything: resolve_mode is global-gated

`backend/services/llm_mode_service.resolve_mode` Layer 1:

```
if not settings.ENABLE_LLM_ASSISTANT_MODE:   # global kill switch
    return MODE_AUTHOR                        # ignores task.config entirely
```

So `task.config["llm_mode"]="assistant"` does **nothing** while the
global flag is OFF. And flipping the global flag ON fires the sentinel
cascade (`feature_flag_service.set` forces the 6 sentinel flags OFF
system-wide). Therefore you **cannot** run a clean author cohort and a
clean assistant cohort simultaneously — the obs must be **temporal**:

- **Author baseline**: global OFF → all tasks author mode WITH sentinels
  active → accumulates (a) author PASS baseline + (b) the 6 per-sentinel
  stamps the counterfactual needs.
- **Assistant window**: global ON → sentinel cascade fires (6 OFF) →
  tasks with `config["llm_mode"]="assistant"` run assistant; the rest
  default to author-WITHOUT-sentinels.

The evaluator's headline metric (assistant-vs-author PASS diff) reads
`metrics["llm_mode_used"]`; the per-sentinel counterfactual reads the 6
stamps (which only accrue while sentinels are ON → the author baseline
window). Both windows feed the same 30d evaluation.

## Phase A — author baseline + secondary-feature obs (≈ days 0-14)

Global `ENABLE_LLM_ASSISTANT_MODE` stays **OFF**. Flip the secondary
Phase 4 flags ON one at a time, each watched against its gate before
moving to the next. This both (a) builds the author baseline and (b)
exercises the Sprint 2-4 features so their telemetry has data.

| Order | Flag | Watch | Gate to proceed |
|-------|------|-------|-----------------|
| 1 | `ENABLE_CAPACITY_SCORE` | `/ops/r11/capacity-stats` | total_with_capacity > 0 + not saturated in one bucket |
| 2 | `ENABLE_COGNITIVE_LAYER_PROMPT` (mode=round_robin) | `/ops/r8-v3/cognitive-layer-stats` | all 7 layers fired ≥ once over a few rounds |
| 3 | `ENABLE_FACTOR_LENS` (mode=shadow) | `/ops/r13/factor-residuals` + `/ops/r13/snapshot-stale-check` | **prerequisite: factor parquet built** (see below); ≥30 alpha residuals stamped |
| 4 | `ENABLE_GRAMMAR_VALIDATOR` | `/ops/g3v2/parse-stats` + worker log `[G3-v2 drop rate]` | drop rate < 5% (else widen grammar / keep OFF) |
| 5 | `ENABLE_G10_LOGIC_DISTILL` | `/ops/g10/logic-library` | ≥1 successful Sunday cron run, cost < cap |
| 6 | `ENABLE_FAMILY_HARD_BAN` | alpha.metrics `_r10v2_hard_banned` count | only flip after running `calibrate_r10_pairwise_corr.py` to set τ |

**Prerequisite for #3 (R13):** the factor snapshot must exist first —
operator runs `scripts/build_factor_returns_snapshot.py` per region
(Fama-French / AQR CSV → parquet). Until then `/ops/r13/snapshot-stale-
check` shows all-stale and R13 soft-skips every alpha.

**Skip / defer signals:** if a gate fails (e.g. G3-v2 drop rate high,
R10-v2 τ uncalibrated), leave that flag OFF — it is NOT required for the
R12 decision (only `ENABLE_LLM_ASSISTANT_MODE` + the 6 sentinels are).

## Phase B — assistant window (≈ days 14-30)

1. **Snapshot author baseline** (optional): note current author PASS rate
   from `/ops/llm-mode/comparison` (or query alphas where
   `metrics->>'llm_mode_used' = 'author'`).
2. **Flip the global**: `ENABLE_LLM_ASSISTANT_MODE = True` via
   FeatureFlagOverride. This auto-cascades the 6 sentinels OFF and writes
   `feature_flag_audit` rows with `sentinel_trigger_for`. Confirm via
   `/ops/feature-flags` audit (include_sentinel=true).
3. **Designate the assistant cohort**: set `task.config["llm_mode"] =
   "assistant"` on the gray-rollout tasks (POST /api/v1/tasks or edit).
   Keep a comparable set on default (author-without-sentinels). Aim for a
   balanced split so the bootstrap CI has samples in each arm.
4. **Watch** `/ops/llm-mode/comparison`: assistant arm should reach ≥ a
   few dozen alphas before the decision (the evaluator reports
   INSUFFICIENT below that).

**Incident kill-switch**: flip `ENABLE_LLM_ASSISTANT_MODE` OFF anytime →
`resolve_mode` instantly reverts every in-flight task to author. To also
restore the 6 sentinels: `restore_sentinel()` +
`verify_sentinel_restore()` (Sprint 5 PR2).

## Phase C — decision (2026-07-04 ± 5d)

Hand off to `docs/sprint5_r12_decision_runbook.md`:

```
python scripts/r12_decision_evaluator.py --days 30 \
    --out docs/r12_decision_$(date +%Y-%m-%d).json
```

Read `sprint5_route` (GO / NO-GO / PARTIAL) + per-sentinel
retire/restore_candidates → execute the matching runbook route.

## Pre-flight checklist (do these before Phase A)

- [ ] `alembic upgrade head` on production (Phase 4 added migrations
      through `p7e9c3b1d8a2`)
- [ ] `scripts/build_factor_returns_snapshot.py` run for ≥ USA + CHN
      (R13 prerequisite)
- [ ] `scripts/calibrate_r10_pairwise_corr.py` run per region → set
      `FAMILY_BAN_MIN_PAIRWISE_CORR` (R10-v2 prerequisite)
- [ ] ops console reachable + `OPS_API_TOKEN` set (telemetry gates)
- [ ] confirm the 6 sentinel stamps are firing in author baseline
      (`scripts/r12_decision_evaluator.py --days 1` dry-look: per_sentinel
      stamped_n > 0)

## Why this order

Author-baseline-first means the per-sentinel counterfactual gets its data
while sentinels are ON (Phase A); the assistant window (Phase B) then
isolates the headline assistant-vs-author comparison. Secondary features
(R11/R8-v3/R13/G3-v2/G10) are flipped during Phase A so their 30d
promotion obs (per `docs/flag_lifecycle.md` Tier 1→2) overlaps the R12
window — no wasted calendar time.
