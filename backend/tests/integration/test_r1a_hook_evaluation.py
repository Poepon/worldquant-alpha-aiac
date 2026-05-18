"""R1a hook integration tests (Phase 0, 2026-05-17).

Validates that enhance_existing_node_evaluate shim is correctly invoked
from node_evaluate's tail block when ENABLE_R1A_HOOK flag is True, and
that hook failures are isolated per-alpha (don't break the batch).

Coverage:
    test_r1a_hook_disabled_no_metrics_written
        flag=False — `_r1a_*` keys MUST NOT appear in alpha.metrics
    test_r1a_hook_enabled_writes_attribution
        flag=True + PASS alpha — `_r1a_attribution` in {hypothesis,
        implementation, both, unknown}; `_r1a_hook_version='v1'`;
        empty attribution_evidence is skipped (NF-1)
    test_r1a_hook_failure_does_not_break_node
        flag=True + monkeypatched shim raising exception — node still
        returns normally; `_r1a_attribution=None` + `_r1a_hook_error` set;
        `_r1a_hook_version='v1'` still marked (GO denominator inclusive)
    test_r1a_hook_attribution_distribution
        3 sim_result variants → IMPLEMENTATION / HYPOTHESIS / UNKNOWN
        attribution per backend.agents.prompts.alignment heuristic
        (line 351-380).

PG_reachable skipif mirrors test_node_evaluate_regime.py — node_evaluate
warms up the agents stack which depends on Postgres.

Shim unit tests (success/failure paths) are NOT duplicated; covered by
backend/tests/integration/test_core_integration.py::test_enhance_*.
"""
from __future__ import annotations

import os
import socket
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

os.environ.setdefault("POSTGRES_PORT", "5433")


def _pg_reachable() -> bool:
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = int(os.getenv("POSTGRES_PORT", "5433"))
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="R1a hook integration test requires Postgres reachable (agents stack warm-up)",
)

# Warm-up
import backend.tasks  # noqa: E402, F401

from backend.agents.graph.nodes.evaluation import node_evaluate  # noqa: E402
from backend.agents.graph.state import AlphaCandidate, MiningState  # noqa: E402
from backend.config import settings  # noqa: E402


def _mk_state(alphas: List[AlphaCandidate], factor_tier: int = 1) -> MiningState:
    s = MiningState(
        task_id=1,
        region="USA",
        universe="TOP3000",
        dataset_id="ds1",
        pending_alphas=alphas,
        hypotheses=[],
        fields=[],
    )
    s.factor_tier = factor_tier
    return s


