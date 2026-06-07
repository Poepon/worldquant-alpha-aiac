# Feature Flag Lifecycle

How AIAC feature flags move through three tiers, and when to retire them.

> Source of truth: `backend/services/feature_flag_service.py::SUPPORTED_FLAGS` for tier-1 entries; `backend/config.py` `Settings` class for tier-2.

## The three tiers

### Tier 1 — `SUPPORTED_FLAGS` (operator-flippable)
Listed in `SUPPORTED_FLAGS`, surfaced in the ops UI (`/api/v1/ops/flags`), DB-overridable via `FeatureFlagOverride` rows. Used while a feature is in Phase A observation / Phase B calibration. Default `False` for new features; `True` only for safety kill-switches the operator may want to disable in an emergency.

### Tier 2 — `Settings` field, not in `SUPPORTED_FLAGS`
Settings-only kill-switch. Cannot be flipped from the ops UI. Read by code via `settings.X` or `getattr(settings, "X", default)`. Default `True` once promoted. Examples: `ENABLE_PER_NODE_THINKING_EFFORT`.

### Tier 3 — Retired (no flag at all)
Code unconditionally on the enabled path. Setting field deleted from `Settings`. Any leftover `FeatureFlagOverride` row is silently no-op'd by `load_overrides_into_cache`. Clean those up with `scripts/cleanup_orphan_flag_overrides.py --apply`.

## Promotion rules

A flag advances tier when it meets a hard criterion — not when "it feels stable."

### Tier 1 → Tier 2

Promote when **all of**:

1. **≥ 14 days production-ON, zero rollback**. Operator has not flipped OFF in the canary observation window. Verify by querying `feature_flag_audit` (or whatever the audit table is) for any `set OFF` action on the flag in the last 14d — must be empty.
2. **Phase A telemetry GO gate PASS**. If the feature has a per-flag dashboard (e.g. R1a `/ops/r1a/telemetry`, R1b `/ops/r1b/telemetry`, R8 `/ops/r8/kb-shape`, G2 `/ops/cost/telemetry`), the GO-gate counters all in the green range per the originating plan's §10.
3. **No open production-impact issues** tagged against the flag.

When all three hold, the next maintenance window:
- Change the `Settings` field default from `False` to `True`.
- Delete the entry from `SUPPORTED_FLAGS`.
- Replace the entry with a comment block recording the retirement date + reason.
- Run `scripts/cleanup_orphan_flag_overrides.py --apply` to delete the now-orphan DB rows.

### Tier 2 → Tier 3

Promote when **all of**:

1. **Default `True` ≥ 30 days**, zero environment override observed in `.env` or `.env.example`.
2. **No `getattr(settings, "X", default_other_than_True)` reader exists** in the codebase. (i.e. all callers trust the on-path behavior.)
3. **Removing the `if settings.X:` guard simplifies the code** (otherwise the kill-switch is paying for its complexity).

When all three hold:
- Delete the field from `Settings`.
- Delete the `if settings.X:` guards and any `else` legacy branch.
- Update affected docstrings / module-level comments.

## Why three tiers (not two)

Going Tier 1 → Tier 3 directly is tempting but loses the safety net of an env-level kill-switch. A Tier 2 stop ensures a single `.env` line can disable runaway costs in an emergency, even after the ops UI entry is gone. Once a feature is mature *and* has no plausible rollback scenario, the Tier 2 guard becomes pure dead code and is removed at Tier 3.

## Audit query — list candidates for promotion

For Tier 1 → Tier 2 candidates (need DB access):

```sql
-- Flags currently ON in production override AND no OFF action in 14d:
SELECT
  o.flag_name,
  o.flag_value,
  o.updated_at AS on_since,
  EXTRACT(epoch FROM (now() - o.updated_at)) / 86400.0 AS days_on
FROM feature_flag_overrides o
WHERE o.flag_value = 'true'
  AND NOT EXISTS (
    SELECT 1 FROM feature_flag_audit a
    WHERE a.flag_name = o.flag_name
      AND a.flag_value = 'false'
      AND a.created_at > now() - interval '14 days'
  )
ORDER BY days_on DESC;
```

Anything with `days_on ≥ 14` is a candidate. Cross-check each row against the originating plan's GO gate before promoting.

## Phase 4 flag inventory (added 2026-05-20)

14 new ENABLE_* flags + tuning sub-knobs shipped across Sprints 0-4.
All default OFF (Tier 1). Promotion paths summarized below; details
in `docs/phase4_a_b_plan_v5_2026-05-19.md`.

### Sprint 0 (kill switches, default ON exception)
- `ENABLE_LLM_API_CIRCUIT` — default **ON** (kill-switch class); flip OFF only if circuit breaker misbehaves
- `ENABLE_R8_L0` — sub-flag of `ENABLE_HIERARCHICAL_RAG`; default OFF currently, promote to Tier 2 once L0 exact-hit rate ≥ 5% per `/ops/r8/query-stats`

### Sprint 1 (R12 critical + P0 risks)
- `ENABLE_LLM_ASSISTANT_MODE` — pending R12 decision **2026-07-04 ± 5d**; if GO, Tier 2 promotion + 6 sentinel cleanup in Sprint 5
- `ENABLE_TASK_STOP_LOSS` + `TASK_STOP_LOSS_*` tuning — Tier 1→Tier 2 after ≥14d production-ON with zero false-trigger
- `FLAT_CROSS_REGION_QUOTA` + `FLAT_CROSS_REGION_ENFORCE` — `ENFORCE` flag is the Tier 1→2 gate (default `False` warn-only → flip `True` real reject)

