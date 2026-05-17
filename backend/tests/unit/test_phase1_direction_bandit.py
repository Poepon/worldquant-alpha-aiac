"""Phase 1 R2/Q7 (2026-05-17) ContextualDirectionBandit unit tests.

Covers plan v1.3 §1.9 8 test cases + v1.3-specific defensive cases:
- MF-V1.3-5 reward clip [0,1]
- MF-V1.2-4 segment_id stability via string concat (NOT Python hash)
- Forward-compat arm rename silent skip
- last_select cache + consume invariant
- to_dict/from_dict roundtrip
"""
from __future__ import annotations

import random
from typing import List

import pytest

from backend.agents.evolution_strategy import (
    ContextualDirectionBandit,
    DEFAULT_BANDIT_ARMS,
    DEFAULT_COLD_THRESHOLD,
    DirectionArm,
    compute_arm_reward,
    segment_id,
)


# ---------------------------------------------------------------------------
# DirectionArm — Beta-Bernoulli primitive
# ---------------------------------------------------------------------------

class TestDirectionArm:
    def test_init_defaults_uniform_beta_1_1(self):
        a = DirectionArm(name="x")
        assert a.alpha == 1.0
        assert a.beta == 1.0
        assert a.total_pulls == 0
        assert a.total_reward == 0.0
        assert a.mean_reward == 0.5  # Beta(1,1) prior mean

    def test_update_clips_reward_to_unit_interval(self):
        # MF-V1.3-5 defensive clip — even if caller violates the [0,1] contract,
        # arm.update must not corrupt Beta posterior (alpha can never decrement)
        a = DirectionArm(name="x")
        a.update(1.85)  # would corrupt posterior if not clipped
        assert a.alpha == 2.0  # 1.0 + clip(1.85)=1.0
        assert a.beta == 1.0   # 1.0 + (1-1)=1.0

        b = DirectionArm(name="y")
        b.update(-0.3)  # clipped to 0
        assert b.alpha == 1.0
        assert b.beta == 2.0  # 1.0 + (1-0)=2.0

    def test_update_normal_reward_updates_both(self):
        a = DirectionArm(name="x")
        a.update(0.6)
        assert pytest.approx(a.alpha) == 1.6
        assert pytest.approx(a.beta) == 1.4
        assert a.total_pulls == 1
        assert pytest.approx(a.total_reward) == 0.6
        assert pytest.approx(a.mean_reward) == 0.6

    def test_sample_returns_unit_interval_value(self):
        a = DirectionArm(name="x", alpha=5.0, beta=2.0)
        for _ in range(100):
            s = a.sample()
            assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# segment_id — stable JSONB key (MF-V1.2-4)
# ---------------------------------------------------------------------------

class TestSegmentId:
    def test_uses_string_concat_not_python_hash(self):
        sid = segment_id(("USA", "pricevolume", "hypothesis"))
        assert sid == "USA|pricevolume|hypothesis"

    def test_case_sensitive(self):
        # Caller MUST normalize (region.upper(), category.lower()) BEFORE
        # passing — segment_id itself is case-sensitive to make the contract
        # explicit and detectable in tests.
        assert segment_id(("USA", "x", "y")) != segment_id(("usa", "x", "y"))

    def test_pipe_separator_stable(self):
        # Plan §1.5 design — keep separator as `|` so JSONB queries can split
        # cleanly without regex escape concerns
        sid = segment_id(("CHN", "fundamental", "implementation"))
        assert sid.count("|") == 2


# ---------------------------------------------------------------------------
# ContextualDirectionBandit — core state machine
# ---------------------------------------------------------------------------

