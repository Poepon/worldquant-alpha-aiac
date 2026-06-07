# Sprint 5 — R12 Decision Runbook (conditional cleanup)

> **Status**: SCAFFOLDING ONLY. Nothing in Sprint 5 executes until the
> operator runs the R12 decision at the **2026-07-04 ± 5d** decision
> point. Today (2026-05-20) the 14 Phase 4 flags ship default-OFF; there
> is NO production observation data yet. This runbook is the gated
> playbook the operator follows once the decision lands.

## Why Sprint 5 is conditional

The R12 critical-path bet (`ENABLE_LLM_ASSISTANT_MODE` — LLM as research
assistant, not expression author) needs **30 days of production obs**
before its GO/NO-GO/PARTIAL decision can be made. Per plan v5 §7:

- Sprint 1 shipped the R12 machinery (2026-06-04 estimate)
- +30d obs window → decision point **2026-07-04 ± 5d**
- Sprint 5 cleanup (this runbook) runs **2026-07-20+**, AFTER the decision

The 6 sentinel flags forced OFF when `ENABLE_LLM_ASSISTANT_MODE=True`
either retire permanently (GO), restore (NO-GO), or split per-flag
(PARTIAL).

## Step 0 — run the decision evaluator (decision-independent tooling)

```bash
python scripts/r12_decision_evaluator.py --days 30 \
    --out docs/r12_decision_$(date +%Y-%m-%d).json
```

This is shipped + tested NOW (Sprint 5 PR1). It produces:
- **main_decision**: GO / NO-GO / PARTIAL / INSUFFICIENT on the
  LLM-mode PASS-rate diff (bootstrap effect-size CI, reuses A1.4
  `llm_mode_comparison.evaluate_go_gate`)
- **per_sentinel**: 6 counterfactual margins (PASS rate among
  sentinel-stamped alphas vs baseline) → per-flag RESTORE / DEPRECATE
  recommendation
- **sprint5_route**: GO / NO-GO / PARTIAL
- **retire_candidates / restore_candidates** lists

The 6 sentinel stamp keys (confirmed in `evaluation.py`):

| Flag | Stamp key |
|------|-----------|
| ENABLE_R1B_HYPOTHESIS_MUTATE | `_r1b_mutation_triggered` |
| ENABLE_G5_CROSSOVER | `_g5_crossover` |
| ENABLE_HYPOTHESIS_FOREST_REUSE | `_hypothesis_forest_reference` |
| ENABLE_R8_L0 | `_r8_l0_on` |
| ENABLE_AST_ORIGINALITY_GATE | `_g3_ast_originality_blocked` |
| ENABLE_SIMULATION_CACHE | `_simulation_cache_hit` |

## Route GO — assistant mode wins (~9 人日)

`main_decision == "GO"`: assistant mode beats author with statistical
significance. The 6 author-mode sentinel mechanisms are permanently
retired + the G3 shadow code retires (B4.2).

### B4.2 — retire G3 shadow (3 人日)
- `backend/alpha_originality.py` (427 lines) → delete or `@deprecated`
- `frontend/.../G3OriginalityMonitor.jsx` (409 lines) → remove + route
- `scripts/calibrate_g3_threshold.py` (261 lines) → retire
- 3 test files rewrite (`test_g3_alpha_originality` / `_wiring` / `_ops`)
- `/ops/g3/originality-stats` endpoint retire or redirect to G3-v2
- `ENABLE_AST_ORIGINALITY_GATE` removed from SUPPORTED_FLAGS
- G3-v2 (`ENABLE_GRAMMAR_VALIDATOR`, shipped Sprint 4 B4.1) becomes the
  sole originality/syntax path

### 6 sentinel permanent cleanup (6 人日)
For each sentinel in `retire_candidates`:
- Tier 1 → Tier 3 per `docs/flag_lifecycle.md`: delete from
  SUPPORTED_FLAGS + config field + the `if settings.X:` guard
- `scripts/cleanup_orphan_flag_overrides.py --apply` for DB rows
- update `docs/flag_lifecycle.md` "Recent retirements" + canary SOP §1

## Route NO-GO — author mode wins (~1 人日)

`main_decision == "NO-GO"`: assistant mode underperforms. Cancel B4.2,
keep G3 shadow, restore the 6 sentinels.

### Restore + verify
```python
# 1. Restore the sentinel cascade (already shipped Sprint 1 A1.2)
result = await ff_service.restore_sentinel("ENABLE_LLM_ASSISTANT_MODE")

# 2. Verify completeness (Sprint 5 PR2)
report = await ff_service.verify_sentinel_restore("ENABLE_LLM_ASSISTANT_MODE")
assert report["complete"], report["warnings"]
#   complete == (dangling_cascade_rows == 0 AND restore_rows > 0)
```

`verify_sentinel_restore` checks:
1. zero `sentinel_set` audit rows with `restored_at IS NULL`
2. ≥1 `sentinel_restore` audit row exists
3. per-flag current override state report

### Lessons-learned memo (~0.5 人日)
Write `docs/r12_no_go_retrospective_<date>.md`: why assistant mode
underperformed (cost? PASS rate? sharpe distribution?), what the
per-sentinel margins showed, what we'd change before re-attempting.

### Unfreeze
The Sprint 1-4 freeze on the 6 sentinel mechanisms lifts — they resume
normal Tier 1 → Tier 2 promotion per `docs/flag_lifecycle.md`.

## Route PARTIAL — mixed / inconclusive (3-6 人日)

`main_decision in ("PARTIAL", "INSUFFICIENT")`: the headline diff is
inconclusive, but per-sentinel margins may still be decisive.

- For each flag in `retire_candidates` (margin ≤ floor): retire per the
  GO route's per-sentinel cleanup
- For each flag in `restore_candidates` (margin > floor): restore per the
  NO-GO route + `verify_sentinel_restore`
- For each flag in `insufficient_sentinels`: extend obs another window,
  re-run the evaluator
- G3-v2 + G3 shadow coexist until G3's own margin is decisive
- update `docs/flag_lifecycle.md` per-flag

## Guardrails (all routes)

- **Run the evaluator on staging-mirror first** if production data is
  ambiguous — the bootstrap CI is sensitive to small assistant pools.
- **Never retire a flag whose `insufficient_sentinels` verdict held** —
  absence of evidence ≠ evidence of absence; extend obs.
- **Audit trail**: every retire/restore writes a `feature_flag_audit`
  row. The decision JSON (`docs/r12_decision_<date>.json`) is the
  forensic record of what the data said at decision time.
