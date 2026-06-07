# Production Canary SOP — 20 ENABLE_* flags fully ON

> **Date**: 2026-05-18 (post master plan v1.3 100% ship)
> **Scope**: 24h structured monitoring window after the full set of 20 production flag overrides is ON in `feature_flag_overrides`. Defines what to look at, at what cadence, with which thresholds, and what to do if a red flag fires.
> **Default disposition**: **observe, do NOT preemptively roll back.** Per `feedback_no_reflex_flag_cleanup` — flags are designed to live ON; rollback only on concrete red-flag thresholds below.

---

## §1 Flag inventory + per-flag observable endpoint

20 overrides live (`SELECT * FROM feature_flag_overrides`), grouped by subsystem:

| # | Flag | Subsystem | Primary observability | Code-path nice-to-have |
|---|---|---|---|---|
| 1 | `ENABLE_R1A_HOOK` | CoSTEER attribution | `GET /api/v1/ops/r1a/telemetry` | `r1a_attribution_log` row count |
| 2 | `ENABLE_DAG_TRACE` | LangGraph DAG persistence | task detail trace_steps + `experiment_runs.runtime_state['dag']` | unit `test_dag_trace.py` |
| 3 | `ENABLE_DIRECTION_BANDIT` | UCB1 dataset selector | `runs/{run_id}` bandit pulls + `metrics` reward | `test_bandit_selector.py` |
| 4 | `ENABLE_DUAL_CHANNEL_RAG` | RAG R4' dual-channel | `node_rag_query` trace_step input/output | `dual_channel_rag.py` |
| 5 | `ENABLE_FAMILY_CAP` | R10 family de-dup cap | `GET /api/v1/ops/r1a/telemetry` family_cap_blocks col | `family_classifier.py` |
| 6 | `ENABLE_FLAT_CONTINUOUS` | flat-F1 hypothesis-driven session | `POST /ops/start-flat-session` started | task detail `mining_mode='FLAT_CONTINUOUS'` |
| 7 | `ENABLE_GRADED_SCORE` | composite scoring multi-grade | `alpha_scoring._composite_score` | `test_alpha_scoring.py` |
| 8 | `ENABLE_HIERARCHICAL_RAG` | R8 4-layer RAG | `GET /api/v1/ops/r8/query-stats` + `/r8/kb-shape` | `hierarchical_rag.py` `_layer_call` |
| 9 | `ENABLE_LLM_JUDGE` | R5 LLM composite re-rank | `llm_judge_log` rows + `judge_cost_usd` | `r5_judge_service.py` |
| 10 | `ENABLE_MACRO_NARRATIVE_GUIDANCE` | macro context injection | `GET /api/v1/ops/macro/token-budget` | `macro_narratives/` |
| 11 | `ENABLE_NEGATIVE_KNOWLEDGE_NUDGE` | failure pattern nudge | `GET /api/v1/ops/negative-knowledge/top` | `node_generate` prompt grep |
| 12 | `ENABLE_PILLAR_AWARE_SELECTION` | pillar balance bias | `GET /api/v1/ops/pillar/latest` | `pillar_balance/` |
| 13 | `ENABLE_REGIME_AWARE_THRESHOLDS` | regime-adjusted Sharpe gates | `GET /api/v1/ops/regime/current` | `regime_state/` |
| 14 | `ENABLE_REGIME_INFERENCE` | regime classifier | `GET /api/v1/ops/regime/snapshot` | `regime_classifier.py` |
| 15 | `ENABLE_ROBUSTNESS_CHECK` | hold-out / sub-period robustness | `alphas.metrics['_robustness_score']` non-null count | `node_evaluate` |
| 16 | `ENABLE_SIGNAL_CONTROL_DUAL_RUN` | signal vs control parallel sim | `alphas.metrics['_control_sharpe']` non-null count | `_evaluate_single_alpha` |
| 17 | `ENABLE_SIMULATION_CACHE` | R9 BRAIN sim dedupe cache | `GET /api/v1/ops/r8/query-stats` (shared cache log) | `simulation_cache_service.py` |
| 18 | `ENABLE_STYLE_PRESET_GUIDANCE` | style preset injection | trace_step `style_preset` field | `node_hypothesize` |
| 19 | `ENABLE_TASK_SCHEMA_V2` | Phase 1.5-C schema cutover | `task.schedule`/`task.starting_tier` non-null on new tasks | `routers/tasks.py` |
| 20 | `ENABLE_AST_DIVERSITY_DIM` | AST-based diversity dim | `diversity_tracker._ast_features` non-empty | `diversity_tracker.py` |

