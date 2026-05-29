"""Integration tests for per-functional-block LLM model routing (plan §6).

Source: docs/per_function_llm_routing_plan_2026-05-29.md §6 + the 2026-05-30
fresh-eye review folded into the merry-walrus plan.

The PR1-PR5 *unit* tests (test_llm_function_routing / test_llm_per_call_routing /
test_llm_task_override) all inject the routing map by directly poking
``cfg._flag_override_cache`` — which BYPASSES the one link plan P0-1 says is the
likeliest silent-failure point: the DB → ``load_overrides_into_cache`` → cache
hop. ``LLM_FUNCTION_MODEL_MAP`` is NOT an ``ENABLE_``-prefixed flag, so
``settings.X`` can never see it; routing only works if ``resolve_model_for``
direct-reads the cache AND ``load_overrides_into_cache`` actually populates it
from a real override row.

So these tests "真跑 cache,不全 mock": tests 1/2/3/5/6/7 read through the REAL
``_flag_override_cache``; 1/2 write a REAL ``FeatureFlagOverride`` row to an
in-memory DB and drive the REAL ``FeatureFlagService.load_overrides_into_cache``;
3/5/6/7 go through the REAL ``LLMService.call()`` path (fake HTTP transport only).

Two extra "pin" tests document review-found gaps (global circuit brown-out;
``api_key_ref`` not yet wired) so a future fix is a CONSCIOUS behaviour change.

No live Postgres: ``FeatureFlagOverride`` uses only text/bool columns and the
task is an in-memory SimpleNamespace.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.config as cfg
from backend.config import settings, _flag_override_cache
from backend.models.config import FeatureFlagAudit, FeatureFlagOverride
from backend.services.feature_flag_service import FeatureFlagService
from backend.agents.services.llm_service import (
    LLMService,
    resolve_model_for,
    set_task_function_overrides,
    clear_task_function_overrides,
)
import backend.agents.services.llm_service as llm_mod


# =========================================================================
# Fixtures — real (in-memory) DB carrying FeatureFlagOverride + the real
# _flag_override_cache, plus a fake-transport LLMService.
# =========================================================================
@pytest_asyncio.fixture
async def ff_engine():
    """In-memory engine with ONLY the feature-flag tables (text columns →
    SQLite-safe; no JSONB). Mirrors test_feature_flag_runtime.py:35."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    isolated = MetaData()
    FeatureFlagOverride.__table__.to_metadata(isolated)
    FeatureFlagAudit.__table__.to_metadata(isolated)
    async with engine.begin() as conn:
        await conn.run_sync(isolated.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_maker(ff_engine):
    return sessionmaker(ff_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _isolate_cache_and_ctx():
    """Every test starts/ends with an empty cache + no task override binding,
    so the real-cache reads are deterministic and never bleed across tests."""
    _flag_override_cache.clear()
    clear_task_function_overrides()
    yield
    _flag_override_cache.clear()
    clear_task_function_overrides()


# --- fake OpenAI transport (echoes the model it was asked for) -----------
# Identical contract to test_llm_per_call_routing.py:24 so behaviour matches
# the PR2 unit suite.
class _FakeCompletions:
    def __init__(self, fail_models=None):
        self.fail_models = set(fail_models or ())
        self.models_seen = []

    async def create(self, **kw):
        m = kw["model"]
        self.models_seen.append(m)
        if m in self.fail_models:
            raise TimeoutError(f"simulated API failure for {m}")
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"model": m}),
                                        reasoning_content=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(total_tokens=10),
        )


class _FakeOpenAIClient:
    def __init__(self, fail_models=None):
        self.chat = SimpleNamespace(completions=_FakeCompletions(fail_models))


@pytest.fixture
def svc(monkeypatch):
    """A default-openai LLMService whose HTTP transport is faked. Circuit
    disabled by default so routing/fallback logic is what's under test."""
    monkeypatch.setattr(settings, "ENABLE_LLM_API_CIRCUIT", False, raising=False)
    s = LLMService(provider="openai")
    s._credentials_loaded = True  # skip the DB credential reload
    s.client = _FakeOpenAIClient()
    return s


# --- helper: write override rows to the real DB + load the real cache ----
async def _seed_and_load(session_maker, *, routing_on, model_map):
    """Insert real FeatureFlagOverride rows then drive the REAL
    FeatureFlagService.load_overrides_into_cache — the exact path the
    /ops/flags/refresh-all endpoint + the 60s refresher use."""
    async with session_maker() as db:
        db.add(FeatureFlagOverride(
            flag_name="ENABLE_PER_FUNCTION_LLM_ROUTING",
            flag_value=json.dumps(bool(routing_on)),
            flag_type="bool",
            updated_by="test",
        ))
        if model_map is not None:
            db.add(FeatureFlagOverride(
                flag_name="LLM_FUNCTION_MODEL_MAP",
                flag_value=json.dumps(model_map),
                flag_type="json",
                updated_by="test",
            ))
        await db.commit()
        loaded = await FeatureFlagService(db).load_overrides_into_cache()
    return loaded


