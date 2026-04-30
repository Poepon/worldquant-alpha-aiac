# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AIAC 2.0 is a Human-AI collaborative alpha-mining platform built on the **Alpha-GPT** paradigm fused with **RD-Agent**'s CoSTEER feedback loop. It targets WorldQuant BRAIN, generating alpha expressions via LLM, simulating them on BRAIN, and self-evolving via a knowledge base.

Stack: Python 3.10+ FastAPI backend (async SQLAlchemy + asyncpg), Celery (Redis) for background jobs, LangGraph for agent workflows, OpenAI-compatible LLM (DeepSeek by default), React 18 + Vite + Ant Design frontend, PostgreSQL.

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

`backend/agents/` is the brain of the system. Two parallel layers exist:

1. **Legacy / production path** — `mining_agent.py`, `feedback_agent.py`, `strategy_agent.py`, `evolution_strategy.py`, `field_screener.py`, plus the LangGraph workflow in `agents/graph/` (state in `state.py`, edges in `edges.py`, node implementations split under `graph/nodes/{generation,validation,evaluation,persistence,base}.py`). This is what Celery's `run_mining_task` invokes today.
2. **RD-Agent-style core** — `agents/core/` (see `agents/core/ARCHITECTURE.md`) provides `Hypothesis`, `AlphaExperiment`, `EvoStep`, `ExperimentTrace` (DAG), `EvolvingKnowledge`, `HypothesisFeedback` (with `AttributionType` separating hypothesis vs. implementation failures), and a decoupled pipeline (`HypothesisGen` → `Hypothesis2Experiment` → `ExperimentRunner` → `Experiment2Feedback`). It is integrated into the legacy path via `agents/core/integration.py` (e.g. `enhance_existing_node_evaluate`, `run_enhanced_mining`).

The standard mining trace per alpha follows `TraceStepType` (in `models/base.py`):
`RAG_QUERY → HYPOTHESIS → CODE_GEN → VALIDATE → SIMULATE → SELF_CORRECT? → EVALUATE`.

Prompts are in `backend/agents/prompts/` (loaded from `prompts.yaml` via `loader.py` + `registry.py`); the legacy `agents/prompts.py` shim re-exports them.

### Standalone analytics modules

These are pure-function modules orchestrated by services/agents — keep them dependency-free and unit-testable:

| File | Purpose |
|------|---------|
| `alpha_scoring.py` | Composite score + adaptive thresholds |
| `alpha_semantic_validator.py` | Operator-aware syntax / semantics check (loads operator registry from DB at startup) |
| `dataset_selector.py` | Bandit-style dataset picker (controlled by `BANDIT_*` settings) |
| `genetic_optimizer.py` | GA over alpha expressions (operator/window/wrapper/sign/structure mutations) |
| `diversity_tracker.py` | Fingerprint dedup + novelty score |
| `external_knowledge.py` | Forum + 101-Alphas pattern import |
| `metrics_tracker.py` | Session/Round/Alpha metrics, writes `.cursor/debug.log` |
| `experiment_tracker.py`, `multi_fidelity_eval.py`, `optimization_chain.py`, `selection_strategy.py`, `knowledge_extraction.py` | Supporting evolution / scheduling logic |

### BRAIN integration

All HTTP traffic to `platform.worldquantbrain.com` goes through `backend/adapters/brain_adapter.py` (implementing `protocols/brain_protocol.py`). Credentials come from `BRAIN_EMAIL` / `BRAIN_PASSWORD` in `.env`, optionally overridden per-user via `WQBCredential` rows (`services/credentials_service.py`). Sync tasks (`tasks/sync_tasks.py`) populate `DatasetMetadata`, `DataField`, `Operator`, and user `Alpha` rows from BRAIN — they're scheduled by `celery_beat_schedule` in `celery_app.py` (datasets daily at 06:00, operator stats every 6h, feedback at 23:00, timezone Asia/Shanghai).

### Configuration

`backend/config.py` (Pydantic Settings) is the single source of truth. It reads `.env` and exposes thresholds (`SHARPE_MIN`, `TURNOVER_MAX`, `FITNESS_MIN`, `MAX_CORRELATION`, `SCORE_PASS_THRESHOLD`), bandit / field / diversity weights, multi-fidelity flags, and rate limits (`MAX_SIMULATIONS_PER_DAY`, `MAX_TOKENS_PER_DAY`). Add new tunables here, not as scattered constants.

### Frontend layout

- Routing in `frontend/src/App.jsx` (Dashboard / Tasks / TaskDetail / AlphaLab / AlphaDetail / DataManagement / ConfigCenter).
- All HTTP via `frontend/src/services/api.js`. Vite dev server proxies `/api` → backend on `8001`; **don't hardcode `http://localhost:8001`** in components.
- Live activity uses SSE on `/api/v1/stats/live-feed`.

## Project conventions

- `agents/IMPROVEMENT_ANALYSIS.md`, `backend/CODE_STATUS.md`, `backend/REFACTORING_STATUS.md`, and `backend/agents/core/ARCHITECTURE.md` are the working design notes — read them before larger refactors and update them when status changes.
- Keep root-level scripts (`ace_lib.py`, `helpful_functions.py`, `validator.py`, `run_real_mining.py`, `parsetab.py`) as standalone utilities; new logic should live under `backend/`.
- Top-level files `keys.txt`, `api_structure.json`, `brain_alpha_structure.json` are reference dumps from BRAIN — treat as read-only inputs, not authoritative state.
- Windows is the primary dev platform; Celery is launched with `--pool=solo` because the prefork pool is broken on Windows.
- Default LLM points at `https://api.deepseek.com/v1` with model `deepseek-chat`. Note that `backend/agents/agent_hub.py` currently hard-codes the DeepSeek base URL — prefer `backend/agents/services/llm_service.py` for new code so settings/env wins.
