# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AIAC 2.0 is a Human-AI collaborative alpha-mining platform built on the **Alpha-GPT** paradigm fused with **RD-Agent**'s CoSTEER feedback loop. It targets WorldQuant BRAIN, generating alpha expressions via LLM, simulating them on BRAIN, and self-evolving via a knowledge base.

Stack: Python 3.10+ FastAPI backend (async SQLAlchemy + asyncpg), Celery (Redis) for background jobs, LangGraph for agent workflows, OpenAI-compatible LLM (default model `kimi-k2.6` served via Alibaba Cloud MaaS — see *LLM routing* below), React 18 + Vite + Ant Design frontend, PostgreSQL.

## Common Commands

### Run / Stop (one-shot scripts)

```bash
# Windows
run.bat              # restart (default): kill stale procs, then start
run.bat --start      # start only (skip running ones)
run.bat --stop       # stop all
run.bat --port 8002  # custom backend port (default 8001)

# Linux / macOS
./run.sh [--start|--restart|--stop|--port NUM]
```

The script auto-creates `.env` from `.env.example` (and opens it for editing), creates the venv, installs deps, creates the DB if missing, runs Alembic, and launches Backend / Frontend / Celery worker in three windows.

### Manual dev loop

```bash
# Backend (port 8001, hot reload)
uvicorn backend.main:app --reload --port 8001

# Frontend (port 5174, proxies /api → http://localhost:8001)
cd frontend && npm run dev

# Celery worker (Windows requires --pool=solo)
celery -A backend.celery_app worker --loglevel=info --pool=solo

# Celery beat (scheduled feedback / sync jobs)
celery -A backend.celery_app beat --loglevel=info
```

API docs at `http://localhost:8001/docs`. Frontend at `http://localhost:5174`.

### Database / Migrations

```bash
# First-time DB creation (psycopg2 must reach Postgres on POSTGRES_PORT, default 5433)
python backend/migrations/init_database.py

# Alembic — always run from inside backend/
cd backend
alembic current
alembic upgrade head
alembic revision --autogenerate -m "describe change"
alembic downgrade -1
```

`backend/database.init_db()` also calls `metadata.create_all()` at FastAPI startup as a dev fallback. In prod rely on Alembic.

### Tests

```bash
# Comprehensive suite with regression baseline (preferred for CI / pre-release)
python backend/tests/test_suite.py --all
python backend/tests/test_suite.py --unit          # quick subset
python backend/tests/test_suite.py --regression    # compare against baseline.json
python backend/tests/test_suite.py --all --save-baseline  # bump baseline after intentional improvements

# Pytest-driven unit/integration tests (use the in-memory aiosqlite fixtures in backend/tests/conftest.py)
pytest backend/tests/unit -v
pytest backend/tests/integration -v
pytest backend/tests/test_optimization_modules.py -v

# Single test
pytest backend/tests/unit/test_services.py::TestAlphaService::test_get_alpha -v

# Quick smoke / seed
python backend/benchmark_test.py --quick
python backend/benchmark_test.py --seed
```

`backend/tests/baseline.json` stores the regression baseline — only update it via `--save-baseline` when you intentionally move a metric.

### Frontend lint / build

```bash
cd frontend
npm run lint     # eslint, --max-warnings 0
npm run build    # vite build → dist/
```

There is no Python linter/formatter wired into the repo; do not introduce one without asking.

## Architecture

### Layered backend (strict dependency direction)

```
routers/  →  services/  →  repositories/  →  models/  (SQLAlchemy + asyncpg)
                ↘  adapters/   (BRAIN HTTP API)
                ↘  agents/services/llm_service.py  (OpenAI-compatible)
```

Rules (codified in `backend/CODE_STATUS.md` / `backend/REFACTORING_STATUS.md`):