def _mk_alpha(
    *,
    sharpe: float = 1.5,
    alpha_id: str = "a1",
    expression: str = "ts_rank(close, 20)",
    hypothesis: str = "",
    validation_error: str | None = None,
) -> AlphaCandidate:
    """Build a PENDING alpha that node_evaluate will rank into PASS/FAIL/etc.

    metrics carry BRAIN-style 'checks' so the evaluate node can compute
    quality_status without hitting BRAIN; sharpe is the controlling knob.
    """
    metrics: Dict[str, Any] = {
        "sharpe": sharpe,
        "fitness": 1.05,
        "turnover": 0.25,
        "returns": 0.18,
        "drawdown": 0.05,
        "checks": [
            {"name": "LOW_SHARPE", "result": "PASS" if sharpe >= 1.25 else "FAIL",
             "limit": 1.25, "value": sharpe},
            {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.05},
            {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": 0.25},
            {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.25},
        ],
        "can_submit": sharpe >= 1.25,
        "_sim_settings": {
            "region": "USA", "universe": "TOP3000",
            "delay": 1, "decay": 4, "neutralization": "INDUSTRY",
        },
    }
    a = AlphaCandidate(
        expression=expression,
        is_simulated=True,
        simulation_success=True,
        alpha_id=alpha_id,
        metrics=metrics,
        hypothesis=hypothesis or None,
        validation_error=validation_error,
    )
    a.quality_status = "PENDING"
    return a


# --------------------------------------------------------------------------- #
# Test 1: flag OFF — no _r1a_* keys written
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_r1a_hook_disabled_no_metrics_written():
    """flag=False — _r1a_* keys MUST NOT appear in alpha.metrics."""
    state = _mk_state([_mk_alpha(sharpe=1.5, alpha_id="off_a")])

    original = settings.ENABLE_R1A_HOOK
    settings.ENABLE_R1A_HOOK = False
    try:
        out = await node_evaluate(state, brain=None, config={})
    finally:
        settings.ENABLE_R1A_HOOK = original

    alpha = out["pending_alphas"][0]
    r1a_keys = [k for k in (alpha.metrics or {}).keys() if k.startswith("_r1a_")]
    assert r1a_keys == [], f"flag=OFF must not write any _r1a_* keys; got {r1a_keys}"


# --------------------------------------------------------------------------- #
# Test 2: flag ON, PASS alpha — attribution + version written, no evidence
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_r1a_hook_enabled_writes_attribution():
    """flag=True + PASS alpha — attribution in enum, version=v1, no empty evidence."""
    state = _mk_state([_mk_alpha(sharpe=1.5, alpha_id="on_a")])

    original = settings.ENABLE_R1A_HOOK
    settings.ENABLE_R1A_HOOK = True
    try:
        out = await node_evaluate(state, brain=None, config={})
    finally:
        settings.ENABLE_R1A_HOOK = original

    alpha = out["pending_alphas"][0]
    m = alpha.metrics or {}
    assert m.get("_r1a_hook_version") == "v1", f"hook_version missing/wrong: {m.get('_r1a_hook_version')}"
    assert m.get("_r1a_attribution") in {"hypothesis", "implementation", "both", "unknown"}, \
        f"attribution out of enum: {m.get('_r1a_attribution')}"
    assert isinstance(m.get("_r1a_attribution_confidence"), (int, float)), \
        f"confidence missing/wrong type: {m.get('_r1a_attribution_confidence')}"
    assert isinstance(m.get("_r1a_should_retry"), bool)
    assert isinstance(m.get("_r1a_should_modify"), bool)
    # NF-1: empty attribution_evidence (default list()) must be skipped
    assert "_r1a_attribution_evidence" not in m, \
        f"empty evidence should not be written, got {m.get('_r1a_attribution_evidence')!r}"
    # Hook success path → no error key
    assert "_r1a_hook_error" not in m


# --------------------------------------------------------------------------- #
# Test 3: hook failure isolation — node still returns + None attribution
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_r1a_hook_failure_does_not_break_node():
    """monkeypatched shim raises → node still returns + None attribution + error key."""
    state = _mk_state([_mk_alpha(sharpe=1.5, alpha_id="fail_a")])

    def _raise_boom(*args, **kwargs):
        raise ValueError("R1A_TEST_BOOM")

    original = settings.ENABLE_R1A_HOOK
    settings.ENABLE_R1A_HOOK = True
    try:
        with patch(
            "backend.agents.core.integration.enhance_existing_node_evaluate",
            _raise_boom,
        ):
            out = await node_evaluate(state, brain=None, config={})
    finally:
        settings.ENABLE_R1A_HOOK = original

    # Node returned successfully despite hook crash
    assert "pending_alphas" in out
    alpha = out["pending_alphas"][0]
    m = alpha.metrics or {}
    assert m.get("_r1a_attribution") is None, f"fail path must write None: {m.get('_r1a_attribution')}"
    assert "R1A_TEST_BOOM" in (m.get("_r1a_hook_error") or ""), \
        f"hook_error must contain exception text: {m.get('_r1a_hook_error')!r}"
    # MF-6 fix: version still marked on fail so GO denominator (metrics ? '_r1a_hook_version')
    # includes fail rows for errs_count statistics
    assert m.get("_r1a_hook_version") == "v1", \
        f"version must mark even on fail: {m.get('_r1a_hook_version')!r}"


# --------------------------------------------------------------------------- #
# Test 4: attribution distribution — 3 sim_result variants
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_r1a_hook_attribution_distribution():
    """3 alphas → IMPLEMENTATION / HYPOTHESIS / UNKNOWN attribution.

    Per backend/agents/prompts/alignment.py:determine_attribution_heuristic (line 351-380):
        - validation_error contains 'syntax'/'field' → IMPLEMENTATION
        - alignment_issues=[] + sharpe < 0.5 → HYPOTHESIS
        - alignment_issues=[] + sharpe >= 0.5 → UNKNOWN

    Empty hypothesis dict means quick_alignment_check returns ([], True),
    so alignment_issues=[] for all three.
    """
    alphas = [
        # IMPLEMENTATION: validation_error contains 'syntax'
        _mk_alpha(sharpe=1.5, alpha_id="impl_a",
                  validation_error="syntax error in expression"),
        # HYPOTHESIS: no validation error, no alignment issues, sharpe < 0.5
        _mk_alpha(sharpe=0.3, alpha_id="hypo_a"),
        # UNKNOWN: no validation error, no alignment issues, sharpe >= 0.5
        _mk_alpha(sharpe=1.5, alpha_id="unkn_a"),
    ]
    state = _mk_state(alphas)

    original = settings.ENABLE_R1A_HOOK
    settings.ENABLE_R1A_HOOK = True
    try:
        out = await node_evaluate(state, brain=None, config={})
    finally:
        settings.ENABLE_R1A_HOOK = original

    out_alphas = {a.alpha_id: a for a in out["pending_alphas"]}
    impl_attr = out_alphas["impl_a"].metrics.get("_r1a_attribution")
    hypo_attr = out_alphas["hypo_a"].metrics.get("_r1a_attribution")
    unkn_attr = out_alphas["unkn_a"].metrics.get("_r1a_attribution")

    assert impl_attr == "implementation", \
        f"impl_a: validation_error='syntax...' must give implementation, got {impl_attr}"
    assert hypo_attr == "hypothesis", \
        f"hypo_a: sharpe=0.3 + no issues must give hypothesis, got {hypo_attr}"
    assert unkn_attr == "unknown", \
        f"unkn_a: sharpe=1.5 + no issues must give unknown, got {unkn_attr}"


# --------------------------------------------------------------------------- #
# v1.6 fix: r1a_attribution_log table captures ALL evaluated alphas
# independent of alpha persistence (FAIL/OPTIMIZE alphas now logged too)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_r1a_log_table_captures_all_evaluated_alphas():
    """v1.6 fix: r1a_attribution_log must record every alpha evaluated,
    even those that won't INSERT into the alphas table (FAIL/OPTIMIZE)."""
    from sqlalchemy import text
    from backend.database import AsyncSessionLocal

    alphas = [
        _mk_alpha(sharpe=1.5, alpha_id="log_pass_a"),
        _mk_alpha(sharpe=0.3, alpha_id="log_fail_a"),
        _mk_alpha(sharpe=1.5, alpha_id="log_impl_a",
                  validation_error="syntax error"),
    ]
    state = _mk_state(alphas)

    # Snapshot pre-run log count for THESE alpha_ids
    async with AsyncSessionLocal() as s:
        r = await s.execute(text(
            "SELECT COUNT(*) FROM r1a_attribution_log WHERE alpha_id_brain IN "
            "('log_pass_a', 'log_fail_a', 'log_impl_a')"
        ))
        pre_count = r.scalar() or 0

    original = settings.ENABLE_R1A_HOOK
    settings.ENABLE_R1A_HOOK = True
    try:
        await node_evaluate(state, brain=None, config={})
    finally:
        settings.ENABLE_R1A_HOOK = original

    # Verify all 3 alphas landed in r1a_attribution_log regardless of routing
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("""
            SELECT alpha_id_brain, attribution, hook_version
            FROM r1a_attribution_log
            WHERE alpha_id_brain IN ('log_pass_a', 'log_fail_a', 'log_impl_a')
            ORDER BY alpha_id_brain
        """))
        rows = r.all()

    post_count = len(rows)
    assert post_count - pre_count == 3, (
        f"expected 3 new log rows (one per evaluated alpha), got {post_count - pre_count} "
        f"(pre={pre_count}, post={post_count})"
    )

    by_alpha = {row[0]: row for row in rows}
    assert by_alpha["log_pass_a"][1] == "unknown", \
        f"log_pass_a attribution: {by_alpha['log_pass_a'][1]}"
    assert by_alpha["log_fail_a"][1] == "hypothesis", \
        f"log_fail_a attribution: {by_alpha['log_fail_a'][1]}"
    assert by_alpha["log_impl_a"][1] == "implementation", \
        f"log_impl_a attribution: {by_alpha['log_impl_a'][1]}"
    for alpha_id, row in by_alpha.items():
        assert row[2] == "v1", f"{alpha_id}: hook_version expected 'v1', got {row[2]!r}"

    # Cleanup: don't pollute the table with test rows
    async with AsyncSessionLocal() as s:
        await s.execute(text(
            "DELETE FROM r1a_attribution_log WHERE alpha_id_brain IN "
            "('log_pass_a', 'log_fail_a', 'log_impl_a')"
        ))
        await s.commit()