class TestBanditSelectArm:
    @pytest.fixture(autouse=True)
    def _seed(self):
        random.seed(42)
        yield

    def test_uniform_select_on_cold_global_prior(self):
        # All arms start Beta(1,1), so over many samples each should win ~25%
        b = ContextualDirectionBandit()
        counts = {n: 0 for n in b.arm_names}
        for _ in range(2000):
            arm = b.select_arm(("USA", "x", "unknown"))
            counts[arm] += 1
            # IMPORTANT: do not update — keeps all arms cold
        for n, c in counts.items():
            pct = c / 2000
            # Beta(1,1) sampling is highly variable; allow ±10 ppts
            assert 0.15 <= pct <= 0.35, f"arm {n}: {pct:.2%}"

    def test_converges_to_high_reward_arm_within_segment(self):
        b = ContextualDirectionBandit()
        ctx = ("USA", "pv", "hypothesis")
        # Warm up segment past cold_threshold so segment-local prior dominates
        for _ in range(20):
            b.update(ctx, "rag_template", 0.9)
            b.update(ctx, "knowledge_pattern", 0.1)
            b.update(ctx, "llm_generation", 0.1)
            b.update(ctx, "genetic_mutation", 0.1)
        # After warm-up, posterior strongly favors rag_template
        counts = {n: 0 for n in b.arm_names}
        for _ in range(1500):
            arm = b.select_arm(ctx)
            counts[arm] += 1
        # Should be > 60% rag_template (loose bound for stochasticity)
        assert counts["rag_template"] / 1500 > 0.60

    def test_cold_segment_falls_back_to_global_prior(self):
        b = ContextualDirectionBandit()
        warm_ctx = ("USA", "pv", "hypothesis")
        cold_ctx = ("CHN", "fundamental", "implementation")
        # Make segment Y warm — and bias global prior toward rag_template
        for _ in range(20):
            b.update(warm_ctx, "rag_template", 0.95)
            b.update(warm_ctx, "knowledge_pattern", 0.05)
        # Segment X is cold (0 pulls). select_arm at X should use GLOBAL
        # prior which is dominated by warm_ctx's rag_template updates.
        assert b.is_cold_at(cold_ctx)
        counts = {n: 0 for n in b.arm_names}
        for _ in range(1500):
            arm = b.select_arm(cold_ctx)
            counts[arm] += 1
            b.last_select = None  # prevent any accidental update accumulation
        # Cold segment leaning to rag_template via global prior
        assert counts["rag_template"] / 1500 > 0.50

    def test_warm_segment_uses_local_not_global(self):
        b = ContextualDirectionBandit()
        ctx_a = ("USA", "pv", "hypothesis")
        ctx_b = ("CHN", "x", "y")
        # Warm segment B to favor "rag_template" globally
        for _ in range(20):
            b.update(ctx_b, "rag_template", 0.95)
        # Warm segment A locally to favor "genetic_mutation"
        for _ in range(20):
            b.update(ctx_a, "genetic_mutation", 0.9)
            b.update(ctx_a, "rag_template", 0.05)
        # A is now warm (>= cold_threshold), so local prior dominates
        assert not b.is_cold_at(ctx_a)
        counts = {n: 0 for n in b.arm_names}
        for _ in range(1500):
            arm = b.select_arm(ctx_a)
            counts[arm] += 1
        # Local arm wins despite global pulling toward rag_template
        assert counts["genetic_mutation"] / 1500 > 0.50

    def test_select_arm_caches_last_select(self):
        b = ContextualDirectionBandit()
        ctx = ("USA", "x", "unknown")
        arm = b.select_arm(ctx)
        assert b.last_select == (ctx, arm)


# ---------------------------------------------------------------------------
# update_last_round + forward-compat
# ---------------------------------------------------------------------------

class TestBanditUpdate:
    def test_update_last_round_applies_to_cached_ctx(self):
        b = ContextualDirectionBandit()
        ctx = ("USA", "x", "hypothesis")
        arm = b.select_arm(ctx)
        prior_total = b.global_arms[arm].total_pulls
        b.update_last_round(0.8)
        # last_select cleared
        assert b.last_select is None
        # both global and segment updated
        assert b.global_arms[arm].total_pulls == prior_total + 1
        seg = b.segments[segment_id(ctx)]
        assert seg[arm].total_pulls == 1

    def test_update_last_round_noop_without_prior_select(self):
        b = ContextualDirectionBandit()
        # No select_arm call → last_select is None
        result = b.update_last_round(0.5)
        assert result is None
        # Nothing should have been written
        for arm in b.global_arms.values():
            assert arm.total_pulls == 0

    def test_update_last_round_consumed_after_call(self):
        b = ContextualDirectionBandit()
        ctx = ("USA", "x", "y")
        b.select_arm(ctx)
        b.update_last_round(0.5)
        # Second call should be a no-op (last_select consumed)
        b.update_last_round(0.5)
        # Total pulls is exactly 1, not 2
        seg = b.segments[segment_id(ctx)]
        total = sum(a.total_pulls for a in seg.values())
        assert total == 1

    def test_update_unknown_arm_silently_skipped(self):
        # Forward-compat: Phase 2+ arm rename mid-task — caller may try to
        # apply reward for an arm that no longer exists. Must silently skip.
        b = ContextualDirectionBandit()
        b.update(("USA", "x", "y"), "renamed_arm_does_not_exist", 0.8)
        # No segment created, no global arms touched
        assert b.segments == {}
        for a in b.global_arms.values():
            assert a.total_pulls == 0


# ---------------------------------------------------------------------------
# Persistence — to_dict / from_dict roundtrip
# ---------------------------------------------------------------------------