# =========================================================================
# Test 1 — P0-1 hammer: DB row → real cache → resolve_model_for sees it.
# =========================================================================
@pytest.mark.asyncio
async def test_db_to_cache_to_resolve_roundtrip(session_maker):
    model_map = {"code_gen": {"model": "deepseek-v4-flash", "provider": "openai"}}
    loaded = await _seed_and_load(session_maker, routing_on=True, model_map=model_map)

    # The non-ENABLE_ json flag must actually land in the cache (P0-1).
    assert loaded.get("LLM_FUNCTION_MODEL_MAP") == model_map
    assert _flag_override_cache.get("LLM_FUNCTION_MODEL_MAP") == model_map
    # And the ENABLE_ flag is visible through the settings hook (drives routing).
    assert settings.ENABLE_PER_FUNCTION_LLM_ROUTING is True

    # The whole point: resolve reads the DB-written map (NOT via settings.X).
    assert resolve_model_for("code_gen") == {
        "model": "deepseek-v4-flash", "provider": "openai",
    }
    # An unmapped node still falls through to default.
    assert resolve_model_for("self_correct") is None


# =========================================================================
# Test 2 — master switch OFF in DB → byte-for-byte legacy (no routing).
# =========================================================================
@pytest.mark.asyncio
async def test_db_flag_off_no_routing(session_maker):
    model_map = {"code_gen": {"model": "ROUTED", "provider": "openai"}}
    await _seed_and_load(session_maker, routing_on=False, model_map=model_map)

    # Map IS in the cache, but the gate is off via the real hook.
    assert _flag_override_cache.get("LLM_FUNCTION_MODEL_MAP") == model_map
    assert settings.ENABLE_PER_FUNCTION_LLM_ROUTING is False
    assert resolve_model_for("code_gen") is None  # legacy default path


# =========================================================================
# Test 3 — §6 scenario 2: per-node branching end-to-end (real cache + real
# call(), fake transport). hypothesis≠code_gen → different model on the wire.
# =========================================================================
@pytest.mark.asyncio
async def test_per_node_model_branching_end_to_end(session_maker, svc):
    await _seed_and_load(session_maker, routing_on=True, model_map={
        "hypothesis": {"model": "MODEL-H", "provider": "openai"},
        "code_gen": {"model": "MODEL-C", "provider": "openai"},
    })

    r_h = await svc.call("sys", "user json", node_key="hypothesis")
    r_c = await svc.call("sys", "user json", node_key="code_gen")

    assert r_h.success and r_h.model == "MODEL-H"
    assert r_c.success and r_c.model == "MODEL-C"
    # Both distinct models actually reached the (fake) transport.
    seen = svc.client.chat.completions.models_seen
    assert "MODEL-H" in seen and "MODEL-C" in seen


# =========================================================================
# Test 4 — the production node functions actually PASS their node_key. This
# is the seam that makes routing fire at all; if a node dropped node_key,
# routing would silently never apply to it.
# =========================================================================
@pytest.mark.asyncio
async def test_nodes_pass_their_node_key():
    from backend.tests.fixtures.mock_llm import MockLLMService
    from backend.agents.graph.state import MiningState
    from backend.agents.graph.nodes.generation import node_code_gen, node_hypothesis

    # Both nodes are (state, llm_service, config); their pre-call DB reads
    # (resolve_db) are wrapped non-fatal, so they reach the llm_service.call
    # even with no real DB. config=None → default RunnableConfig handling.
    def _state():
        return MiningState(
            task_id=1, region="USA", dataset_id="ds1",
            fields=[{"id": "close", "type": "MATRIX", "description": "close price"}],
            operators=[{"name": "rank", "definition": "rank(x)"}],
        )

    # --- code_gen ---
    mock_cg = MockLLMService()
    mock_cg.set_json_response({"alphas": [
        {"expression": "rank(close)", "hypothesis": "h", "confidence": 0.8},
    ]})
    try:
        await node_code_gen(_state(), llm_service=mock_cg, config=None)
    except Exception:
        # Downstream parsing/persistence may need more state; the call (and its
        # node_key) is recorded BEFORE any of that, which is all we assert.
        pass
    assert "code_gen" in [c.get("node_key") for c in mock_cg.get_call_history()], \
        "node_code_gen must call llm_service with node_key='code_gen'"

    # --- hypothesis ---
    mock_hy = MockLLMService()
    mock_hy.set_json_response({"hypotheses": [{"hypothesis": "h", "rationale": "r"}]})
    try:
        await node_hypothesis(_state(), llm_service=mock_hy, config=None)
    except Exception:
        pass
    assert "hypothesis" in [c.get("node_key") for c in mock_hy.get_call_history()], \
        "node_hypothesis must call llm_service with node_key='hypothesis'"


