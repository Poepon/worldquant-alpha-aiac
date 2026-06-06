"""Phase 1b B4 — HG pool worker tests (mock workflow, no LLM/brain)."""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.database import SQLAlchemyBase
from backend.agents.graph.state import MiningState, AlphaCandidate
from backend.models import CandidateQueue, HypothesisIntent
from backend.pool import stages as st
from backend.pool import workers
from backend.pool.hydrate import hydrate_hg_state


class _FakeHGWorkflow:
    def __init__(self, final):
        self._final = final

    async def run_hypothesis(self, state, config=None):
        return state

    async def run_codegen(self, state, config=None):
        return self._final


async def _setup_db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    return eng, async_sessionmaker(eng, expire_on_commit=False)


def _final_state():
    return MiningState(
        task_id=7, region="USA", universe="TOP3000", dataset_id="pv1", delay=1,
        dataset_category="price_volume", rag_ab_arm="category", current_hypothesis_id=5,
        effective_default_test_period="P0Y", effective_sharpe_submit_min=1.58,
        patterns=[{"p": 1}], pitfalls=[{"q": 2}], cognitive_layer_id_used="macro_top_down",
        pending_alphas=[
            AlphaCandidate(expression="e1", is_valid=True, hypothesis="h1"),
            AlphaCandidate(expression="e2", is_valid=False),   # skipped
            AlphaCandidate(expression="e3", is_valid=True),
        ],
    )


@pytest.mark.asyncio
async def test_hg_process_one_emits_only_valid_with_full_context(monkeypatch):
    intent = HypothesisIntent(region="USA", universe="TOP3000", dataset_id="pv1",
                              delay=1, config_snapshot={})
    intent.id = 99
    intent.task_id = 7
    final = _final_state()

    async def _fake_hydrate(it, **kw):
        return final  # workflow passthrough ignores it anyway

    monkeypatch.setattr("backend.pool.workers.hydrate_hg_state", _fake_hydrate)
    rows = await workers.hg_process_one(_FakeHGWorkflow(final), intent, {})

    assert len(rows) == 2  # e1, e3 valid; e2 skipped
    r0 = rows[0]
    assert r0["expression"] == "e1" and r0["stage"] == st.SIM_PENDING
    assert r0["hyp_intent_id"] == 99 and r0["task_id"] == 7
    assert r0["current_hypothesis_id"] == 5
    assert r0["region"] == "USA" and r0["dataset_id"] == "pv1" and r0["delay"] == 1
    assert r0["dataset_category"] == "price_volume" and r0["rag_ab_arm"] == "category"
    assert r0["effective_default_test_period"] == "P0Y"
    assert r0["effective_sharpe_submit_min"] == 1.58
    # full RAG/distill context serialized into candidate_queue.context (gotcha #1)
    assert r0["context"]["hypothesis"] == "h1"
    assert r0["context"]["patterns"] == [{"p": 1}]
    assert r0["context"]["pitfalls"] == [{"q": 2}]
    assert r0["context"]["cognitive_layer_id_used"] == "macro_top_down"
    assert rows[1]["expression"] == "e3"


@pytest.mark.asyncio
async def test_hg_process_one_rolls_back_rag_session(monkeypatch):
    """P1: the RAG read txn on wdb is released (rollback) AFTER run_hypothesis and
    BEFORE the LLM-bound codegen, so it doesn't sit idle-in-transaction through it."""
    intent = HypothesisIntent(region="USA", universe="TOP3000", dataset_id="pv1",
                              delay=1, config_snapshot={})
    intent.id = 1
    intent.task_id = 7
    final = _final_state()

    async def _fake_hydrate(it, **kw):
        return final
    monkeypatch.setattr("backend.pool.workers.hydrate_hg_state", _fake_hydrate)

    order = []

    class _FakeWdb:
        async def rollback(self):
            order.append("rollback")

    class _OrderedWorkflow:
        async def run_hypothesis(self, state, config=None):
            order.append("hyp")
            return state

        async def run_codegen(self, state, config=None):
            order.append("codegen")
            return final

    rows = await workers.hg_process_one(_OrderedWorkflow(), intent, {}, wdb=_FakeWdb())
    assert order == ["hyp", "rollback", "codegen"]  # released between hyp and codegen
    assert len(rows) == 2  # still emits the valid candidates (e1, e3)


@pytest.mark.asyncio
async def test_hg_process_one_no_wdb_skips_rollback(monkeypatch):
    """wdb=None (legacy/test path) → no rollback attempted, no crash."""
    intent = HypothesisIntent(region="USA", dataset_id="pv1", config_snapshot={})
    intent.id = 2
    final = _final_state()

    async def _fake_hydrate(it, **kw):
        return final
    monkeypatch.setattr("backend.pool.workers.hydrate_hg_state", _fake_hydrate)
    rows = await workers.hg_process_one(_FakeHGWorkflow(final), intent, {})  # no wdb
    assert len(rows) == 2