- **Routers MUST go through a Service.** No direct DB queries in `routers/*`. Inject the service via `Depends(get_*_service)`.
- **Services compose Repositories + Adapters.** They never construct external clients themselves — pass adapters/LLM through the constructor so tests can swap in `tests/fixtures/mock_brain.py` or `mock_llm.py`.
- **Protocols (`backend/protocols/`) are append-only.** Adding a method is fine; changing an existing signature breaks every adapter and mock.
- **Models are split by domain** under `backend/models/{alpha,task,knowledge,metadata,config,base}.py` and re-exported from `backend/models/__init__.py`. Editing models requires an Alembic revision.

The status docs flag a few routers that historically did direct DB access — when touching `routers/datasets.py`, `routers/operators.py`, `routers/knowledge.py`, `routers/config.py`, `routers/runs.py`, prefer adding/using the matching service rather than re-introducing inline queries.

### Agent / mining workflow

`backend/agents/` + `backend/pool/` are the brain. **Production architecture = four-pool decoupled pipeline** (Phase 1c, commit `b89b732` 2026-06-06 deleted the old serial/FLAT/ONESHOT path, −24k LOC). Gated by `ENABLE_POOL_PIPELINE` (.env). Strategy / dev state: `docs/DEVELOPMENT_PLAN.md` §2A.

- **Three resident worker pools** — `backend/pool/workers.py` (`hg_loop` / `s_loop` / `e_loop`) launched by a Popen-respawn `pool/supervisor.py` via `pool/run_worker.py` (all gated on `ENABLE_POOL_PIPELINE`; `run.bat` starts the supervisor). Work flows through **persistent DB queues**: scheduler beat → `hyp_intent` → **HG** (RAG + hypothesis + code_gen + validate) → `candidate_queue` → **S** (BRAIN simulate) → **E** (evaluate + persist) → `alphas`. Each worker gets its OWN `AsyncSessionLocal` (no two coroutines share a session); claim/lease via two-txn `pool/queue.py` (claim COMMITs before work); heartbeat-lease + `recycle_expired` beat is the single recovery path (no task-level watchdog revive).
- The pools **reuse the existing node implementations**: `agents/graph/` (`MiningWorkflow` + `graph/nodes/{generation,validation,evaluation,persistence}.py`, state in `state.py`) and `agents/pipeline/` (e.g. `persister.py` = the E-stage KB write). So the per-alpha trace still follows `TraceStepType` (`models/base.py`): `RAG_QUERY → HYPOTHESIS → CODE_GEN → VALIDATE → SIMULATE → SELF_CORRECT? → EVALUATE`, now split across HG(gen) / S(sim) / E(eval).
- **Scheduler** (`pool/scheduler.py:schedule_round`, beat every 5 min): `weighted_choice` over `dataset_cell_stats.mining_weight` (the dataset bandit, `ENABLE_DATASET_VALUE_BANDIT`) + `pg_advisory_lock` → inserts `hyp_intent`. **Control plane** (Redis): `pool/drain.py` (`pool:{hg,s,e}:drain` soft-stop — does NOT gate the scheduler; stop inflow via flag) + `pool/budget.py` (`budget:sims:DATE` + token ceiling `POOL_TOKEN_BUDGET_PER_DAY`). Endpoints `POST /ops/pools/{name}/drain|resume`, `GET /ops/pool-*`.
- Prompts: `backend/agents/prompts/` (`prompts.yaml` via `loader.py` + `registry.py`); `agents/prompts.py` shim re-exports. EVAL band SSOT = `_eval_thresholds()` in `agents/graph/nodes/evaluation.py` (flat `EVAL_*` in `config.py`).
- KB/RAG (per-alpha): read = `agents/hierarchical_rag.py` (HG stage); write = pool E-stage `agents/pipeline/persister.py` (SUCCESS_PATTERN + hypotheses, Phase 1 four-track). `r1a_attribution_log` write retired with the FLAT path.

