"""
Integration tests for P1-D robustness gate inlined into node_evaluate.

Source: docs/alphagbm_skills_research_2026-05-15.md skill `pnl-simulator`.

Exercises:
- ENABLE_ROBUSTNESS_CHECK flag OFF passthrough (zero behavior change)
- stable → keeps PASS + `_robustness_passed=True`
- unstable → PASS → PASS_PROVISIONAL + `_robustness_failed=True`
  + `_skip_optimize_pool=True` (M-8) + KB filter (persistence.py) skip
- no-window expression → skipped, KB write still allowed
- round cap + quota pre-check + hot-check + per-alpha timeout
- CancelledError isolation (M-3)
- idempotent on already-stamped alphas
- `_routing_reason` not overwritten if graded-score set it first (M-6)
- trace counters populated
- dual-run-downgraded PROV ignored (PASS-only scope)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.agents.graph.nodes.evaluation import node_evaluate
from backend.agents.graph.state import AlphaCandidate, MiningState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_state(alphas: List[AlphaCandidate]) -> MiningState:
    """Build a minimal MiningState with the given alpha candidates."""
    return MiningState(
        task_id=1,
        region="USA",
        universe="TOP3000",
        dataset_id="ds1",
        pending_alphas=alphas,
        hypotheses=[],
        fields=[],
    )


def _mk_alpha(
    expression: str,
    sharpe: float,
    *,
    quality_status: str = "PASS",
    extra_metrics: Dict[str, Any] = None,
) -> AlphaCandidate:
    """Build an AlphaCandidate with PASS-y defaults so it survives node_evaluate's
    own gating logic before the robustness block runs.

    Provides realistic BRAIN checks (all PASS) + ``can_submit=True`` so the
    pre-existing gates (route_alpha_action / evaluate_with_brain_checks) keep
    quality_status='PASS' instead of downgrading to PROVISIONAL on
    "brain_checks_unverified".
    """
    metrics: Dict[str, Any] = {
        "sharpe": sharpe,
        "fitness": 1.1,
        "turnover": 0.25,
        "returns": 0.18,
        "drawdown": 0.05,
        # BRAIN-style checks list — all PASS so quality_status stays PASS.
        "checks": [
            {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": sharpe},
            {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.1},
            {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": 0.25},
            {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.25},
        ],
        "can_submit": True,
        "_sim_settings": {
            "region": "USA",
            "universe": "TOP3000",
            "delay": 1,
            "decay": 4,
            "neutralization": "INDUSTRY",
        },
    }
    if extra_metrics:
        metrics.update(extra_metrics)
    a = AlphaCandidate(
        expression=expression,
        is_simulated=True,
        simulation_success=True,
        alpha_id=f"mock-alpha-{abs(hash(expression)) % 10000}",
        metrics=metrics,
    )
    # quality_status is a writeable str field on AlphaCandidate (see state.py).
    a.quality_status = quality_status
    return a


def _sim_ok(sharpe: float, can_submit: bool = False) -> Dict[str, Any]:
    return {
        "success": True,
        "alpha_id": f"mock-{sharpe}",
        "metrics": {"sharpe": sharpe, "fitness": 0.7, "turnover": 0.3},
        "can_submit": can_submit,
    }


def _stable_brain() -> MagicMock:
    """BrainAdapter mock whose simulate_alpha always returns a 'stable' sharpe."""
    brain = MagicMock()
    brain.simulate_alpha = AsyncMock(side_effect=lambda **kw: _sim_ok(1.4))
    return brain


def _unstable_brain() -> MagicMock:
    """BrainAdapter mock whose simulate_alpha returns very weak sharpes (fail ratio)."""
    brain = MagicMock()
    brain.simulate_alpha = AsyncMock(side_effect=lambda **kw: _sim_ok(0.2))
    return brain


def _quota_ok() -> Dict[str, Any]:
    """Fake _quota_guard_async return — usage well under threshold."""
    return {
        "today_alpha_count": 100,
        "today_failure_count": 0,
        "today_total_count": 100,
        "threshold": 900,
        "limit": 1000,
        "paused_count": 0,
        "paused": [],
    }


def _quota_high(today_total: int = 700) -> Dict[str, Any]:
    return {
        "today_alpha_count": today_total,
        "today_failure_count": 0,
        "today_total_count": today_total,
        "threshold": 900,
        "limit": 1000,
        "paused_count": 0,
        "paused": [],
    }


class _FakeAsyncRedis:
    """In-memory mock of the subset of redis.asyncio we use.

    The robustness block constructs the client via ``redis.asyncio.from_url(...)``.
    We patch ``redis.asyncio.from_url`` to return one of these instead.
    """

    def __init__(self, initial: int = 0):
        self.store: Dict[str, str] = {}
        self.counter_value = initial
        self.incr_calls = 0
        self.expire_calls = 0
        self.closed = False

    async def get(self, key: str):
        if key == "aiac:robustness_today_used":
            return str(self.counter_value) if self.counter_value else None
        return self.store.get(key)

    async def incr(self, key: str):
        self.incr_calls += 1
        if key == "aiac:robustness_today_used":
            self.counter_value += 1
            return self.counter_value
        v = int(self.store.get(key, "0")) + 1
        self.store[key] = str(v)
        return v

    async def expire(self, key: str, ttl: int):
        self.expire_calls += 1
        return True

    async def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Patching helpers
# ---------------------------------------------------------------------------


def _patch_block(
    *,
    quota_payload: Dict[str, Any] = None,
    redis_client: _FakeAsyncRedis = None,
    settings_overrides: Dict[str, Any] = None,
):
    """Return list of context managers patching the robustness block dependencies."""
    if quota_payload is None:
        quota_payload = _quota_ok()
    if redis_client is None:
        redis_client = _FakeAsyncRedis()
    overrides = {
        "ENABLE_ROBUSTNESS_CHECK": True,
        "ROBUSTNESS_N_PERTURBATIONS": 4,
        "ROBUSTNESS_MIN_RATIO": 0.7,
        "MAX_ROBUSTNESS_PER_ROUND": 5,
        "ROBUSTNESS_SKIP_QUOTA_PCT": 0.65,
        "ROBUSTNESS_HOTCHECK_QUOTA_PCT": 0.85,
        "ROBUSTNESS_PER_ALPHA_TIMEOUT_SEC": 600,
        "ROBUSTNESS_SELECTION_STRATEGY": "first",
    }
    if settings_overrides:
        overrides.update(settings_overrides)

    patches = []
    for k, v in overrides.items():
        patches.append(patch(f"backend.config.settings.{k}", v, create=True))
    patches.append(
        patch(
            "backend.tasks.session_watchdog._quota_guard_async",
            AsyncMock(return_value=quota_payload),
        )
    )
    patches.append(
        patch("redis.asyncio.from_url", lambda *a, **kw: redis_client)
    )
    return patches, redis_client


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_passthrough():
    """Flag OFF → zero robustness sim calls, no stamps, trace counters all 0."""
    brain = MagicMock()
    brain.simulate_alpha = AsyncMock(side_effect=AssertionError("should not call"))
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])

    # ENABLE_ROBUSTNESS_CHECK default False
    result = await node_evaluate(state, brain=brain, config=None)
    brain.simulate_alpha.assert_not_called()
    alpha = result["pending_alphas"][0]
    assert "_robustness_passed" not in (alpha.metrics or {})
    assert "_robustness_skipped" not in (alpha.metrics or {})


@pytest.mark.asyncio
async def test_stable_alpha_keeps_pass():
    brain = _stable_brain()
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    alpha = result["pending_alphas"][0]
    assert alpha.quality_status == "PASS"
    assert alpha.metrics.get("_robustness_passed") is True
    assert alpha.metrics.get("_robustness_worst_ratio") is not None


@pytest.mark.asyncio
async def test_unstable_alpha_downgrades_with_skip_flags():
    """Unstable variants → PASS → PROV, stamps `_robustness_failed`,
    `_skip_optimize_pool=True`.

    Note: ``_routing_reason`` is M-6 ``setdefault`` so whatever previous gate
    (hard_gate_pass / graded_low_confidence) stamped first wins; robustness
    only stamps it if absent. The KB-filter / OPTIMIZE-filter only consults
    the explicit ``_robustness_failed`` / ``_skip_optimize_pool`` keys.
    """
    brain = _unstable_brain()
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    alpha = result["pending_alphas"][0]
    assert alpha.quality_status == "PASS_PROVISIONAL"
    assert alpha.metrics.get("_robustness_passed") is False
    assert alpha.metrics.get("_robustness_failed") is True
    assert alpha.metrics.get("_skip_optimize_pool") is True
    # `_routing_reason` always set (either by earlier gate or robustness fallback);
    # the explicit `_robustness_failed` flag is the authoritative KB-skip signal.
    assert alpha.metrics.get("_routing_reason") is not None


@pytest.mark.asyncio
async def test_no_window_alpha_passthrough_kb_writes():
    """Alpha with no window param → skipped (no_window), no `_robustness_failed`,
    KB write would still be allowed (not blocked by P1-D filter)."""
    brain = _stable_brain()
    state = _mk_state([_mk_alpha("rank(close)", 1.5)])
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    alpha = result["pending_alphas"][0]
    assert alpha.quality_status == "PASS"
    assert alpha.metrics.get("_robustness_skipped") == "no_window"
    assert "_robustness_failed" not in alpha.metrics
    # Brain wasn't called for variants (no_window short-circuits in gate.check)
    brain.simulate_alpha.assert_not_called()


@pytest.mark.asyncio
async def test_round_cap_5_of_6_checked():
    """6 PASS alphas, cap=5 → top-5 by sharpe checked, 6th gets `round_cap`.

    All 6 sharpe values >= sharpe_min(1.25) so _evaluate_single_alpha doesn't
    pre-route any to PROV; the cap is enforced strictly by the robustness block.
    """
    brain = _stable_brain()
    # Sharpe values 1.50, 1.45, 1.40, 1.35, 1.30, 1.27 — all above hard-gate (1.25).
    sharpes = [1.50, 1.45, 1.40, 1.35, 1.30, 1.27]
    # Vary expression so AlphaCandidate fingerprints don't collide.
    alphas = [
        _mk_alpha(f"ts_rank(close, {22 + i})", s)
        for i, s in enumerate(sharpes)
    ]
    state = _mk_state(alphas)
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()

    checked = [
        a for a in result["pending_alphas"]
        if a.metrics.get("_robustness_passed") is True
    ]
    cap_skipped = [
        a for a in result["pending_alphas"]
        if a.metrics.get("_robustness_skipped") == "round_cap"
    ]
    assert len(checked) == 5, (
        f"checked={[a.metrics.get('sharpe') for a in checked]} "
        f"cap_skipped={[a.metrics.get('sharpe') for a in cap_skipped]}"
    )
    assert len(cap_skipped) == 1
    # Lowest-sharpe one got the cap skip
    assert cap_skipped[0].metrics["sharpe"] == pytest.approx(1.27, abs=1e-3)


@pytest.mark.asyncio
async def test_quota_exhausted_skips_round():
    """today_total + redis_extra >= 0.65 * limit → ALL alphas marked quota_exhausted."""
    brain = MagicMock()
    brain.simulate_alpha = AsyncMock(side_effect=AssertionError("should not call"))
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])
    # today=700 limit=1000 → 0.70 >= 0.65 → block
    patches, _redis = _patch_block(quota_payload=_quota_high(700))
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    alpha = result["pending_alphas"][0]
    assert alpha.metrics.get("_robustness_skipped") == "quota_exhausted"
    brain.simulate_alpha.assert_not_called()


@pytest.mark.asyncio
async def test_quota_hot_check_skips_remaining():
    """First alpha completes; redis counter rises; hot-check fires for 2nd.

    Setup:  today_total=0, limit=1000.  After 1st alpha: extra=4 → pct=0.004.
    Set hot_pct=0.003 so:
       alpha 1 hot-check (extra=0) → pct=0.0 < 0.003 → run (4 sims → extra=4)
       alpha 2 hot-check (extra=4) → pct=0.004 >= 0.003 → skip
    """
    brain = _stable_brain()
    alphas = [
        _mk_alpha("ts_rank(close, 22)", 1.5),
        _mk_alpha("ts_zscore(returns, 60)", 1.4),
    ]
    state = _mk_state(alphas)
    patches, _redis = _patch_block(
        quota_payload={
            "today_alpha_count": 0,
            "today_failure_count": 0,
            "today_total_count": 0,
            "threshold": 900,
            "limit": 1000,
            "paused_count": 0,
            "paused": [],
        },
        redis_client=_FakeAsyncRedis(initial=0),
        settings_overrides={
            "ROBUSTNESS_SKIP_QUOTA_PCT": 0.99,
            "ROBUSTNESS_HOTCHECK_QUOTA_PCT": 0.003,
        },
    )
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()

    by_sharpe = sorted(
        result["pending_alphas"], key=lambda a: a.metrics["sharpe"], reverse=True
    )
    assert by_sharpe[0].metrics.get("_robustness_passed") is True
    assert by_sharpe[1].metrics.get("_robustness_skipped") == "quota_exhausted"


@pytest.mark.asyncio
async def test_per_alpha_timeout_does_not_downgrade():
    """gate.check exceeds ROBUSTNESS_PER_ALPHA_TIMEOUT_SEC → skip, keep PASS."""
    # Stable brain — but make simulate_alpha sleep way longer than the timeout.
    brain = MagicMock()

    async def _slow(**kw):
        await asyncio.sleep(2.0)  # longer than our 0.1s timeout below
        return _sim_ok(1.4)

    brain.simulate_alpha = AsyncMock(side_effect=_slow)
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])
    patches, _redis = _patch_block(
        settings_overrides={"ROBUSTNESS_PER_ALPHA_TIMEOUT_SEC": 0.1},
    )
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    alpha = result["pending_alphas"][0]
    assert alpha.metrics.get("_robustness_skipped") == "per_alpha_timeout"
    assert alpha.quality_status == "PASS"  # NOT downgraded
    assert "_robustness_failed" not in alpha.metrics


@pytest.mark.asyncio
async def test_cancelled_error_isolated():
    """One variant raises CancelledError → only that variant's sim counts as failed."""
    # 4 calls: 1st raises CancelledError, others succeed.
    call_state = {"n": 0}

    async def _sim(**kw):
        call_state["n"] += 1
        if call_state["n"] == 1:
            raise asyncio.CancelledError("simulated cancel")
        return _sim_ok(1.4)

    brain = MagicMock()
    brain.simulate_alpha = AsyncMock(side_effect=_sim)
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    alpha = result["pending_alphas"][0]
    # 3 successful sims → result.passed True
    assert alpha.metrics.get("_robustness_passed") is True
    assert alpha.metrics.get("_robustness_n_run") == 3