R1b CoSTEER retry loop is gated by `ENABLE_R1A_HOOK` (downstream) — same observability covers both.

### Phase 4 additions (2026-05-20, Sprints 0-4)

14 new ENABLE_* flags, all default OFF. Operator flips ON via FeatureFlagOverride one-at-a-time across the 30d Sprint 1-5 observation window. R12 decision date 2026-07-04 ± 5d determines sentinel-flag fate (see flag_lifecycle.md §"6 R12 sentinel flags").

| # | Flag | Subsystem | Primary observability | Default-state |
|---|------|-----------|----------------------|---------------|
| 21 | `ENABLE_LLM_API_CIRCUIT` | Sprint 0 LLM kill-switch | LLMService.call WARNING on circuit open | **ON** (kill-switch class) |
| 22 | `ENABLE_TASK_STOP_LOSS` + 3 tuning | Sprint 1 A2 R14 | `task_stop_loss_events` rows + `/ops/task-stop-loss/recent` | OFF |
| 23 | `ENABLE_LLM_ASSISTANT_MODE` (+6 sentinel cascade) | Sprint 1 A1 R12 | `feature_flag_audit` sentinel_trigger_for + `/ops/llm-mode/comparison` | OFF · ⚠️ R12 decision 7/4 ± 5d |
| 24 | `FLAT_CROSS_REGION_ENFORCE` (+ quota dict) | Sprint 1 A3 flat-F4 | `/ops/flat-region/distribution` | warn-only (False) |
| 25 | `ENABLE_CAPACITY_SCORE` + weight | Sprint 2 B1 R11 | `/ops/r11/capacity-stats` + alpha.capacity_usd_estimate distribution | OFF |
| 26 | `ENABLE_FAMILY_HARD_BAN` + τ | Sprint 2 B3 R10-v2 | alpha.metrics['_r10v2_hard_banned'] count + 互验 SQL | OFF · ⚠️ DOA pending Sprint 5 wire |
| 27 | `ENABLE_FACTOR_LENS` + `FACTOR_LENS_MODE` shadow/soft/hard | Sprint 2 B2 R13 | `factor_lens_residuals` rows + `/ops/r13/factor-residuals` (deferred) | OFF (shadow) |
| 28 | `ENABLE_COGNITIVE_LAYER_PROMPT` + select_mode + token_budget | Sprint 3 B5 R8-v3 | `/ops/r8-v3/cognitive-layer-stats` per-layer fire+PASS count | OFF |
| 29 | `ENABLE_G10_LOGIC_DISTILL` + 5 tuning | Sprint 3 A5.1 G10 PR1 | weekly Sunday 03:00 SH cron result dict + `/ops/g10/logic-library` | OFF |
| 30 | `ENABLE_G10_LOGIC_INJECT` + top_k | Sprint 4 A5.2 G10 PR2 | alpha.metrics['_g10_injected'] / ['_g10_entries_n'] count (these candidates ARE persisted) | OFF |
| 31 | `ENABLE_GRAMMAR_VALIDATOR` + retry_max | Sprint 4 B4.1 G3-v2 | **grep worker logs for `[G3-v2 drop rate]` / `[G3-v2 dropped]`** — parse-fail candidates are DROPPED before persistence so alpha.metrics['_g3v2_parse_failed'] is unreachable; observe via the log lines + MiningState.g3v2_parse_fail_count instead (F2 review fix) | OFF |

Total production flag inventory after Sprint 4 ship: **20 (v1.3 baseline) + 14 (Phase 4 new) = 34** ENABLE_* flags. 6 sentinel flags pending R12 decision route (deprecate / restore / partial).

---

## §2 Pre-canary baseline snapshot (T-0)

Run **before** the 24h window starts. Captures the "what should look normal" baseline.

```bash
# Capture T-0 snapshot
python scripts/canary_baseline_capture.py --label T-0
#   → docs/canary_T-0_<YYYY-MM-DD>.json (9 metrics + git HEAD + ISO timestamp)

# At T+24h, capture again and diff
python scripts/canary_baseline_capture.py --label T+24h
python scripts/canary_baseline_capture.py --diff docs/canary_T-0_<YYYY-MM-DD>.json
#   → side-by-side delta table, no save
```