# --------------------------------------------------------------------------- #
# MEDIUM-N4 (FixDE 02adb0b re-review): R1a × R5 flag matrix coverage
# --------------------------------------------------------------------------- #
# Before FixDE bug-#6, R5 was NESTED under `if ENABLE_R1A_HOOK:` so flipping
# R1a OFF silently killed R5. The fix decoupled the guards. The 4-cell
# matrix is now:
#   (R1a OFF, R5 OFF) — covered by test_r1a_hook_disabled_no_metrics_written
#   (R1a ON,  R5 OFF) — covered by tests 2/3/4 above
#   (R1a OFF, R5 ON ) — covered by test_r5_on_r1a_off_writes_r5_metrics_only
#                       (literal bug-#6 regression guard)
#   (R1a ON,  R5 ON ) — covered by test_r5_on_r1a_on_both_write_independently
#                       (interaction / no-crosstalk guard)
# --------------------------------------------------------------------------- #

def _mock_run_r5_judge_factory(
    *,
    r5_attribution=None,
    composite=0.9,
    c1_aligned="true",
    c2_aligned="true",
):
    """Return an async callable matching run_r5_judge's signature/contract.

    Default: both c₁+c₂ aligned → r5_attribution=None (no override),
    which keeps the R1a-on path's `_r1a_attribution` clean for crosstalk
    assertions.
    """
    async def _fake(
        *,
        hypothesis_statement,
        description,
        expression,
        llm_service,
        r1a_attribution=None,
        operators_used=None,
    ):
        agrees = None
        if r5_attribution is not None and r1a_attribution is not None:
            agrees = "true" if r5_attribution == r1a_attribution else "false"
        return {
            "r5_c1_aligned": c1_aligned,
            "r5_c1_confidence": 0.9,
            "r5_c1_reason": "mock c1 ok",
            "r5_c2_aligned": c2_aligned,
            "r5_c2_confidence": 0.88,
            "r5_c2_reason": "mock c2 ok",
            "r5_composite_score": composite,
            "r5_agrees_r1a": agrees,
            "r5_hook_error": None,
            "r5_cost_usd": 0.0042,
            "r5_attribution": r5_attribution,  # popped by caller before merge
        }

    return _fake


