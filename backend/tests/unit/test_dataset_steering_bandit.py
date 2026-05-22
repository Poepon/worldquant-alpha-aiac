"""Unit tests for the dataset-steering value bandit (Tier A, plan v3).

Layers:
  - pure math (selection_strategy.discounted_thompson_update / thompson_sample_
    weight / weighted_choice) — dialect-free, assert the β≥0 invariant + the
    seed-preservation (C-1) + floor decay + weighted-pick distribution.
  - reward classification (dataset_weight_refresh._classify) — PRESIM_SKIP
    exclusion + the can_submit/delta_score book-marginal gate.
  - the full beat-job flow on the in-memory aiosqlite fixture (the job is
    dialect-free Python-side aggregation + ORM upsert by design, so it runs on
    sqlite): seed-from-history → discounted incremental update → idempotent
    re-run → unresolved-dataset exclusion → flag-OFF no-op.
  - DatasetSelector._load_bandit_state real-ORM round-trip (the iteration-bug
    fix — it previously iterated dict keys → silent AttributeError → never
    loaded; per [[feedback_orm_constructor_real_test]]).
"""
import random
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.selection_strategy import (
    discounted_thompson_update,
    thompson_sample_weight,
    weighted_choice,
)
from backend.tasks.dataset_weight_refresh import _classify, _refresh_async, _WM_KEY


# =============================================================================
# Pure math
# =============================================================================
class TestDiscountedThompsonUpdate:
    def test_beta_stays_nonnegative(self):
        # The v1 invariant: single nested Bernoulli count, 0<=s<=t → β>=0.
        for alpha, beta, s, t in [
            (1.0, 1.0, 0, 0), (1.0, 1.0, 5, 5), (1.0, 1.0, 0, 50),
            (2.0, 2280.0, 1, 50), (10.0, 870.0, 0, 200), (5.0, 5.0, 3, 7),
        ]:
            a2, b2 = discounted_thompson_update(alpha, beta, s, t, gamma=0.95)
            assert b2 >= 0.0, (alpha, beta, s, t, b2)
            assert a2 >= 0.0

    def test_quiet_arm_g1_preserves(self):
        # t_d=0 → g=γ^0=1 → posterior unchanged (no calendar drift).
        a2, b2 = discounted_thompson_update(2.0, 2280.0, 0, 0, gamma=0.95)
        assert a2 == 2.0 and b2 == 2280.0

    def test_seed_not_wiped_on_quiet_window(self):
        # C-1 at the math level: a heavily-seeded arm (β=2280) that gets no
        # new sims this window keeps its seed exactly (g=1).
        a, b = 2.0, 2280.0
        for _ in range(3):  # several quiet refreshes
            a, b = discounted_thompson_update(a, b, 0, 0, gamma=0.95)
        assert (a, b) == (2.0, 2280.0)

    def test_heavy_window_forgets_prior(self):
        # Many sims in one window → g=γ^t≈0 → the prior is heavily discounted
        # so the posterior MEAN tracks this window's recent yield, not the
        # stale seed. seed mean ≈ 2/2282 ≈ 0.0009; window yield = 2/100 = 0.02.
        a2, b2 = discounted_thompson_update(2.0, 2280.0, 2, 100, gamma=0.95)
        assert b2 >= 0.0
        recent_mean = a2 / (a2 + b2)
        assert 0.012 < recent_mean < 0.025   # near the 2% window yield
        assert recent_mean > 0.0009 * 5      # far above the stale seed mean

    def test_s_clamped_to_t(self):
        # Defensive: malformed s>t never drives β negative.
        _, b2 = discounted_thompson_update(1.0, 1.0, 99, 3, gamma=0.95)
        assert b2 >= 0.0