### Sprint 2 (eval + risk)
- `ENABLE_CAPACITY_SCORE` + `CAPACITY_SCORE_WEIGHT` — Tier 1→Tier 2 after `/ops/r11/capacity-stats` 7d obs + composite-score distribution shift acceptable
- `ENABLE_FAMILY_HARD_BAN` + `FAMILY_BAN_MIN_PAIRWISE_CORR` — ⚠️ **DOA pending Sprint 5 upstream wire** (no `state.r10v2_pnl_corr_matrix` producer yet, per Sprint 2 F9 review)
- `ENABLE_FACTOR_LENS` + `FACTOR_LENS_MODE` (`shadow`→`soft`→`hard` rollout) + `FACTOR_LENS_RESIDUAL_SHARPE_MIN` — three-stage rollout per `/ops/r13/factor-residuals` 7d obs

### Sprint 3 (SOTA Part 1)
- `ENABLE_COGNITIVE_LAYER_PROMPT` + `COGNITIVE_LAYER_SELECT_MODE` (`round_robin`→`bandit`→`deficit_aware`) + `COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET` — promote to `bandit` after ≥7d round_robin seeding via `/ops/r8-v3/cognitive-layer-stats`
- `ENABLE_G10_LOGIC_DISTILL` + `LOGIC_DISTILL_MAX_COST_USD_PER_WEEK` + 4 tuning sub-knobs — Tier 1→Tier 2 after weekly cron has ≥4 successful runs without cost overrun

### Sprint 4 (SOTA Part 2)
- `ENABLE_G10_LOGIC_INJECT` + `G10_LOGIC_INJECT_TOP_K` — Tier 1→Tier 2 after `/ops/g10/logic-library` shows ≥20 active entries + hypothesis prompt round-latency ≤ +500ms acceptable
- `ENABLE_GRAMMAR_VALIDATOR` + `GRAMMAR_VALIDATOR_RETRY_MAX` — Tier 1→Tier 2 after parse-fail rate ≤ 2% on production round samples (telemetry endpoint deferred)

### 6 R12 sentinel flags (status pending R12 decision)

Following ENABLE_* are forced OFF when `ENABLE_LLM_ASSISTANT_MODE=True`. The R12 decision determines whether they retire permanently (GO), restore (NO-GO), or partial (per counterfactual SQL):

| Flag | Group | Sentinel reason |
|------|-------|-----------------|
| `ENABLE_R1B_HYPOTHESIS_MUTATE` | R1b-CoSTEER | author-mode mutation |
| `ENABLE_G5_CROSSOVER` | G5-Crossover | author-mode trajectory |
| `ENABLE_HYPOTHESIS_FOREST_REUSE` | G8-Forest | author-mode reuse |
| `ENABLE_R8_L0` | Phase3-R8 (sub-flag) | author-mode L0 hits |
| `ENABLE_AST_ORIGINALITY_GATE` | G3-Originality | ⚠️ @deprecated_pending_r12_decision (Sprint 4 B4.1 ships G3-v2 successor) |
| `ENABLE_SIMULATION_CACHE` | Phase3-R9 | author-mode cache hits |

Sprint 5 B4.2 retires `ENABLE_AST_ORIGINALITY_GATE` (G3 shadow code) conditionally per R12 decision.

## Recent retirements

- **2026-05-19 batch**:
  - **Tier 1 → Tier 3** — `ENABLE_HIERARCHICAL_RAG_CACHE`, `ENABLE_R5_L2_RANKING` (subsumed into `ENABLE_HIERARCHICAL_RAG`); `ENABLE_REGIME_INFERENCE`, `ENABLE_REGIME_AWARE_THRESHOLDS`, `ENABLE_STYLE_PRESET_GUIDANCE` (consolidated into `ENABLE_REGIME` + `REGIME_STAGE` str enum, with the 3 legacy names kept as read-only `@property` derivations for caller compatibility).
  - **Tier 2 → Tier 3** — `ENABLE_T1_SIGN_FLIP_RETRY`, `ENABLE_SMART_SIM_SETTINGS`, `ENABLE_PRE_SIMULATE_FILTER`, `ENABLE_LLM_THESIS_SCORE_ON_TRIGGER` (`ENABLE_LLM_THESIS_SCORE_ON_PROMOTED` was a dead flag with no reader, deleted in the same batch).
  - **Group rename + dependency annotations** — R1b CoSTEER 5 sub-flags grouped under `R1b-CoSTEER` with stage 1/5–5/5 + deps in description. Flat-mode 3 sub-flags split: `ENABLE_LLM_MUTATE_ALPHA` moved to `Mining-Strategy` (not flat-specific); `ENABLE_FLAT_CONTINUOUS` + `ENABLE_DEFAULT_FLAT_SESSION` grouped under `Flat-Mode`.
- **2026-05-18** — `ENABLE_CASCADE_LEGACY` retired in phase15-D PR3c (cascade dispatch + router + watchdog probe now refuse unconditionally).
