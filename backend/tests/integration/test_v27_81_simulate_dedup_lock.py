"""V-27.81 — simulate dedup in-flight lock.

RCA: docs/v27_backlog.md B 段. filter_unsimulated_expressions SELECTs which
expression hashes already exist in `alphas`, but between that SELECT and the
brain.simulate_alpha call another worker can simulate the same
(hash, region, universe) — both burn a BRAIN slot. Fix: a Redis SET-NX
in-flight lock claimed before simulate, released after.

  - TestSimulateSlotLock — the lock primitives against a REAL Redis.
  - TestNodeSimulateDedupLock — node_simulate's dedup loop drops an
    expression whose slot is already held, and respects the flag.
  - flip-retry (node_evaluate) wires the same primitives — asserted at the
    source level here; the primitive behaviour is covered above.

Run:
    pytest backend/tests/integration/test_v27_81_simulate_dedup_lock.py -v

Requires: a reachable Redis (REDIS_URL). The node_simulate tests are pure
mock (no DB / Redis).
"""
from __future__ import annotations

import uuid

import pytest

from backend.tasks.redis_pool import (
    _simulate_lock_key,
    claim_simulate_slot,
    release_simulate_slot,
    get_redis_client,
)


@pytest.fixture
def slot_key():
    """A unique (hash, region, universe) triple, released on teardown."""
    triple = (f"testhash{uuid.uuid4().hex}", "ZZ1", "TOP3000")
    yield triple
    try:
        get_redis_client().delete(_simulate_lock_key(*triple))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lock primitives — real Redis
# ---------------------------------------------------------------------------

class TestSimulateSlotLock:
    def test_claim_first_succeeds(self, slot_key):
        # V-27.81 followup: claim now returns a per-claim token string (or
        # None when the slot is already held) instead of a bare bool.
        token = claim_simulate_slot(*slot_key)
        assert isinstance(token, str) and token

    def test_claim_second_fails(self, slot_key):
        assert claim_simulate_slot(*slot_key) is not None
        assert claim_simulate_slot(*slot_key) is None

    def test_claim_after_release_succeeds(self, slot_key):
        token = claim_simulate_slot(*slot_key)
        assert token is not None
        release_simulate_slot(*slot_key, token)
        assert claim_simulate_slot(*slot_key) is not None

    def test_release_is_cas_token_scoped(self, slot_key):
        # V-27.81 followup: release is a Lua CAS, not a blind DELETE. A
        # worker whose slot TTL expired and was re-claimed by someone else
        # must NOT delete the new holder's slot when it finally releases.
        stale_token = claim_simulate_slot(*slot_key)
        assert stale_token is not None
        # Simulate TTL expiry + another worker re-claiming the same slot.
        get_redis_client().delete(_simulate_lock_key(*slot_key))
        new_token = claim_simulate_slot(*slot_key)
        assert new_token is not None and new_token != stale_token
        # The original worker releases with its now-stale token — no-op.
        release_simulate_slot(*slot_key, stale_token)
        # The new holder's slot must still be held.
        assert claim_simulate_slot(*slot_key) is None
        # And the rightful owner can still release it.
        release_simulate_slot(*slot_key, new_token)
        assert claim_simulate_slot(*slot_key) is not None

    def test_key_distinct_per_triple(self):
        k1 = _simulate_lock_key("h1", "USA", "TOP3000")
        k2 = _simulate_lock_key("h1", "EUR", "TOP3000")
        k3 = _simulate_lock_key("h1", "USA", "TOP1000")
        k4 = _simulate_lock_key("h2", "USA", "TOP3000")
        assert len({k1, k2, k3, k4}) == 4

    def test_claim_fail_open_on_redis_error(self, monkeypatch):
        # SAFETY: a Redis outage must NOT block simulation — claim returns a
        # (locally-generated, unusable) token == pre-V-27.81 "proceed"
        # behaviour so the worker proceeds. release's CAS later no-ops.
        import backend.tasks.redis_pool as rp

        class _BoomClient:
            def set(self, *a, **k):
                raise ConnectionError("redis down")

        monkeypatch.setattr(rp, "get_redis_client", lambda: _BoomClient())
        assert claim_simulate_slot("h", "USA", "TOP3000") is not None


# ---------------------------------------------------------------------------
# node_simulate dedup loop — pure mock (no DB / Redis)
# ---------------------------------------------------------------------------

class _MockBrain:
    def __init__(self):
        self.simulated_exprs: list = []

    async def simulate_batch(self, expressions, **kw):
        self.simulated_exprs = list(expressions)
        return [
            {"success": True, "alpha_id": f"sim{i}", "metrics": {"sharpe": 1.0}}
            for i in range(len(expressions))
        ]


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _mk_state(exprs):
    from backend.agents.graph.state import MiningState, AlphaCandidate
    return MiningState(
        task_id=1, region="ZZ1", universe="TOP3000", dataset_id="pv1",
        fields=[], operators=[],
        pending_alphas=[
            AlphaCandidate(expression=e, is_valid=True, simulation_success=False)
            for e in exprs
        ],
    )