class TestBanditRoundtrip:
    def test_empty_bandit_roundtrip(self):
        b = ContextualDirectionBandit()
        d = b.to_dict()
        b2 = ContextualDirectionBandit.from_dict(d)
        assert b2.arm_names == b.arm_names
        assert b2.cold_threshold == b.cold_threshold
        assert b2.segments == {}
        assert b2.last_select is None

    def test_warm_bandit_roundtrip_preserves_state(self):
        b = ContextualDirectionBandit()
        for _ in range(7):
            b.update(("USA", "pv", "hypothesis"), "rag_template", 0.7)
            b.update(("CHN", "fundamental", "implementation"), "genetic_mutation", 0.4)
        # Manually set last_select to a known value — select_arm is stochastic
        # (Thompson Sampling) so we can't deterministically assert which arm
        # wins. Roundtrip should preserve whatever was cached.
        b.last_select = (("USA", "pv", "hypothesis"), "rag_template")
        d = b.to_dict()
        b2 = ContextualDirectionBandit.from_dict(d)

        assert b2.global_arms["rag_template"].total_pulls == 7
        assert pytest.approx(b2.global_arms["rag_template"].total_reward) == 0.7 * 7
        assert b2.global_arms["genetic_mutation"].total_pulls == 7

        sid_a = segment_id(("USA", "pv", "hypothesis"))
        sid_b = segment_id(("CHN", "fundamental", "implementation"))
        assert sid_a in b2.segments
        assert sid_b in b2.segments
        assert b2.segments[sid_a]["rag_template"].total_pulls == 7
        assert b2.segments[sid_b]["genetic_mutation"].total_pulls == 7

        # last_select roundtrip — exact value preserved through to_dict/from_dict
        assert b2.last_select == (("USA", "pv", "hypothesis"), "rag_template")

    def test_roundtrip_with_renamed_arms_drops_obsolete(self):
        # If a future arm rename produces an old persisted state with an
        # extra arm, from_dict should ignore that arm gracefully
        b = ContextualDirectionBandit(arm_names=["a", "b"])
        b.update(("X", "Y", "Z"), "a", 0.5)
        d = b.to_dict()
        d["global_arms"]["legacy_arm_no_longer_exists"] = {
            "name": "legacy_arm_no_longer_exists",
            "alpha": 2.0, "beta": 3.0, "total_pulls": 1, "total_reward": 0.0,
        }
        b2 = ContextualDirectionBandit.from_dict(d)
        assert "legacy_arm_no_longer_exists" not in b2.global_arms
        assert "a" in b2.global_arms
        assert b2.global_arms["a"].total_pulls == 1


# ---------------------------------------------------------------------------
# compute_arm_reward — reward formula
# ---------------------------------------------------------------------------

class _FakeAlpha:
    def __init__(self, metrics: dict):
        self.metrics = metrics


class TestComputeArmReward:
    def test_empty_round_returns_zero(self):
        assert compute_arm_reward([]) == 0.0

    def test_reward_clipped_to_unit_interval(self):
        # MF-V1.3-5: huge sharpe should NOT produce reward > 1.0
        big = _FakeAlpha({"sharpe": 5.0, "fitness": 2.0, "composite_score": 1.0})
        r = compute_arm_reward([big])
        assert 0.0 <= r <= 1.0

    def test_reward_floor_at_zero(self):
        # Strong negative components shouldn't produce reward < 0
        bad = _FakeAlpha({"sharpe": 0, "fitness": 0,
                          "turnover": 1.0, "_self_corr": 1.0,
                          "composite_score": 0})
        r = compute_arm_reward([bad])
        assert r >= 0.0

    def test_reward_uses_5_dim_weighted_sum(self):
        # sharpe alone with weight 0.30 — 1.0 sharpe → 0.30 (within [0,1])
        a = _FakeAlpha({"sharpe": 1.0, "fitness": 0, "turnover": 0,
                        "_self_corr": 0, "composite_score": 0})
        r = compute_arm_reward([a])
        assert pytest.approx(r, abs=0.01) == 0.30

    def test_reward_averages_across_alphas(self):
        # Two alphas: r1 = 0.30 (sharpe=1), r2 = 0 (empty) → avg = 0.15
        a1 = _FakeAlpha({"sharpe": 1.0})
        a2 = _FakeAlpha({})
        r = compute_arm_reward([a1, a2])
        assert pytest.approx(r, abs=0.01) == 0.15

    def test_reward_with_missing_metrics_dict(self):
        # Alpha-like object with no metrics attr at all
        class NoMetrics:
            pass
        r = compute_arm_reward([NoMetrics()])
        assert r == 0.0