The script reads 9 metrics: `flag_count_on / tasks_failed_pct_7d /
alphas_passed_24h / kb_total_entries / r1a_attribution_rows_24h /
r1b_retry_rows_24h / r8_query_rows_24h / brain_sim_count_24h /
mining_tasks_running_now`. Exit 0 on success, 1 on DB error.

---

## §3 Monitoring cadence

| T+ | Action | Tool |
|---|---|---|
| **T+0** | Capture baseline (§2). Verify `/api/v1/ops/flags` returns 20 ON. | `python scripts/canary_baseline_capture.py --label T-0` |
| **T+1h** | Boot health: `uvicorn` clean, no 500 in `.uvicorn.err`, celery worker up | `tail -200 .uvicorn.err` |
| **T+1h** | R1a hook firing: `r1a_attribution_log` row count growing | re-run §2 capture script with `--label T+1h` |
| **T+6h** | Mid-window check — run all §4 red-flag queries | `python scripts/canary_redflag_check.py --t0 <T-0 ISO>` |
| **T+24h** | Full sweep — diff vs T-0 + re-run §4 | `--diff docs/canary_T-0_<date>.json` + `canary_redflag_check.py` |

Off-hours: rely on `.uvicorn.err` tail (any new exception traceback = pager). No active polling needed.

---

## §4 Red-flag thresholds + auto-action

Each subsystem has **one** red-flag SQL. If it fires, follow the action column.

| Subsystem | Red-flag query | Trigger | Action |
|---|---|---|---|
| **Boot health** | grep `Traceback\|500 Internal` in `.uvicorn.err` since T-0 | ≥1 unhandled `500` for a non-test route | Investigate, do NOT auto-flip flags |
| **R1a hook crash** | `SELECT COUNT(*) FROM r1a_attribution_log WHERE error IS NOT NULL AND created_at > T0` | > 10% of new rows have `error` | **Roll back `ENABLE_R1A_HOOK`** (§5) |
| **R1a starved** | r1a row count not growing in last 1h while mining tasks RUNNING | 0 new rows in 1h with ≥1 task RUNNING | Investigate hook wiring, NOT auto-flip |
| **R1b runaway cost** | `SELECT SUM(total_cost_usd) FROM r1b_retry_log WHERE created_at > T0` | > $5 in 24h (baseline ~$0.50/24h) | **Roll back `ENABLE_R1A_HOOK`** (R1b is downstream) |
| **R8 cache thrashing** | `SELECT cache_hit_rate FROM /ops/r8/query-stats` | < 10% after T+6h with > 50 queries | Investigate `_layer_call` Redis path, NOT auto-flip |
| **R8 elevation runaway** | `SELECT COUNT(*) FROM r8_query_log WHERE had_failure_tree_elevation = true AND created_at > T0` | > 50% of new rows | **Roll back `ENABLE_HIERARCHICAL_RAG`** if also R5 cost spikes |
| **LLM Judge cost spike** | `SELECT SUM(judge_cost_usd) FROM llm_judge_log WHERE created_at > T0` | > $10 in 24h | **Roll back `ENABLE_LLM_JUDGE`** |
| **Sim cache wrong-hit** | `alphas.metrics['_sim_cache_hit']=true` but `sharpe IS NULL` | ≥1 row | **Roll back `ENABLE_SIMULATION_CACHE`**, investigate bucket key |
| **Failed task rate** | T-7d baseline FAILED pct from §2 vs T+24h sliding window | T+24h pct > T-7d pct * 1.20 | Compare with `.uvicorn.err` + celery — if R1a/R1b traceback dominates, roll back R1a |
| **BRAIN sim 4xx burst** | grep `BRAIN.*4[0-9][0-9]` in celery log since T-0 | > 100 4xx in 1h (baseline ~5) | Investigate RAG context — could be R8 `requires_role` mismatch; do NOT auto-flip |

**All other flags** (`DAG_TRACE` / `MACRO` / `REGIME` / `STYLE_PRESET` etc) have no rollback trigger in the 24h window — they're passive enrichment, low blast radius. If their telemetry endpoint 500s, that's a code bug to fix, not a flag rollback.

---

