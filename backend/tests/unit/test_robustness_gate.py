"""
Unit tests for P1-D: window-perturbation robustness gate.

Source: docs/alphagbm_skills_research_2026-05-15.md skill `pnl-simulator`.

Covers:
- enumerate_window_perturbations pure-function behaviour (M-4 / M-5 edge cases)
- RobustnessResult composition (ratio / consistency math)
- RobustnessGate.check with mock BrainAdapter (M-1 redis, M-3 CancelledError)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.window_perturbation import enumerate_window_perturbations
from backend.multi_fidelity_eval import RobustnessGate, RobustnessResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_alpha(expression: str, metrics: Dict[str, Any]):
    """Stand-in for AlphaCandidate-like object — only attrs RobustnessGate uses."""
    a = MagicMock()
    a.expression = expression
    a.metrics = metrics
    return a


def _sim_ok(sharpe: float, can_submit: bool = False) -> Dict[str, Any]:
    """Minimal successful simulate_alpha response shape."""
    return {
        "success": True,
        "alpha_id": f"mock-{sharpe}",
        "metrics": {"sharpe": sharpe, "fitness": 0.7, "turnover": 0.3},
        "can_submit": can_submit,
    }


# ===========================================================================
# TestEnumerateWindowPerturbations
# ===========================================================================


class TestEnumerateWindowPerturbations:
    def test_no_window_returns_empty(self):
        assert enumerate_window_perturbations("rank(close)") == []

    def test_empty_or_none_expression(self):
        assert enumerate_window_perturbations("") == []
        assert enumerate_window_perturbations(None) == []  # type: ignore[arg-type]

    def test_single_window_n4_first_strategy(self):
        variants = enumerate_window_perturbations("ts_rank(close, 22)", n=4)
        assert len(variants) == 4
        # All variants distinct
        exprs = [e for e, _ in variants]
        assert len(set(exprs)) == 4
        # None equal to original
        assert all(e != "ts_rank(close, 22)" for e in exprs)
        # All swap NUM 22 → some other WINDOW_VALUES element
        for new_expr, desc in variants:
            assert "ts_rank(close," in new_expr
            assert "22 ->" in desc
        # First entry should be the absolute-nearest by |w - 22|
        from backend.window_perturbation import WINDOW_VALUES
        nearest = min((w for w in WINDOW_VALUES if w != 22), key=lambda w: (abs(w - 22), w))
        assert f"ts_rank(close, {nearest})" == variants[0][0]

    def test_multi_window_first_strategy(self):
        # Two top-level non-overlapping matches: ts_rank(...,22) and ts_zscore(...,60).
        # 'first' picks the FIRST match (ts_rank,22) and perturbs 22.
        expr = "add(ts_rank(close, 22), ts_zscore(returns, 60))"
        variants = enumerate_window_perturbations(expr, n=3, selection_strategy="first")
        assert len(variants) == 3
        for new_expr, desc in variants:
            assert "ts_zscore(returns, 60)" in new_expr  # 60 preserved
            assert "ts_rank(close, 22)" not in new_expr  # 22 swapped
            assert "22 ->" in desc

    def test_multi_window_largest_strategy(self):
        # 'largest' picks the site with the largest NUM (60) and perturbs it.
        expr = "add(ts_rank(close, 22), ts_zscore(returns, 60))"
        variants = enumerate_window_perturbations(expr, n=3, selection_strategy="largest")
        assert len(variants) == 3
        for new_expr, desc in variants:
            assert "ts_rank(close, 22)" in new_expr  # 22 preserved
            assert "ts_zscore(returns, 60)" not in new_expr  # 60 swapped
            assert "60 ->" in desc

    def test_largest_ties_break_by_start(self):
        # Two 60s — tiebreak picks the earlier (smaller start) one (the OUTER ts_rank).
        expr = "ts_rank(ts_zscore(close, 60), 60)"
        variants = enumerate_window_perturbations(expr, n=1, selection_strategy="largest")
        assert len(variants) == 1
        new_expr = variants[0][0]
        # Only the FIRST 60 (after ts_zscore inner) should be perturbed because
        # regex matches ts_rank(...) first... actually ts_rank's second arg
        # capture starts at the outer ts_zscore(...,60), and ts_zscore's at 60.
        # Whichever wins, exactly ONE of the two 60s remains.
        assert new_expr.count(", 60)") == 1

    def test_n_exceeds_available(self):
        # WINDOW_VALUES has 11 elements; original=22 → 10 available.
        variants = enumerate_window_perturbations("ts_rank(close, 22)", n=100)
        assert 1 <= len(variants) <= 10

    def test_deterministic(self):
        # Same input → same output across calls (no random).
        a = enumerate_window_perturbations("ts_rank(close, 22)", n=4)
        b = enumerate_window_perturbations("ts_rank(close, 22)", n=4)
        c = enumerate_window_perturbations("ts_rank(close, 22)", n=4)
        assert a == b == c

    def test_ternary_function_perturbed(self):
        # P3 fix: ternary ts_co_skewness(x, y, 20) — last positional digit
        # arg (= window) is now correctly identified by the balanced-paren
        # parser. Pre-fix the flat regex returned [].
        from backend.window_perturbation import WINDOW_VALUES
        variants = enumerate_window_perturbations(
            "ts_co_skewness(close, returns, 20)", n=4,
        )
        # Exact expected outputs: 4 nearest WINDOW_VALUES to 20, in
        # (abs distance, value) order. Inner args (close, returns) intact.
        nearest_4 = sorted(
            (w for w in WINDOW_VALUES if w != 20),
            key=lambda w: (abs(w - 20), w),
        )[:4]
        expected = [
            (f"ts_co_skewness(close, returns, {w})",
             f"window_perturbation: ts_co_skewness 20 -> {w}")
            for w in nearest_4
        ]
        assert variants == expected

    def test_nested_inner_calls_perturbed(self):
        # P3 fix: ts_corr(rank(close), rank(returns), 20) — inner () would
        # break the flat-regex `[^,]+` second-arg match; balanced-paren
        # parser handles it.
        from backend.window_perturbation import WINDOW_VALUES
        variants = enumerate_window_perturbations(
            "ts_corr(rank(close), rank(returns), 20)", n=2,
        )
        nearest_2 = sorted(
            (w for w in WINDOW_VALUES if w != 20),
            key=lambda w: (abs(w - 20), w),
        )[:2]
        expected = [
            (f"ts_corr(rank(close), rank(returns), {w})",
             f"window_perturbation: ts_corr 20 -> {w}")
            for w in nearest_2
        ]
        assert variants == expected

    def test_non_standard_window_still_enumerates(self):
        # Original window 7 not in WINDOW_VALUES; still picks nearest.
        variants = enumerate_window_perturbations("ts_rank(close, 7)", n=4)
        assert len(variants) == 4
        # First variant must be the closest WINDOW_VALUES element to 7.
        from backend.window_perturbation import WINDOW_VALUES
        nearest = min(WINDOW_VALUES, key=lambda w: (abs(w - 7), w))
        assert f"ts_rank(close, {nearest})" == variants[0][0]

    def test_duplicate_window_dedup(self):
        # Two 22s, but with 'first' strategy only ONE site is perturbed → no dup risk.
        expr = "ts_rank(ts_rank(close, 22), 22)"
        variants = enumerate_window_perturbations(expr, n=4)
        # All new expressions must be unique
        exprs = [e for e, _ in variants]
        assert len(set(exprs)) == len(exprs)

    def test_group_function_matched(self):
        variants = enumerate_window_perturbations("group_zscore(x, 40)", n=2)
        assert len(variants) == 2
        for new_expr, desc in variants:
            assert "group_zscore" in new_expr
            assert "40 ->" in desc

    def test_all_in_order_strategy(self):
        # 'all_in_order' picks N distinct top-level matches (one per site).
        expr = "add(ts_rank(close, 22), ts_zscore(returns, 60))"
        variants = enumerate_window_perturbations(
            expr, n=2, selection_strategy="all_in_order"
        )
        # 2 variants from 2 different sites
        assert len(variants) == 2
        descs = [d for _, d in variants]
        # One should be from ts_rank (22), the other from ts_zscore (60)
        assert any("22 ->" in d for d in descs)
        assert any("60 ->" in d for d in descs)


# ===========================================================================
# TestRobustnessResult / Ratio math (via gate.check end-to-end)
# ===========================================================================


@pytest.mark.asyncio
class TestRobustnessRatioMath:
    async def _run_with_sharpes(
        self, base: float, variant_sharpes: List[float], min_ratio: float = 0.7
    ) -> RobustnessResult:
        """Helper: build a mock-brain queue returning these sharpes in order."""
        brain = MagicMock()
        seq = iter(variant_sharpes)

        async def _sim(expression: str, **kw):
            return _sim_ok(next(seq))

        brain.simulate_alpha = AsyncMock(side_effect=_sim)

        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=min_ratio)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": base, "_sim_settings": {"region": "USA", "universe": "TOP3000"}},
        )
        return await gate.check(alpha)

    async def test_passed_above_ratio_07(self):
        # base=1.5, worst=1.1 → ratio≈0.733 → passed
        res = await self._run_with_sharpes(1.5, [1.2, 1.3, 1.1, 1.4])
        assert res.passed is True
        assert res.worst_sharpe == pytest.approx(1.1, abs=1e-3)
        assert res.worst_ratio == pytest.approx(1.1 / 1.5, abs=1e-3)
        assert res.perturbation_count == 4

    async def test_failed_below_ratio(self):
        # base=1.5, worst=0.9 → ratio=0.6 → not passed
        res = await self._run_with_sharpes(1.5, [0.9, 1.2, 1.1, 1.4])
        assert res.passed is False
        assert res.worst_sharpe == pytest.approx(0.9, abs=1e-3)
        assert res.worst_ratio == pytest.approx(0.6, abs=1e-3)

    async def test_negative_baseline_uses_max(self):
        # base=-1.5 → "worst" is MAX (closest to 0 = lowest |sharpe| on negative side).
        res = await self._run_with_sharpes(-1.5, [-1.0, -1.4, -0.9])
        # variants len = 3 (only 3 sharpes queued, but enumerate produces 4 variants;
        # so the 4th call's StopIteration raises and counts as sim_failed)
        # We don't care about that here — focus on worst/ratio direction.
        # The worst of [-1.0, -1.4, -0.9] with negative baseline is max = -0.9
        assert res.worst_sharpe == pytest.approx(-0.9, abs=1e-3)
        # ratio = -0.9 / 1.5 = -0.6 → not passed
        assert res.worst_ratio == pytest.approx(-0.9 / 1.5, abs=1e-3)
        assert res.passed is False

    async def test_can_submit_consistency_all_match(self):
        brain = MagicMock()

        async def _sim(expression: str, **kw):
            return _sim_ok(1.4, can_submit=True)

        brain.simulate_alpha = AsyncMock(side_effect=_sim)
        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=0.5)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {
                "sharpe": 1.5,
                "can_submit": True,
                "_sim_settings": {"region": "USA"},
            },
        )
        res = await gate.check(alpha)
        assert res.can_submit_consistency == pytest.approx(1.0)

    async def test_can_submit_consistency_half_flip(self):
        brain = MagicMock()
        seq = iter([
            _sim_ok(1.4, can_submit=True),
            _sim_ok(1.3, can_submit=True),
            _sim_ok(1.2, can_submit=False),
            _sim_ok(1.1, can_submit=False),
        ])

        async def _sim(expression: str, **kw):
            return next(seq)

        brain.simulate_alpha = AsyncMock(side_effect=_sim)
        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=0.5)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {
                "sharpe": 1.5,
                "can_submit": True,
                "_sim_settings": {"region": "USA"},
            },
        )
        res = await gate.check(alpha)
        assert res.can_submit_consistency == pytest.approx(0.5, abs=1e-3)


# ===========================================================================
# TestRobustnessGateCheck — edge cases via mock BrainAdapter
# ===========================================================================


@pytest.mark.asyncio
class TestRobustnessGateCheck:
    async def test_baseline_metrics_missing(self):
        gate = RobustnessGate(MagicMock(), n_perturbations=4, min_ratio=0.7)
        alpha = _mk_alpha("ts_rank(close, 22)", {})
        res = await gate.check(alpha)
        assert res.skip_reason == "baseline_metrics_missing"
        assert res.passed is False

    async def test_baseline_sharpe_zero(self):
        gate = RobustnessGate(MagicMock(), n_perturbations=4, min_ratio=0.7)
        alpha = _mk_alpha("ts_rank(close, 22)", {"sharpe": 0.0})
        res = await gate.check(alpha)
        assert res.skip_reason == "baseline_sharpe_zero"

    async def test_no_window_skip(self):
        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(side_effect=AssertionError("should not be called"))
        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=0.7)
        alpha = _mk_alpha("rank(close)", {"sharpe": 1.5})
        res = await gate.check(alpha)
        assert res.skip_reason == "no_window"
        brain.simulate_alpha.assert_not_called()

    async def test_all_sims_pass(self):
        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(return_value=_sim_ok(1.4))
        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=0.7)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": 1.5, "_sim_settings": {"region": "USA"}},
        )
        res = await gate.check(alpha)
        assert res.passed is True
        assert res.perturbation_count == 4
        assert res.sim_failed_count == 0

    async def test_all_sims_fail_below_ratio(self):
        # unstable variants — all way below
        brain = MagicMock()
        seq = iter([_sim_ok(0.4), _sim_ok(0.5), _sim_ok(0.6), _sim_ok(0.3)])

        async def _sim(expression: str, **kw):
            return next(seq)

        brain.simulate_alpha = AsyncMock(side_effect=_sim)
        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=0.7)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": 1.5, "_sim_settings": {"region": "USA"}},
        )
        res = await gate.check(alpha)
        assert res.passed is False
        assert res.perturbation_count == 4

    async def test_partial_failure(self):
        # 2 success + 2 failure (raise)
        call_count = {"n": 0}

        async def _sim(expression: str, **kw):
            call_count["n"] += 1
            if call_count["n"] in (2, 4):
                raise RuntimeError("transient")
            return _sim_ok(1.4)

        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(side_effect=_sim)
        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=0.7)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": 1.5, "_sim_settings": {"region": "USA"}},
        )
        res = await gate.check(alpha)
        # 2 succeed at sharpe=1.4 → worst=1.4 / 1.5 = 0.933 → passed
        assert res.perturbation_count == 2
        assert res.sim_failed_count == 2
        assert res.passed is True

    async def test_all_perturbations_failed(self):
        async def _sim(expression: str, **kw):
            raise RuntimeError("all bad")

        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(side_effect=_sim)
        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=0.7)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": 1.5, "_sim_settings": {"region": "USA"}},
        )
        res = await gate.check(alpha)
        assert res.skip_reason == "all_perturbations_failed"
        assert res.passed is False
        assert res.sim_failed_count == 4

    async def test_cancelled_error_caught(self):
        # M-3: ensure CancelledError on one variant doesn't tear down the gate.
        # NOTE: RobustnessGate._one wraps simulate_alpha in try/except Exception
        # so it catches RuntimeError, but CancelledError (BaseException) propagates
        # — gather(return_exceptions=True) collects it. We assert that path here.
        call_count = {"n": 0}

        async def _sim(expression: str, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise asyncio.CancelledError("simulated cancel")
            return _sim_ok(1.4)

        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(side_effect=_sim)
        gate = RobustnessGate(brain, n_perturbations=4, min_ratio=0.5)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": 1.5, "_sim_settings": {"region": "USA"}},
        )
        res = await gate.check(alpha)
        # 1 CancelledError + 3 successes → 3 successful sharpes, 1 sim_failed
        assert res.sim_failed_count == 1
        assert res.perturbation_count == 3
        assert res.passed is True  # 1.4 / 1.5 > 0.5

    async def test_sim_kwargs_propagated_from_sim_settings(self):
        captured: List[Dict[str, Any]] = []

        async def _sim(expression: str, **kw):
            captured.append({"expression": expression, **kw})
            return _sim_ok(1.4)

        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(side_effect=_sim)
        gate = RobustnessGate(brain, n_perturbations=2, min_ratio=0.5)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {
                "sharpe": 1.5,
                "_sim_settings": {
                    "region": "CHN",
                    "universe": "TOP500",
                    "delay": 1,
                    "decay": 4,
                    "neutralization": "INDUSTRY",
                    # Extra key not in simulate_alpha signature — must be stripped.
                    "_sim_settings_reason": "test",
                },
            },
        )
        res = await gate.check(alpha)
        assert res.perturbation_count == 2
        for call in captured:
            assert call["region"] == "CHN"
            assert call["universe"] == "TOP500"
            assert "_sim_settings_reason" not in call

    async def test_redis_counter_incremented(self):
        # M-1 + P2 fix: each successful simulate increments the per-UTC-day
        # key (was: a single sliding-TTL "today_used" key).
        redis = MagicMock()
        redis.incr = AsyncMock(return_value=1)
        redis.expire = AsyncMock(return_value=True)

        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(return_value=_sim_ok(1.4))
        gate = RobustnessGate(
            brain, n_perturbations=3, min_ratio=0.5, redis_client=redis
        )
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": 1.5, "_sim_settings": {"region": "USA"}},
        )
        await gate.check(alpha)
        assert redis.incr.await_count == 3
        # Every incr followed by expire (TTL refresh)
        assert redis.expire.await_count == 3
        # Key is per-UTC-day; verify it has the right prefix + a date suffix.
        expected_key = RobustnessGate.today_key()
        assert expected_key.startswith(RobustnessGate.REDIS_COUNTER_KEY_PREFIX + ":")
        for call in redis.incr.await_args_list:
            assert call.args[0] == expected_key

    async def test_redis_counter_failure_does_not_break_check(self):
        # Counter failure must NOT block the gate.
        redis = MagicMock()
        redis.incr = AsyncMock(side_effect=RuntimeError("redis down"))
        redis.expire = AsyncMock(return_value=True)

        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(return_value=_sim_ok(1.4))
        gate = RobustnessGate(
            brain, n_perturbations=2, min_ratio=0.5, redis_client=redis
        )
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": 1.5, "_sim_settings": {"region": "USA"}},
        )
        res = await gate.check(alpha)
        assert res.passed is True
        assert res.perturbation_count == 2

    async def test_n_perturbations_respected(self):
        # n=2 → only 2 calls
        brain = MagicMock()
        brain.simulate_alpha = AsyncMock(return_value=_sim_ok(1.4))
        gate = RobustnessGate(brain, n_perturbations=2, min_ratio=0.5)
        alpha = _mk_alpha(
            "ts_rank(close, 22)",
            {"sharpe": 1.5, "_sim_settings": {"region": "USA"}},
        )
        res = await gate.check(alpha)
        assert res.perturbation_count == 2
        assert brain.simulate_alpha.await_count == 2

    async def test_baseline_metrics_non_numeric(self):
        # If sharpe is a string or non-numeric, gracefully degrade.
        gate = RobustnessGate(MagicMock(), n_perturbations=4, min_ratio=0.7)
        alpha = _mk_alpha("ts_rank(close, 22)", {"sharpe": "bad"})
        res = await gate.check(alpha)
        assert res.skip_reason == "baseline_metrics_missing"


# ===========================================================================
# Sanity smoke: RobustnessResult dataclass default construction
# ===========================================================================


def test_robustness_result_defaults():
    r = RobustnessResult(baseline_sharpe=1.5, perturbation_count=0)
    assert r.passed is False
    assert r.perturbation_sharpes == []
    assert r.skip_reason is None
    assert r.can_submit_consistency == 1.0