**Retired in `b89b732` (2026-06-06 — do NOT look for these; this file's pre-06-06 version described them as live):** `tasks/mining_tasks.py` / `run_mining_task` / `_run_flat_iteration` / `run_flat_pipeline_session`; FLAT / ONESHOT / CASCADE schedules + `POST /api/v1/tasks` + `/ops/start-flat-session` + `/ops/flat-sessions/*` endpoints; `tasks/orchestrator.py` (`ENABLE_AUTO_ORCHESTRATOR`); `mining_agent.py` / `strategy_agent.py` / `evolution_strategy.py` / `field_screener.py` / `feedback_r1b.py` / `feedback_g5.py` / `genetic_optimizer.py`; `agents/core/` source (only `__pycache__` remains; `AttributionType` lives in `agents/attribution_types.py`); `rag_service.py`; `experiment_runs` table. DB still has 71 historical FLAT/ONESHOT `MiningTask` rows (query-only, can't restart). `feedback_agent.py` survives (consultant/sync path). **Phase 2 (async cognitive reconcile, `ENABLE_POOL_COGNITIVE_RECONCILE`) NOT yet activated** — feedback is the pool E-stage synchronous write, not yet an async reconcile beat.

### Standalone analytics modules

These are pure-function modules orchestrated by services/agents — keep them dependency-free and unit-testable:

| File | Purpose |
|------|---------|
| `alpha_scoring.py` | Composite score + adaptive thresholds |
| `alpha_semantic_validator.py` | Operator-aware syntax / semantics check (loads operator registry from DB at startup) |
| `dataset_selector.py` | Bandit-style dataset picker (`BANDIT_*` settings); note the live pool path picks via `pool/scheduler.py` weighted_choice over `dataset_cell_stats.mining_weight` |
| `diversity_tracker.py` | Fingerprint dedup + novelty score |
| `external_knowledge.py` | Forum + 101-Alphas pattern import |
| `metrics_tracker.py` | Session/Round/Alpha metrics, writes `.cursor/debug.log` |
| `experiment_tracker.py`, `multi_fidelity_eval.py`, `optimization_chain.py`, `selection_strategy.py`, `knowledge_extraction.py` | Supporting evolution / scheduling logic |

### Optimization closure layer (`backend/services/optimization/`)

A separate post-mining loop that takes an already-mined near-gate alpha and tries to push it over the BRAIN submission bar by sweeping simulation settings (neutralization / truncation / decay …). It is **layered with frozen seams** so Stages A→C can swap implementations without touching the orchestrator:

- **Layer 3** `service.py:OptimizationService.run_one_cycle(parent_alpha, trigger_source, budget)` — the frozen entry point. Composes injected protocols (`protocols.py`): `VariantGenerator` (`generators/settings_sweep.py`) → `Simulator` (`simulator.py`) → `WinnerSelector` (`winner_selector.py`) → `RobustnessFilter` (`robustness.py`, gated by `OPT_ROBUSTNESS_FILTER` — guards against multiple-testing / lone-peak overfit) → `Persister` (`persister.py`) → `SubmitPolicy` (`submit_policy.py`). Build via `factory.py`.
- **Layer 4** triggers: the 6h beat `tasks/optimization_tasks.py:run_optimization_cycle` (gated by `ENABLE_OPTIMIZATION_LOOP`, default OFF, `OPT_*` budgets) scans near-gate alphas automatically; the **manual "以 alpha 为蓝本" trigger** (`POST /ops/optimization/optimize-alpha`, `trigger_source="manual"`, Redis NX lock) runs **independently of the loop flag**. Neither auto-submits — Stage A `SubmitPolicy` returns `"queue"` only; winners land in the submit-backlog for human review.

### Marginal value & submit-backlog (`backend/marginal_*.py`)