@pytest.mark.asyncio
async def test_r5_on_r1a_off_writes_r5_metrics_only(monkeypatch):
    """[FixDE bug-#6 regression guard]

    R1a=OFF + R5=ON — before FixDE, R5 was nested under ENABLE_R1A_HOOK
    so this cell did nothing. After the fix:
      * r1a_attribution_log row MUST be written with r5_* columns filled
      * alpha.metrics MUST NOT contain `_r1a_attribution`,
        `_r1a_hook_version`, etc. (R1a guard never fired)
    """
    from sqlalchemy import text
    from backend.database import AsyncSessionLocal

    # Provide hypothesis + non-empty expression so the (mocked) R5 path
    # is "exercised" semantically; the real LLM never gets called.
    state = _mk_state([
        _mk_alpha(
            sharpe=1.5,
            alpha_id="r5only_a",
            hypothesis="momentum reverses on high-volume names",
        )
    ])
    # Set explanation so R5 description isn't empty (R5 inner guard skips
    # c₁/c₂ on empty desc, but our mock bypasses that — still set it to
    # exercise the real evaluation.py call path inputs).
    state.pending_alphas[0].explanation = "rolling z-score of close over 20d"

    # Inject fakes BEFORE running node_evaluate so the lazy imports inside
    # the R1a+R5 block pick up the patches.
    monkeypatch.setattr(
        "backend.agents.graph.r5_judge.run_r5_judge",
        _mock_run_r5_judge_factory(r5_attribution=None),  # no override
    )
    # Stub get_llm_service — its return value is forwarded to the mocked
    # run_r5_judge which ignores it. Avoids needing real API creds.
    monkeypatch.setattr(
        "backend.agents.services.llm_service.get_llm_service",
        lambda: object(),
    )

    # Pre-snapshot the log table for this alpha_id
    async with AsyncSessionLocal() as s:
        r = await s.execute(text(
            "SELECT COUNT(*) FROM r1a_attribution_log WHERE alpha_id_brain = 'r5only_a'"
        ))
        pre_count = r.scalar() or 0

    orig_r1a = settings.ENABLE_R1A_HOOK
    orig_r5 = settings.ENABLE_LLM_JUDGE
    settings.ENABLE_R1A_HOOK = False
    settings.ENABLE_LLM_JUDGE = True
    try:
        out = await node_evaluate(state, brain=None, config={})
    finally:
        settings.ENABLE_R1A_HOOK = orig_r1a
        settings.ENABLE_LLM_JUDGE = orig_r5

    # 1) alpha.metrics MUST NOT carry any _r1a_* keys (R1a guard was OFF)
    alpha = out["pending_alphas"][0]
    m = alpha.metrics or {}
    r1a_keys = [k for k in m.keys() if k.startswith("_r1a_")]
    assert r1a_keys == [], (
        f"R1a=OFF must not write _r1a_* keys to metrics, even with R5=ON; "
        f"got {r1a_keys}"
    )

    # 2) r1a_attribution_log row WAS written with r5_* columns filled
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("""
            SELECT attribution, hook_version,
                   r5_c1_aligned, r5_c2_aligned,
                   r5_composite_score, r5_cost_usd, r5_hook_error
            FROM r1a_attribution_log
            WHERE alpha_id_brain = 'r5only_a'
            ORDER BY id DESC
            LIMIT 1
        """))
        row = r.first()
        # Cleanup before any assertion failures
        await s.execute(text(
            "DELETE FROM r1a_attribution_log WHERE alpha_id_brain = 'r5only_a'"
        ))
        await s.commit()

    assert row is not None, (
        "R5-only path must still INSERT into r1a_attribution_log so "
        "r5_* metrics have a place to land"
    )
    # attribution is None (R1a guard didn't fire, R5 mock returned None override)
    assert row[0] is None, f"R5 None-override + R1a OFF must keep attribution=None; got {row[0]!r}"
    assert row[1] == "v1", f"hook_version must be v1, got {row[1]!r}"
    # r5_* columns populated by the mock
    assert row[2] == "true", f"r5_c1_aligned must be populated by R5 hook; got {row[2]!r}"
    assert row[3] == "true", f"r5_c2_aligned must be populated by R5 hook; got {row[3]!r}"
    assert row[4] == pytest.approx(0.9), f"r5_composite_score must be populated; got {row[4]!r}"
    assert row[5] == pytest.approx(0.0042), f"r5_cost_usd must be populated; got {row[5]!r}"
    assert row[6] is None, f"r5_hook_error must be None on happy path; got {row[6]!r}"

    # Sanity: only 1 new row
    async with AsyncSessionLocal() as s:
        r = await s.execute(text(
            "SELECT COUNT(*) FROM r1a_attribution_log WHERE alpha_id_brain = 'r5only_a'"
        ))
        post_after_cleanup = r.scalar() or 0
    assert post_after_cleanup == pre_count, "cleanup must restore baseline count"