class TestThompsonSampleWeight:
    def test_minedout_weight_tiny(self):
        rng = random.Random(0)
        w = thompson_sample_weight(2.0, 2280.0, 2280, floor_c=0.1, tau=500.0, rng=rng)
        assert 0.0 < w < 0.05  # pv1-like: posterior ≈0.0009 + decayed floor ≈0

    def test_undermined_keeps_floor(self):
        # Beta(1,1) under-mined source, pulls=0 → floor ≈ floor_c present so it
        # is never starved by weighted sampling.
        rng = random.Random(0)
        w = thompson_sample_weight(1.0, 1.0, 0, floor_c=0.1, tau=500.0, rng=rng)
        assert w > 0.1  # at least the full floor on top of θ∈(0,1)

    def test_floor_decays_with_pulls(self):
        # Same posterior, same RNG state → only the floor differs; more
        # cumulative pulls → smaller floor → smaller weight.
        w_fresh = thompson_sample_weight(3.0, 3.0, 0, floor_c=0.1, tau=500.0, rng=random.Random(7))
        w_mined = thompson_sample_weight(3.0, 3.0, 5000, floor_c=0.1, tau=500.0, rng=random.Random(7))
        assert w_fresh > w_mined


class TestWeightedChoice:
    def test_single_item(self):
        assert weighted_choice(["a"], [3.0]) == "a"

    def test_empty(self):
        assert weighted_choice([], []) is None

    def test_uniform_fallback_on_zero_weights(self):
        # All-zero weights → uniform fallback (never starves the loop).
        picks = [weighted_choice(["a", "b"], [0.0, 0.0], rng=random.Random(i)) for i in range(50)]
        assert set(picks) == {"a", "b"}

    def test_length_mismatch_fallback(self):
        # weights misaligned → uniform fallback, still returns a valid item.
        assert weighted_choice(["a", "b"], [1.0]) in {"a", "b"}

    def test_favors_high_weight(self):
        # Seeded RNG distribution check (ON path is unassertable position-wise).
        rng = random.Random(42)
        n_hi = sum(
            weighted_choice(["lo", "hi"], [0.01, 10.0], rng=rng) == "hi"
            for _ in range(1000)
        )
        assert n_hi > 950  # ~99.9% expected; loose bound for RNG variance


# =============================================================================
# Reward classification
# =============================================================================
class TestClassify:
    def test_presim_skip_excluded(self):
        assert _classify({"_pre_brain_skip": True}, True) == (False, False)

    def test_real_sim_no_marginal(self):
        assert _classify({}, True) == (True, False)

    def test_book_marginal_positive(self):
        assert _classify({"_iqc_marginal": {"delta_score": 0.3}}, True) == (True, True)

    def test_marginal_nonpositive(self):
        assert _classify({"_iqc_marginal": {"delta_score": -0.1}}, True) == (True, False)
        assert _classify({"_iqc_marginal": {"delta_score": 0.0}}, True) == (True, False)

    def test_marginal_requires_can_submit(self):
        assert _classify({"_iqc_marginal": {"delta_score": 0.3}}, False) == (True, False)

    def test_bool_delta_not_numeric(self):
        # True is an int subclass — must not count as a positive delta_score.
        assert _classify({"_iqc_marginal": {"delta_score": True}}, True) == (True, False)

    def test_missing_iqc(self):
        assert _classify({"is_sharpe": 1.2}, True) == (True, False)


# =============================================================================
# Beat-job full flow (real-ORM sqlite)
# =============================================================================
class _SharedSessionCM:
    """async-context wrapper that yields the test's shared session and does
    NOT close it on exit — so the job's commits/reads and the test's asserts
    all go through ONE session/connection (avoids StaticPool multi-session
    transaction-visibility skew where the job re-reads its own writes)."""

    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


@pytest_asyncio.fixture
def session_factory(db_session):
    return lambda: _SharedSessionCM(db_session)