The platform ranks/routes submission candidates by an **offline marginal ΔSharpe** (`marginal_drain.py` / `marginal_analysis.py`) against a local OS-backtest pool — see the multi-dimensional scorecard in `marginal_analysis.py` (NOT Sharpe-led: an alpha's **absolute** margin in bps is an economic gate, ≥5 bps to clear cost; Δmargin is dropped as collinear). `marginal_recon.py` is the **kill-switch validator**: it reconciles that offline proxy against BRAIN's authoritative `before-and-after-performance` for CAN_SUBMIT alphas; sign-agreement ≤60% over ≥~15 pairs ⇒ the offline ΔSharpe is not a valid proxy ⇒ stop using it to route. Surfaced via the `ops/submit-backlog` page (verdict-sorted queue).

### BRAIN integration

All HTTP traffic to `platform.worldquantbrain.com` goes through `backend/adapters/brain_adapter.py` (implementing `protocols/brain_protocol.py`). Credentials come from `BRAIN_EMAIL` / `BRAIN_PASSWORD` in `.env`, optionally overridden per-user via `WQBCredential` rows (`services/credentials_service.py`). Sync tasks (`tasks/sync_tasks.py`) populate `DatasetMetadata`, `DataField`, `Operator`, and user `Alpha` rows from BRAIN — they're scheduled by `celery_beat_schedule` in `celery_app.py` (datasets daily at 06:00, operator stats every 6h, feedback at 23:00, timezone Asia/Shanghai).

### Configuration

`backend/config.py` (Pydantic Settings) is the single source of truth. It reads `.env` and exposes the flat eval-band thresholds (`EVAL_SHARPE_MIN`, `EVAL_FITNESS_MIN`, `EVAL_TURNOVER_MIN/MAX`, `EVAL_SUBUNIV_MIN`, `EVAL_SELF_CORR_MAX`, `EVAL_SCORE_PASS/OPTIMIZE`, `EVAL_PROVISIONAL_*` — consumed via `_eval_thresholds()` in `agents/graph/nodes/evaluation.py`), legacy globals (`SHARPE_MIN`, `TURNOVER_MAX`, `FITNESS_MIN`, `MAX_CORRELATION`, `SCORE_PASS_THRESHOLD` — kept as fallbacks for the Consultant role-switch path), bandit / field / diversity weights, multi-fidelity flags, and rate limits (`MAX_SIMULATIONS_PER_DAY`, `MAX_TOKENS_PER_DAY`). Add new tunables here, not as scattered constants.

`ENABLE_BRAIN_CONSULTANT_MODE` (P3-Brain, 2026-05-16) is a manual toggle flipped from the ops console (`POST /ops/brain/activate-consultant`) after the user receives a BRAIN Consultant upgrade email. Switching unlocks `effective_sharpe_submit_min` (raised to `max(SHARPE_MIN, 1.58)`), `effective_default_test_period` (`P0Y`), and `effective_region_universes` (5 regions: USA/CHN/HKG/JPN/EUR). Task启动时冻结快照到 `MiningTask.config["brain_role_snapshot"]`,后续 round 内读快照而非全局 settings — see `backend/services/brain_role_switch_service.py` + plan §14.

### LLM routing (`agents/services/llm_service.py`)

All LLM calls go through `LLMService`. Providers are a **named registry** (`LLM_PROVIDERS` json feature-flag: `{name → {label, sdk, base_url}}`; secret key lives encrypted in `CredentialsService` under `credential:llm_provider_<name>`). A routing entry references a provider by name via `provider_ref` (expanded by `_expand_provider_ref`). Three are registered: `aliyun_coding_plan` (`coding.dashscope.aliyuncs.com`), `aliyun_maas` (`token-plan.cn-beijing.maas.aliyuncs.com`), `anthropic` (`ai.yaspost.com`). **Active provider = `aliyun_coding_plan`** — production switched off `aliyun_maas` (token-plan) on 2026-06-04 when its budget was exhausted. The codenamed model ids (e.g. `kimi-k2.5` for `code_gen`, `qwen3.6-plus` for `hypothesis`) are the real upstream ids on the coding-plan menu. `resolve_model_for(node_key)` picks per functional block: (1) **per-task override** from `task.config["llm_overrides"]` bound to a `ContextVar` (honoured regardless of the global flag); (2) **global map** `LLM_FUNCTION_MODEL_MAP`, only when `ENABLE_PER_FUNCTION_LLM_ROUTING` is ON, with a `__default__` catch-all that captures unmapped node_keys + untagged `node_key=None` calls. Returns `None` → caller uses `self.model` (flag-OFF + no override is byte-for-byte legacy). **Footgun (2026-06-04):** the base `openai` construction default resolves to `api.openai.com` with an EMPTY key (no `credential:openai_*` / no `.env OPENAI_*`) — a DEAD endpoint. So flipping `ENABLE_PER_FUNCTION_LLM_ROUTING` OFF is now effectively an **LLM kill-switch** (no working base fallback). The config.py STARTUP defaults (`_load_llm_function_model_map` + `_load_llm_providers` seed) were hardened to MIRROR the live coding-plan override so an override-deleted path still degrades to a live endpoint; the cross-process refresher (`feature_flag_runtime`) warms the cache from DB synchronously at lifespan / `worker_process_init` BEFORE the first task, so the live DB override (flag ON since 2026-05-31) is in effect with no cache-cold window. The circuit breaker is scoped **per-(provider, endpoint, model)**. Caveat: `config.py`'s `__getattribute__` flag hook only honours `ENABLE_`-prefixed override keys, so non-`ENABLE_` runtime config (e.g. `LLM_FUNCTION_MODEL_MAP`, `LLM_PROVIDERS`) must be read directly from `_flag_override_cache`, never via `settings.X`.

### Frontend layout

- Routing in `frontend/src/App.jsx` (Dashboard / Tasks / TaskDetail / AlphaDetail / DataManagement / ConfigCenter / Ops sub-routes). `AlphaLab` (and `FactorLibrary`) were retired in 2026-05 — `/alphas` redirects to `/tasks`; alpha detail remains at `/alphas/:id`.
- All HTTP via `frontend/src/services/api.js`. Vite dev server proxies `/api` → backend on `8001`; **don't hardcode `http://localhost:8001`** in components.
- Live activity uses SSE on `/api/v1/stats/live-feed`.

## Project conventions

- **`docs/DEVELOPMENT_PLAN.md` is the single mainline dev doc** (current architecture state + strategy + NO-GO + flag status; historical plans in `docs/archive/`). `agents/IMPROVEMENT_ANALYSIS.md`, `backend/CODE_STATUS.md`, `backend/REFACTORING_STATUS.md` are the working design notes — read them before larger refactors and update them when status changes. (`agents/core/ARCHITECTURE.md` was deleted with `agents/core/` in `b89b732`.)
- Keep root-level scripts (`ace_lib.py`, `helpful_functions.py`, `validator.py`, `run_real_mining.py`, `parsetab.py`) as standalone utilities; new logic should live under `backend/`.
- Top-level files `keys.txt`, `api_structure.json`, `brain_alpha_structure.json` are reference dumps from BRAIN — treat as read-only inputs, not authoritative state.
- Windows is the primary dev platform; Celery is launched with `--pool=solo` because the prefork pool is broken on Windows.
- Route all new LLM code through `backend/agents/services/llm_service.py` (settings/env + per-function routing win). Note `backend/agents/agent_hub.py` hard-codes a base URL — do not copy that pattern. See the *LLM routing* section above for the real production endpoint and model defaults.
- BRAIN role switching走 P3-Brain group 的 `FeatureFlagOverride`(键名 `ENABLE_BRAIN_CONSULTANT_MODE`),**不引入 `WQBCredential.role` 字段**。切换有安全网:`alpha_service.submit_alpha` 时 PROD-corr 403 自动回退 flag 并写 audit。**能力分类(方向 C)**:数据一致性能力(Sharpe 阈值 / testPeriod)走 task 启动快照,running task 不受切换影响;endpoint 选择能力(multi-sim / PROD-corr)走全局 `settings.ENABLE_BRAIN_CONSULTANT_MODE`,切回 USER 立即降级以避免 USER 状态调用 Consultant API。