# =========================================================================
# Test 5 — PR5: task.config["llm_overrides"] flows through the contextvar
# into the real call(), independent of the global flag, beating the global map.
# =========================================================================
@pytest.mark.asyncio
async def test_task_override_flows_into_call(session_maker, svc):
    # Global flag OFF + a CONFLICTING global map present in the real cache.
    await _seed_and_load(session_maker, routing_on=False, model_map={
        "code_gen": {"model": "GLOBAL-M", "provider": "openai"},
    })
    assert settings.ENABLE_PER_FUNCTION_LLM_ROUTING is False

    task = SimpleNamespace(id=7, region="USA", config={
        "llm_overrides": {"code_gen": {"model": "MODEL-T", "provider": "openai"}},
    })
    # Same call mining_tasks.py:1465 / mining_agent.py:636 make.
    token = set_task_function_overrides((task.config or {}).get("llm_overrides"))
    try:
        resp = await svc.call("sys", "user json", node_key="code_gen")
    finally:
        clear_task_function_overrides(token)

    # Task-level override wins even with the global flag OFF.
    assert resp.success and resp.model == "MODEL-T"
    # And it's gone after clear → back to default.
    assert resolve_model_for("code_gen") is None


# =========================================================================
# Test 6 — PR3: telemetry records the EFFECTIVE (routed) model, not default.
# =========================================================================
@pytest.mark.asyncio
async def test_telemetry_records_effective_model(session_maker, svc, monkeypatch):
    await _seed_and_load(session_maker, routing_on=True, model_map={
        "code_gen": {"model": "ROUTED-X", "provider": "openai"},
    })

    captured = {}

    def _spy(*, model, **kw):
        captured["model"] = model
        captured.update(kw)

    # call() does `from backend.cost_tracker import record_llm_call` at call time.
    monkeypatch.setattr("backend.cost_tracker.record_llm_call", _spy, raising=False)

    resp = await svc.call("sys", "user json", node_key="code_gen")

    assert resp.success and resp.model == "ROUTED-X"
    assert captured.get("model") == "ROUTED-X", \
        "cost telemetry must record the routed model, not svc.model"
    assert captured.get("model") != svc.model


# =========================================================================
# Test 7 — runtime degradation (plan 放行条件 3 / P1#6).
# Circuit CLOSED: routed model fails at API level → fall back to default ONCE.
# =========================================================================
@pytest.mark.asyncio
async def test_runtime_degradation_circuit_closed(session_maker, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_LLM_API_CIRCUIT", False, raising=False)
    s = LLMService(provider="openai")
    s._credentials_loaded = True
    s.client = _FakeOpenAIClient(fail_models={"BAD-routed"})

    await _seed_and_load(session_maker, routing_on=True, model_map={
        "code_gen": {"model": "BAD-routed", "provider": "openai"},
    })

    resp = await s.call("sys", "user json", node_key="code_gen")

    assert resp.success and resp.model == s.model  # fell back to default
    seen = s.client.chat.completions.models_seen
    assert seen == ["BAD-routed", s.model]  # tried routed, then default — once


# =========================================================================
# Test 7b (review-gap PIN #1) — global circuit OPEN suppresses BOTH the routed
# call and its fallback. This documents the CURRENT global-circuit limitation
# (plan 放行条件 3③ asked for per-(provider,endpoint); not yet done). When that
# fix lands, this assertion must FLIP — making the change a conscious one.
# =========================================================================
@pytest.mark.asyncio
async def test_circuit_open_suppresses_fallback_known_limitation(
    session_maker, monkeypatch
):
    monkeypatch.setattr(settings, "ENABLE_LLM_API_CIRCUIT", True, raising=False)
    monkeypatch.setattr(llm_mod.LLM_API_CIRCUIT, "is_open", lambda: True)

    s = LLMService(provider="openai")
    s._credentials_loaded = True
    s.client = _FakeOpenAIClient()  # transport is healthy; the circuit isn't

    await _seed_and_load(session_maker, routing_on=True, model_map={
        "code_gen": {"model": "ROUTED-Y", "provider": "openai"},
    })

    resp = await s.call("sys", "user json", node_key="code_gen")

    # Fast-fail: no HTTP attempt at all, and the default-model fallback is also
    # suppressed (a single bad routed provider browns out the whole fleet).
    assert resp.success is False
    assert resp.error == "llm_api_circuit_open"
    assert s.client.chat.completions.models_seen == []  # default never tried


# =========================================================================
# Test 8 (review-gap PIN #2) — api_key_ref is accepted but NOT yet wired into
# client construction (_get_client reuses self.api_key). Documents the PR5
# follow-up so a real per-key path is a conscious change later.
# =========================================================================
def test_api_key_ref_currently_ignored(svc):
    # A routed entry naming a different credential key still builds a client
    # keyed by the DEFAULT openai key — i.e. api_key_ref had no effect.
    svc._get_client("openai", None, "MOONSHOT_API_KEY")
    expected_key = LLMService._client_cache_key("openai", svc.base_url, svc.api_key)
    assert expected_key in svc._client_cache, \
        "api_key_ref is currently ignored — client uses the default openai key"