# ---------------------------------------------------------------------------
# build_context — async helper, mocked DB
# ---------------------------------------------------------------------------

class _FakeTask:
    """Minimal MiningTask-shaped fake for build_context tests."""
    def __init__(self, task_id=1, region="usa", target_datasets=None):
        self.id = task_id
        self.region = region
        self.target_datasets = target_datasets or []


class TestBuildContext:
    @pytest.mark.asyncio
    async def test_reads_region_top_level_not_config(self):
        # MF-V1.3-1: must NOT read task.config.get("regions")
        from backend.agents.evolution_strategy import build_context

        task = _FakeTask(region="chn")
        # db_factory returns a no-op async-cm that raises on use — proves
        # we don't query when there are no datasets and no task_id matches
        class _NoOpSession:
            async def __aenter__(self_):
                return self_
            async def __aexit__(self_, *a):
                return False
            async def execute(self_, q):
                class _R:
                    def scalar_one_or_none(_): return None
                    def scalars(_):
                        class _S:
                            def all(__): return []
                        return _S()
                return _R()
        def _factory():
            return _NoOpSession()

        ctx = await build_context(task, db_factory=_factory)
        assert ctx[0] == "CHN"             # upper-cased from "chn"
        assert ctx[1] == "other"           # no datasets → "other"
        assert ctx[2] == "unknown"         # no R1a rows → "unknown"

    @pytest.mark.asyncio
    async def test_dataset_category_normalized_lowercase(self):
        from backend.agents.evolution_strategy import build_context

        task = _FakeTask(target_datasets=["fnd6"])
        class _Session:
            def __init__(_, recent):
                _.recent = recent
                _._calls = 0
            async def __aenter__(_):
                return _
            async def __aexit__(_, *a):
                return False
            async def execute(_, q):
                _._calls += 1
                class _R:
                    def __init__(__, val): __._val = val
                    def scalar_one_or_none(__): return __._val
                    def scalars(__):
                        class _S:
                            def all(___): return _.recent
                        return _S()
                if _._calls == 1:
                    return _R("Fundamental6")
                return _R(None)
        def _factory():
            return _Session(["hypothesis", "hypothesis", "unknown"])

        ctx = await build_context(task, db_factory=_factory)
        assert ctx[0] == "USA"
        assert ctx[1] == "fundamental6"        # lowercased
        assert ctx[2] == "hypothesis"          # majority of 3 R1a rows

    @pytest.mark.asyncio
    async def test_failure_pattern_no_majority_returns_unknown(self):
        from backend.agents.evolution_strategy import build_context

        task = _FakeTask()
        class _Session:
            async def __aenter__(_): return _
            async def __aexit__(_, *a): return False
            async def execute(_, q):
                class _R:
                    def scalar_one_or_none(_): return None
                    def scalars(_):
                        class _S:
                            def all(__):
                                # 3 different values → tie → "unknown"
                                return ["hypothesis", "implementation", "both"]
                        return _S()
                return _R()
        def _factory(): return _Session()

        ctx = await build_context(task, db_factory=_factory)
        assert ctx[2] == "unknown"

    @pytest.mark.asyncio
    async def test_db_error_falls_through_safely(self):
        from backend.agents.evolution_strategy import build_context

        task = _FakeTask(target_datasets=["x"])
        class _BrokenSession:
            async def __aenter__(_): return _
            async def __aexit__(_, *a): return False
            async def execute(_, q):
                raise RuntimeError("DB down")
        def _factory(): return _BrokenSession()

        ctx = await build_context(task, db_factory=_factory)
        # All-soft-fail: returns sensible defaults instead of raising
        assert ctx == ("USA", "other", "unknown")


# ---------------------------------------------------------------------------
# Forward-compat invariants on persisted shape
# ---------------------------------------------------------------------------

class TestBanditForwardCompat:
    def test_default_arms_match_r1a_locked_set(self):
        # plan §4.2 hypothesis-dominant branch locks these — DO NOT CHANGE
        # without re-running R1a observation in Phase 2+
        assert DEFAULT_BANDIT_ARMS == (
            "rag_template",
            "knowledge_pattern",
            "llm_generation",
            "genetic_mutation",
        )

    def test_default_cold_threshold_is_5(self):
        # MF-V1.2-5: tuning point — too low oscillates, too high never warms
        assert DEFAULT_COLD_THRESHOLD == 5

    def test_to_dict_includes_schema_version(self):
        b = ContextualDirectionBandit()
        d = b.to_dict()
        assert d["v"] == 1, "schema version field required for Phase 2+ upgrade"