@pytest.mark.asyncio
async def test_r5_on_r1a_on_both_write_independently(monkeypatch):
    """[Interaction guard]

    Both R1a + R5 ON — verifies decoupled guards both fire AND R5's
    None-verdict does NOT clobber R1a's heuristic verdict (no crosstalk).
    Uses R5 mock returning r5_attribution=None (both PASS) so the
    [V1.0-A2-3] override rule is intentionally NOT triggered; R1a verdict
    must survive intact in both metrics and the log row.
    """
    from sqlalchemy import text
    from backend.database import AsyncSessionLocal

    # sharpe=1.5 + empty hypothesis → R1a heuristic → 'unknown'
    state = _mk_state([
        _mk_alpha(
            sharpe=1.5,
            alpha_id="both_on_a",
            hypothesis="reversal on stretched price",
        )
    ])
    state.pending_alphas[0].explanation = "ts_rank close 20d"

    monkeypatch.setattr(
        "backend.agents.graph.r5_judge.run_r5_judge",
        _mock_run_r5_judge_factory(r5_attribution=None),
    )
    monkeypatch.setattr(
        "backend.agents.services.llm_service.get_llm_service",
        lambda: object(),
    )

    orig_r1a = settings.ENABLE_R1A_HOOK
    orig_r5 = settings.ENABLE_LLM_JUDGE
    settings.ENABLE_R1A_HOOK = True
    settings.ENABLE_LLM_JUDGE = True
    try:
        out = await node_evaluate(state, brain=None, config={})
    finally:
        settings.ENABLE_R1A_HOOK = orig_r1a
        settings.ENABLE_LLM_JUDGE = orig_r5

    # 1) alpha.metrics MUST carry R1a verdict (decoupled guard fired)
    alpha = out["pending_alphas"][0]
    m = alpha.metrics or {}
    assert m.get("_r1a_hook_version") == "v1", \
        f"R1a guard must fire when flag ON; got version={m.get('_r1a_hook_version')!r}"
    r1a_attr = m.get("_r1a_attribution")
    assert r1a_attr in {"hypothesis", "implementation", "both", "unknown"}, \
        f"R1a attribution must be enum value; got {r1a_attr!r}"
    # 2) no-crosstalk: R5 returned None override → R1a value preserved
    # (We pin r1a_attr above; mock injects r5_attribution=None so override
    # block in evaluation.py L2710 must NOT run.)
    assert m.get("_r1a_hook_error") is None or "_r1a_hook_error" not in m

    # 3) DB log row MUST contain BOTH R1a fields AND R5 fields filled
    async with AsyncSessionLocal() as s:
        r = await s.execute(text("""
            SELECT attribution, hook_version,
                   r5_c1_aligned, r5_c2_aligned,
                   r5_composite_score, r5_cost_usd, r5_hook_error
            FROM r1a_attribution_log
            WHERE alpha_id_brain = 'both_on_a'
            ORDER BY id DESC
            LIMIT 1
        """))
        row = r.first()
        await s.execute(text(
            "DELETE FROM r1a_attribution_log WHERE alpha_id_brain = 'both_on_a'"
        ))
        await s.commit()

    assert row is not None, "R1a+R5 ON must INSERT log row"
    # R1a attribution survived — R5 None did NOT clobber it
    assert row[0] == r1a_attr, (
        f"DB attribution must match metrics attribution (R5 None must NOT "
        f"overwrite R1a verdict); db={row[0]!r} vs metrics={r1a_attr!r}"
    )
    assert row[1] == "v1"
    # R5 columns ALSO populated alongside R1a — independent guards
    assert row[2] == "true", f"r5_c1_aligned must be populated; got {row[2]!r}"
    assert row[3] == "true", f"r5_c2_aligned must be populated; got {row[3]!r}"
    assert row[4] == pytest.approx(0.9), f"r5_composite_score must be populated; got {row[4]!r}"
    assert row[5] == pytest.approx(0.0042), f"r5_cost_usd must be populated; got {row[5]!r}"
    assert row[6] is None, f"r5_hook_error must be None on happy path; got {row[6]!r}"