async def _mk_alpha(db, dataset_id, region="USA", *, can_submit=False,
                    delta=None, presim=False, created_at=None):
    from backend.models import Alpha

    metrics = {}
    if presim:
        metrics["_pre_brain_skip"] = True
    if delta is not None:
        metrics["_iqc_marginal"] = {"delta_score": delta}
    a = Alpha(
        region=region, universe="TOP3000", dataset_id=dataset_id,
        expression="rank(close)", status="simulated", quality_status="PENDING",
        human_feedback="NONE", can_submit=can_submit, metrics=metrics,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(a)
    await db.flush()
    return a


async def _mk_dataset(db, dataset_id, region="USA"):
    from backend.models import DatasetMetadata

    db.add(DatasetMetadata(
        dataset_id=dataset_id, region=region, universe="TOP3000",
        name=dataset_id, mining_weight=1.0,
    ))
    await db.flush()


@pytest.mark.asyncio
class TestBeatJobFlow:
    async def _state(self, db):
        from backend.models import BanditState
        rows = (await db.execute(select(BanditState))).scalars().all()
        return {(r.region, r.dataset_id): r for r in rows}

    async def test_seed_from_history(self, db_session, session_factory):
        # pv1: 1 book-marginal + 3 plain real + 1 presim-skip → T=4, S=1.
        await _mk_dataset(db_session, "pv1")
        await _mk_dataset(db_session, "fundamental6")
        await _mk_alpha(db_session, "pv1", can_submit=True, delta=0.2)
        for _ in range(3):
            await _mk_alpha(db_session, "pv1", can_submit=True, delta=-0.1)
        await _mk_alpha(db_session, "pv1", presim=True)  # excluded from T
        # fundamental6: 2 book-marginal + 1 plain → T=3, S=2.
        await _mk_alpha(db_session, "fundamental6", can_submit=True, delta=0.5)
        await _mk_alpha(db_session, "fundamental6", can_submit=True, delta=0.1)
        await _mk_alpha(db_session, "fundamental6", can_submit=False)
        await db_session.commit()

        out = await _refresh_async(
            gamma=0.95, floor_c=0.1, tau=500.0, window_days=7,
            session_factory=session_factory, rng=random.Random(0),
        )
        assert out["seeded"] == 2 and out["updated"] == 0

        st = await self._state(db_session)
        pv1, fund = st[("USA", "pv1")], st[("USA", "fundamental6")]
        # seed: α=1+S, β=1+(T−S); PRESIM_SKIP excluded → pv1 T=4 not 5.
        assert (pv1.alpha_param, pv1.beta_param, pv1.pulls) == (2.0, 4.0, 4)
        assert (fund.alpha_param, fund.beta_param, fund.pulls) == (3.0, 2.0, 3)
        # mined-out pv1 posterior mean << fundamental's.
        assert pv1.alpha_param / (pv1.alpha_param + pv1.beta_param) < \
               fund.alpha_param / (fund.alpha_param + fund.beta_param)

        # mining_weight written back to the datasets rows (both moved off the
        # default 1.0 and stay positive). NOTE: we deliberately do NOT assert
        # the pv1<fund ORDERING of the sampled weight — θ is a random Beta draw
        # and the two posteriors overlap (review SF-3: the ON path is not
        # position-assertable). The deterministic value-ordering is the
        # posterior MEAN, asserted above.
        from backend.models import DatasetMetadata
        weights = {
            d: w for d, w in (await db_session.execute(
                select(DatasetMetadata.dataset_id, DatasetMetadata.mining_weight)
            )).all()
        }
        assert 0.0 < weights["pv1"] != 1.0
        assert 0.0 < weights["fundamental6"] != 1.0

        # watermark created.
        from backend.models import SystemConfig
        wm = (await db_session.execute(
            select(SystemConfig).where(SystemConfig.config_key == _WM_KEY)
        )).scalar_one_or_none()
        assert wm is not None and wm.config_value

    async def test_unresolved_dataset_excluded(self, db_session, session_factory):
        await _mk_dataset(db_session, "pv1")
        await _mk_alpha(db_session, "pv1", can_submit=True, delta=0.2)
        await _mk_alpha(db_session, None, can_submit=True, delta=0.9)  # NULL dataset_id
        await db_session.commit()

        await _refresh_async(
            gamma=0.95, floor_c=0.1, tau=500.0, window_days=7,
            session_factory=session_factory, rng=random.Random(0),
        )
        st = await self._state(db_session)
        assert set(st.keys()) == {("USA", "pv1")}  # NULL row never made an arm

    async def test_incremental_discounted_update(self, db_session, session_factory):
        # Seed alphas are OLD (outside the next window) so the incremental
        # window captures only the new sims.
        old = datetime.utcnow() - timedelta(days=10)
        await _mk_dataset(db_session, "pv1")
        await _mk_alpha(db_session, "pv1", can_submit=True, delta=0.2, created_at=old)
        await _mk_alpha(db_session, "pv1", can_submit=False, created_at=old)
        await db_session.commit()
        # First run seeds from history: T=2, S=1 → α=2, β=2, pulls=2.
        await _refresh_async(gamma=0.95, floor_c=0.1, tau=500.0, window_days=7,
                             session_factory=session_factory, rng=random.Random(0))
        st = await self._state(db_session)
        assert (st[("USA", "pv1")].alpha_param, st[("USA", "pv1")].pulls) == (2.0, 2)

        # New sims arrive; rewind the watermark so they fall in the next window.
        from backend.models import SystemConfig
        wm = (await db_session.execute(
            select(SystemConfig).where(SystemConfig.config_key == _WM_KEY)
        )).scalar_one()
        wm.config_value = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        for _ in range(3):  # 3 new real sims, 0 marginal → S=0, T=3
            await _mk_alpha(db_session, "pv1", can_submit=False, created_at=datetime.utcnow())
        await db_session.commit()

        await _refresh_async(gamma=0.95, floor_c=0.1, tau=500.0, window_days=7,
                             session_factory=session_factory, rng=random.Random(0))
        st = await self._state(db_session)
        pv1 = st[("USA", "pv1")]
        # discounted update on the 3-new window (S=0, T=3): g=γ^3,
        # α'=g·2+0, β'=g·2+3, pulls=2+3=5.
        g = 0.95 ** 3
        assert abs(pv1.alpha_param - g * 2.0) < 1e-6
        assert abs(pv1.beta_param - (g * 2.0 + 3.0)) < 1e-6
        assert pv1.pulls == 5

    async def test_idempotent_rerun_no_double_count(self, db_session, session_factory):
        old = datetime.utcnow() - timedelta(days=10)
        await _mk_dataset(db_session, "pv1")
        await _mk_alpha(db_session, "pv1", can_submit=True, delta=0.2, created_at=old)
        await _mk_alpha(db_session, "pv1", can_submit=False, created_at=old)
        await db_session.commit()
        await _refresh_async(gamma=0.95, floor_c=0.1, tau=500.0, window_days=7,
                             session_factory=session_factory, rng=random.Random(0))
        a1 = (await self._state(db_session))[("USA", "pv1")]
        snap = (a1.alpha_param, a1.beta_param, a1.pulls)

        # Re-run immediately, no new alphas → window empty → g=1, no change.
        await _refresh_async(gamma=0.95, floor_c=0.1, tau=500.0, window_days=7,
                             session_factory=session_factory, rng=random.Random(1))
        a2 = (await self._state(db_session))[("USA", "pv1")]
        assert (a2.alpha_param, a2.beta_param, a2.pulls) == snap

    async def test_flag_off_task_noops(self, monkeypatch):
        # The celery entrypoint returns early (no DB touch) when flag OFF.
        from backend.config import settings
        from backend.tasks import dataset_weight_refresh as mod
        monkeypatch.setattr(settings, "ENABLE_DATASET_VALUE_BANDIT", False, raising=False)
        out = mod.run_dataset_weight_refresh()
        assert out.get("skipped_reason") == "flag_off"


# =============================================================================
# DatasetSelector._load_bandit_state round-trip (iteration-bug fix)
# =============================================================================
@pytest.mark.asyncio
async def test_load_bandit_state_populates_arms(db_session):
    from backend.dataset_selector import DatasetSelector
    from backend.models import BanditState, DatasetMetadata

    db_session.add(DatasetMetadata(
        dataset_id="pv1", region="USA", universe="TOP3000", name="pv1",
        mining_weight=1.0,
    ))
    db_session.add(BanditState(
        region="USA", dataset_id="pv1", pulls=2280, total_reward=1.0,
        sim_count_today=0, alpha_param=2.0, beta_param=2280.0,
        pulls_at_last_refresh=2280,
    ))
    await db_session.commit()

    sel = DatasetSelector(db_session)
    await sel.initialize(region="USA", universe="TOP3000")

    arm = next(a for a in sel.bandit.arms.values() if a.dataset_id == "pv1")
    # Pre-fix this stayed at defaults (the loader iterated dict keys → silent
    # AttributeError → never loaded; also wrote arm.pulls not total_pulls).
    assert arm.alpha_param == 2.0
    assert arm.beta_param == 2280.0
    assert arm.total_pulls == 2280
    assert arm.pulls_at_last_refresh == 2280