def _patch_node_simulate_deps(monkeypatch, *, claim_results):
    """Patch out the DB session + filter so node_simulate runs offline.
    `claim_results` is a list of bool returned by successive claim calls."""
    async def _fake_filter(db, exprs, region, universe):
        return list(exprs), []  # everything is NEW (not in DB)

    monkeypatch.setattr(
        "backend.selection_strategy.filter_unsimulated_expressions", _fake_filter
    )
    monkeypatch.setattr(
        "backend.database.AsyncSessionLocal", lambda: _FakeSession()
    )
    # Disable the portfolio two-factor dedup — it's an independent filter
    # (V-26.77) that would otherwise eat generic skeletons like rank(FIELD)
    # and confound this test's claim/simulate accounting.
    monkeypatch.setattr(
        "backend.agents.seed_pool.portfolio_skeletons.get_portfolio_skeleton_index",
        lambda region: None,
    )
    claim_calls: list = []

    def _fake_claim(h, r, u):
        claim_calls.append((h, r, u))
        idx = len(claim_calls) - 1
        ok = claim_results[idx] if idx < len(claim_results) else True
        # V-27.81 followup: claim returns a token string on success / None on
        # contention, mirroring the real primitive.
        return f"tok{idx}" if ok else None

    monkeypatch.setattr(
        "backend.tasks.redis_pool.claim_simulate_slot", _fake_claim
    )
    monkeypatch.setattr(
        "backend.tasks.redis_pool.release_simulate_slot", lambda *a: None
    )
    return claim_calls


class TestNodeSimulateDedupLock:
    @pytest.mark.asyncio
    async def test_in_flight_duplicate_skipped(self, monkeypatch):
        from backend.agents.graph.nodes import evaluation

        # claim succeeds for the 1st expr, fails for the 2nd (held by a
        # concurrent worker).
        claim_calls = _patch_node_simulate_deps(
            monkeypatch, claim_results=[True, False]
        )
        state = _mk_state(["rank(close)", "rank(open)"])
        brain = _MockBrain()

        result = await evaluation.node_simulate(state, brain, None)
        pending = result["pending_alphas"]

        # 2nd expr marked in-flight duplicate, never sent to BRAIN
        assert "in-flight duplicate" in (pending[1].simulation_error or "")
        assert pending[1].is_simulated is True
        assert pending[1].simulation_success is False
        assert brain.simulated_exprs == ["rank(close)"]
        assert len(claim_calls) == 2

    @pytest.mark.asyncio
    async def test_all_claimable_all_simulated(self, monkeypatch):
        from backend.agents.graph.nodes import evaluation

        _patch_node_simulate_deps(monkeypatch, claim_results=[True, True])
        state = _mk_state(["rank(close)", "rank(open)"])
        brain = _MockBrain()

        await evaluation.node_simulate(state, brain, None)
        assert set(brain.simulated_exprs) == {"rank(close)", "rank(open)"}

    @pytest.mark.asyncio
    async def test_flag_off_skips_claim(self, monkeypatch):
        from backend.agents.graph.nodes import evaluation
        from backend.config import settings

        monkeypatch.setattr(settings, "SIMULATE_DEDUP_LOCK_ENABLED", False)
        claim_calls = _patch_node_simulate_deps(
            monkeypatch, claim_results=[False, False]
        )
        state = _mk_state(["rank(close)", "rank(open)"])
        brain = _MockBrain()

        await evaluation.node_simulate(state, brain, None)
        # Flag off → claim_simulate_slot never called, both still simulated.
        assert claim_calls == []
        assert set(brain.simulated_exprs) == {"rank(close)", "rank(open)"}


# ---------------------------------------------------------------------------
# flip-retry wires the same primitives (source-level guard)
# ---------------------------------------------------------------------------

def test_flip_retry_wires_dedup_lock():
    import inspect
    from backend.agents.graph.nodes import evaluation
    src = inspect.getsource(evaluation.node_evaluate)
    # flip-retry dedup loop must claim + release the in-flight slot.
    assert "claim_simulate_slot" in src, (
        "flip-retry no longer claims an in-flight simulate slot — V-27.81 lost"
    )
    assert "release_simulate_slot" in src, (
        "flip-retry no longer releases its in-flight simulate slots — leak risk"
    )
    assert "SIMULATE_DEDUP_LOCK_ENABLED" in src, (
        "flip-retry no longer respects the V-27.81 kill-switch flag"
    )