@pytest.mark.asyncio
async def test_already_stamped_idempotent():
    """alpha.metrics already has `_robustness_passed=True` → block skips it."""
    brain = MagicMock()
    brain.simulate_alpha = AsyncMock(side_effect=AssertionError("should not call"))
    alpha = _mk_alpha(
        "ts_rank(close, 22)", 1.5,
        extra_metrics={"_robustness_passed": True, "_robustness_worst_ratio": 0.9},
    )
    state = _mk_state([alpha])
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    a = result["pending_alphas"][0]
    # Stamp preserved
    assert a.metrics.get("_robustness_passed") is True
    assert a.metrics.get("_robustness_worst_ratio") == 0.9
    brain.simulate_alpha.assert_not_called()


@pytest.mark.asyncio
async def test_routing_reason_not_overwritten():
    """M-6: ``_routing_reason`` stamped by an earlier gate (here:
    hard_gate_pass from route_alpha_action) survives a robustness downgrade.

    The downgrade uses ``metrics.setdefault(...)`` so it never clobbers an
    existing reason — important because graded-score / dual-run / hard-gate
    routing may already have committed an audit-grade reason. The KB filter
    relies on the explicit ``_robustness_failed`` key, not on the reason
    string.
    """
    brain = _unstable_brain()
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    a = result["pending_alphas"][0]
    assert a.quality_status == "PASS_PROVISIONAL"
    # Pre-existing reason (hard_gate_pass, set by route_alpha_action above
    # the robustness block) preserved — robustness did NOT clobber it.
    assert a.metrics["_routing_reason"] != "robustness_downgrade"
    # but robustness still stamps the explicit gate keys
    assert a.metrics.get("_robustness_failed") is True
    assert a.metrics.get("_skip_optimize_pool") is True


