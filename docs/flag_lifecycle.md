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

## Recent retirements

- **2026-05-19 batch**:
  - **Tier 1 → Tier 3** — `ENABLE_HIERARCHICAL_RAG_CACHE`, `ENABLE_R5_L2_RANKING` (subsumed into `ENABLE_HIERARCHICAL_RAG`); `ENABLE_REGIME_INFERENCE`, `ENABLE_REGIME_AWARE_THRESHOLDS`, `ENABLE_STYLE_PRESET_GUIDANCE` (consolidated into `ENABLE_REGIME` + `REGIME_STAGE` str enum, with the 3 legacy names kept as read-only `@property` derivations for caller compatibility).
  - **Tier 2 → Tier 3** — `ENABLE_T1_SIGN_FLIP_RETRY`, `ENABLE_SMART_SIM_SETTINGS`, `ENABLE_PRE_SIMULATE_FILTER`, `ENABLE_LLM_THESIS_SCORE_ON_TRIGGER` (`ENABLE_LLM_THESIS_SCORE_ON_PROMOTED` was a dead flag with no reader, deleted in the same batch).
  - **Group rename + dependency annotations** — R1b CoSTEER 5 sub-flags grouped under `R1b-CoSTEER` with stage 1/5–5/5 + deps in description. Flat-mode 3 sub-flags split: `ENABLE_LLM_MUTATE_ALPHA` moved to `Mining-Strategy` (not flat-specific); `ENABLE_FLAT_CONTINUOUS` + `ENABLE_DEFAULT_FLAT_SESSION` grouped under `Flat-Mode`.
- **2026-05-18** — `ENABLE_CASCADE_LEGACY` retired in phase15-D PR3c (cascade dispatch + router + watchdog probe now refuse unconditionally).