## §5 Per-flag rollback SQL

Soft rollback (< 1 minute, no restart needed — `feature_flag_service.py` cache TTL is 30s). Hard rollback = code revert + redeploy.

```sql
-- Template (replace FLAG_NAME):
UPDATE feature_flag_overrides
   SET flag_value='false', updated_at=now(), note=note || ' | canary rollback ' || now()::text
 WHERE flag_name='FLAG_NAME';

-- Most-likely rollback targets per §4:
-- 1. R1a + downstream R1b (most blast radius):
UPDATE feature_flag_overrides SET flag_value='false', updated_at=now(),
       note=COALESCE(note,'') || ' | canary rollback ' || now()::text
 WHERE flag_name='ENABLE_R1A_HOOK';

-- 2. Hierarchical RAG (if elevation runaway):
UPDATE feature_flag_overrides SET flag_value='false', updated_at=now(),
       note=COALESCE(note,'') || ' | canary rollback ' || now()::text
 WHERE flag_name='ENABLE_HIERARCHICAL_RAG';

-- 3. LLM Judge (cost spike):
UPDATE feature_flag_overrides SET flag_value='false', updated_at=now(),
       note=COALESCE(note,'') || ' | canary rollback ' || now()::text
 WHERE flag_name='ENABLE_LLM_JUDGE';

-- 4. Simulation cache (wrong-hit suspected):
UPDATE feature_flag_overrides SET flag_value='false', updated_at=now(),
       note=COALESCE(note,'') || ' | canary rollback ' || now()::text
 WHERE flag_name='ENABLE_SIMULATION_CACHE';

-- Verify rollback took effect:
curl -s -H "X-Ops-Token: $OPS_TOKEN" http://localhost:8001/api/v1/ops/flags \
  | python -m json.tool | grep -A1 FLAG_NAME
```

**After rollback**: capture incident note in `docs/canary_rollback_<date>_<flag>.md` with: red-flag query output that triggered it, SQL run, downstream observability that confirmed the rollback worked. Do NOT delete the override row — flip `flag_value` only — so the audit history stays intact.

---

## §6 Escalation tree

```
red-flag fires
    │
    ├── boot health crashed → fix code, NEVER flip flags blindly
    │
    ├── single subsystem (R1a / R1b / R8 / Judge / Cache):
    │     → run §5 SQL for that flag only
    │     → wait 30s for cache refresh
    │     → re-run §4 query — confirm drop
    │     → investigate root cause, plan re-enable
    │
    └── multi-subsystem (FAILED pct + multiple telemetry red):
          → roll back R1A_HOOK first (highest blast radius)
          → wait 5 min, re-check
          → if still red, escalate to "stop all mining tasks"
              psql -c "UPDATE mining_tasks SET status='PAUSED' WHERE status='RUNNING';"
          → full investigation before re-enable
```

---

## §7 What does NOT trigger rollback

Per `feedback_no_reflex_flag_cleanup` — these are **not** red flags:

- A flag's telemetry endpoint returns 0 rows because no mining task ran in the window (idle ≠ broken)
- R1a `UNKNOWN` attribution > 50% — that's an attribution-classifier accuracy concern, not a hook stability concern; logged for Phase 4 reclassifier work
- Family-cap blocking 30% of candidates — that's the cap **doing its job**
- R5 LLM judge disagreeing with rule-based score — designed disagreement; tracked, not rolled back
- DAG trace JSON > 100KB per run — large but storage-cheap; index check at T+7d, not T+24h
- Cache hit rate < 50% in first 6h — cold cache, expected; re-evaluate at T+24h

These observations belong in a weekly review, not the 24h canary window.

---

## §8 Post-canary

At T+24h with all red-flag queries clean:

1. **Diff §2 snapshot T-0 vs T+24h**: alphas_passed should be in expected range (~30-100/day per region, depending on cascade vs flat schedule); BRAIN sim count should not have dropped > 50%; KB entries should not have decreased.
2. **Document outcome** in `docs/canary_outcome_<date>.md` — even if zero rollbacks, the snapshot becomes the next baseline.
3. **Memory entry**: append a `[[project_canary_<date>]]` to MEMORY.md noting the window completed cleanly + key metric deltas.
4. **No reflexive flag clean-up**: per `feedback_no_reflex_flag_cleanup`, success = continue observation, NOT close flags.