@pytest.mark.asyncio
async def test_trace_counters_populated():
    """record_trace output_data must include the 9 robustness counters."""
    brain = _stable_brain()
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])

    # Capture trace output by intercepting trace_service hook (not strictly
    # needed — we instead inspect the returned dict's trace_steps).
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()

    # node_evaluate's return dict carries trace_steps via the trace_update merge.
    trace_steps = result.get("trace_steps") or []
    assert trace_steps, "trace_steps should contain EVALUATE step"
    evaluate_step = trace_steps[-1]
    # TraceStepData may be a dict / pydantic model — read defensively
    output = (
        evaluate_step.output_data
        if hasattr(evaluate_step, "output_data")
        else evaluate_step.get("output_data", {})
    ) or {}
    for key in (
        "robustness_attempted",
        "robustness_passed",
        "robustness_failed_downgrade",
        "robustness_skipped_no_window",
        "robustness_skipped_quota",
        "robustness_skipped_round_cap",
        "robustness_skipped_timeout",
        "robustness_skipped_other",
        "robustness_sim_failed_total",
    ):
        assert key in output, f"trace output missing {key}: {sorted(output.keys())}"


@pytest.mark.asyncio
async def test_prov_alpha_skipped_scope_pass_only():
    """PROV alphas (scope=PASS only) are NOT checked by robustness gate.

    Use an alpha with `LOW_TURNOVER`+ low sharpe that ``_evaluate_single_alpha``
    will naturally route to PROVISIONAL (so the scope filter on the robustness
    block leaves it alone). brain.simulate_alpha must not be called.
    """
    brain = MagicMock()
    brain.simulate_alpha = AsyncMock(side_effect=AssertionError("should not call"))
    # Build an alpha that node_evaluate routes to PROV via near-pass path:
    # sharpe below sharpe_min (1.25) but score still triggers near_pass.
    a = _mk_alpha("ts_rank(close, 22)", 0.8)
    # Override the checks so HIGH_TURNOVER fails (turnover above limit) — this
    # forces the near_pass / PROV path instead of PASS.
    a.metrics["turnover"] = 0.78
    a.metrics["checks"] = [
        {"name": "LOW_SHARPE", "result": "FAIL", "limit": 1.25, "value": 0.8},
        {"name": "HIGH_TURNOVER", "result": "FAIL", "limit": 0.7, "value": 0.78},
    ]
    a.metrics["can_submit"] = False
    state = _mk_state([a])
    patches, _redis = _patch_block()
    for p in patches:
        p.start()
    try:
        result = await node_evaluate(state, brain=brain, config=None)
    finally:
        for p in reversed(patches):
            p.stop()
    out_a = result["pending_alphas"][0]
    # Whatever non-PASS status node_evaluate landed on, robustness block must
    # have left the alpha alone.
    assert out_a.quality_status != "PASS"
    assert "_robustness_passed" not in out_a.metrics
    assert "_robustness_skipped" not in out_a.metrics
    brain.simulate_alpha.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_zero_trace_drift():
    """Flag OFF → trace counters all 0 (baseline.json regression invariant)."""
    brain = MagicMock()
    brain.simulate_alpha = AsyncMock(side_effect=AssertionError("should not call"))
    state = _mk_state([_mk_alpha("ts_rank(close, 22)", 1.5)])
    result = await node_evaluate(state, brain=brain, config=None)
    trace_steps = result.get("trace_steps") or []
    output = (
        trace_steps[-1].output_data
        if hasattr(trace_steps[-1], "output_data")
        else trace_steps[-1].get("output_data", {})
    ) or {}
    for key in (
        "robustness_attempted",
        "robustness_passed",
        "robustness_failed_downgrade",
        "robustness_skipped_no_window",
        "robustness_skipped_quota",
        "robustness_skipped_round_cap",
        "robustness_skipped_timeout",
        "robustness_skipped_other",
        "robustness_sim_failed_total",
    ):
        assert output.get(key, 0) == 0, f"counter {key} should be 0 when flag OFF"