def test_resolve_hyp_id_scalar_then_list():
    s1 = MiningState(task_id=1, region="USA", current_hypothesis_id=8)
    assert workers._resolve_hyp_id(s1) == 8
    s2 = MiningState(task_id=1, region="USA", current_hypothesis_id=None,
                     current_hypothesis_ids=[3, 4])
    assert workers._resolve_hyp_id(s2) == 3  # scalar dropped → first of list
    s3 = MiningState(task_id=1, region="USA")
    assert workers._resolve_hyp_id(s3) is None


@pytest.mark.asyncio
async def test_emit_candidates_and_complete_is_atomic():
    """P0 fix: candidate INSERT + intent→DONE in ONE txn (no duplicate-on-retry
    window). Owner-guarded."""
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            async with s.begin():
                intent = HypothesisIntent(region="USA", config_snapshot={},
                                          stage=st.INTENT_CLAIMED, claimed_by="hg-1")
                s.add(intent)
                await s.flush()
                iid = intent.id
        rows = [dict(region="USA", expression="e1", stage=st.SIM_PENDING, hyp_intent_id=iid),
                dict(region="USA", expression="e2", stage=st.SIM_PENDING, hyp_intent_id=iid)]
        n = await workers.emit_candidates_and_complete(iid, rows, worker_id="hg-1", session_factory=sf)
        assert n == 2
        async with sf() as s:
            cands = (await s.execute(select(CandidateQueue))).scalars().all()
            intent = await s.get(HypothesisIntent, iid)
        assert len(cands) == 2 and {c.expression for c in cands} == {"e1", "e2"}
        assert intent.stage == st.INTENT_DONE
        assert intent.claimed_by is None and intent.lease_expires_at is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_emit_candidates_and_complete_stale_emits_nothing():
    """P0 fix: if the intent was lease-recycled + reclaimed by another HG worker,
    a stale worker emits NO duplicate candidates and leaves the intent untouched."""
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            async with s.begin():
                intent = HypothesisIntent(region="USA", config_snapshot={},
                                          stage=st.INTENT_CLAIMED, claimed_by="hg-2")
                s.add(intent)
                await s.flush()
                iid = intent.id
        rows = [dict(region="USA", expression="dup", stage=st.SIM_PENDING, hyp_intent_id=iid)]
        n = await workers.emit_candidates_and_complete(iid, rows, worker_id="hg-1", session_factory=sf)
        assert n == -1  # stale claim
        async with sf() as s:
            cands = (await s.execute(select(CandidateQueue))).scalars().all()
            intent = await s.get(HypothesisIntent, iid)
        assert cands == []  # NO duplicate candidate rows
        assert intent.stage == st.INTENT_CLAIMED and intent.claimed_by == "hg-2"  # untouched
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_hydrate_hg_state_builds_round_state(monkeypatch):
    async def _fake_fields(s, ds, r, u, d):
        return [{"field_id": "close"}]

    async def _fake_ops(s):
        return [{"name": "rank"}]

    monkeypatch.setattr("backend.tasks.fetch_helpers._get_dataset_fields", _fake_fields)
    monkeypatch.setattr("backend.tasks.fetch_helpers._get_operators", _fake_ops)

    eng, sf = await _setup_db()
    try:
        intent = HypothesisIntent(
            region="USA", universe="TOP3000", dataset_id="pv1", delay=1, fanout=10,
            config_snapshot={"brain_role_snapshot": {
                "effective_default_test_period": "P0Y",
                "effective_sharpe_submit_min": 1.58,
                "brain_consultant_mode_at_start": True,
            }},
        )
        intent.task_id = 7
        state = await hydrate_hg_state(intent, session_factory=sf)
        assert state.region == "USA" and state.dataset_id == "pv1" and state.delay == 1
        assert state.num_alphas_target == 10
        assert state.fields == [{"field_id": "close"}]
        assert state.operators == [{"name": "rank"}]
        assert state.effective_default_test_period == "P0Y"
        assert state.effective_sharpe_submit_min == 1.58
        assert state.brain_consultant_mode_at_start is True
        assert state.available_dataset_pool == []  # legacy single-anchor
    finally:
        await eng.dispose()


def test_llm_overrides_set_clear_roundtrip():
    from backend.agents.services.llm_service import (
        _TASK_FN_OVERRIDES, set_task_function_overrides, clear_task_function_overrides,
    )
    assert _TASK_FN_OVERRIDES.get() is None
    tok = set_task_function_overrides({"hypothesis": {"model": "x"}})
    assert _TASK_FN_OVERRIDES.get() == {"hypothesis": {"model": "x"}}
    clear_task_function_overrides(tok)
    assert _TASK_FN_OVERRIDES.get() is None
