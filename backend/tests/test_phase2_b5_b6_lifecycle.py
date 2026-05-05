"""Phase 2 B5/B6 — attribution classification + abandonment trigger tests.

B5 classify_attribution and B6 should_abandon_hypothesis are pure functions
that don't touch DB; they're the heart of the lifecycle logic. Plus we run
an end-to-end integration test against live Postgres for
_process_hypothesis_feedback to verify the DB transitions fire correctly.
"""
from __future__ import annotations

import socket
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.agents.graph.early_stop import (
    classify_attribution,
    should_abandon_hypothesis,
    HYPOTHESIS_ABANDON_ROUNDS,
)


# =============================================================================
# B5 — classify_attribution (pure)
# =============================================================================

def test_classify_unknown_when_no_alphas():
    assert classify_attribution(
        alpha_count=0, pass_count=0,
        syntax_fail_count=0, simulate_fail_count=0, quality_fail_count=0,
    ) == "unknown"


def test_classify_unknown_when_at_least_one_pass():
    """PASS happened — abandon-relevant attribution doesn't apply."""
    assert classify_attribution(
        alpha_count=5, pass_count=1,
        syntax_fail_count=2, simulate_fail_count=1, quality_fail_count=1,
    ) == "unknown"


def test_classify_implementation_when_syntax_simulate_dominate():
    """≥75% of FAIL is syntax+simulate → IMPLEMENTATION (LLM rendered bad code)."""
    # 4 LLM-fail vs 1 quality-fail = 80% impl share
    assert classify_attribution(
        alpha_count=5, pass_count=0,
        syntax_fail_count=2, simulate_fail_count=2, quality_fail_count=1,
    ) == "implementation"


def test_classify_hypothesis_when_quality_dominates():
    """≥75% of FAIL is quality (signal direction wrong) → HYPOTHESIS."""
    # 4 quality-fail vs 1 sim-fail = 80% qual share
    assert classify_attribution(
        alpha_count=5, pass_count=0,
        syntax_fail_count=0, simulate_fail_count=1, quality_fail_count=4,
    ) == "hypothesis"


def test_classify_hypothesis_when_only_quality_fails():
    """All FAIL are quality — pure hypothesis fail."""
    assert classify_attribution(
        alpha_count=4, pass_count=0,
        syntax_fail_count=0, simulate_fail_count=0, quality_fail_count=4,
    ) == "hypothesis"


def test_classify_implementation_when_only_syntax():
    """All FAIL are syntax — LLM can't write valid code."""
    assert classify_attribution(
        alpha_count=3, pass_count=0,
        syntax_fail_count=3, simulate_fail_count=0, quality_fail_count=0,
    ) == "implementation"


def test_classify_both_when_evenly_mixed():
    """50/50 split → BOTH (neither dominates ≥75%)."""
    assert classify_attribution(
        alpha_count=4, pass_count=0,
        syntax_fail_count=1, simulate_fail_count=1,
        quality_fail_count=2,  # 50% impl / 50% qual
    ) == "both"


# =============================================================================
# B6 — should_abandon_hypothesis (pure)
# =============================================================================

def test_abandon_no_history():
    assert should_abandon_hypothesis([]) == (False, None)


def test_abandon_short_history_no_action():
    """Less than N rounds → no abandon decision yet."""
    history = [
        {"round_index": 1, "pass_count": 0, "attribution": "hypothesis"},
        {"round_index": 2, "pass_count": 0, "attribution": "hypothesis"},
    ]
    abandon, reason = should_abandon_hypothesis(history)
    assert abandon is False


def test_abandon_n_consecutive_hypothesis_fails_triggers():
    """N=3 rounds with 0 PASS + HYPOTHESIS attribution → abandon."""
    history = [
        {"round_index": 1, "pass_count": 0, "attribution": "hypothesis"},
        {"round_index": 2, "pass_count": 0, "attribution": "hypothesis"},
        {"round_index": 3, "pass_count": 0, "attribution": "hypothesis"},
    ]
    abandon, reason = should_abandon_hypothesis(history)
    assert abandon is True
    assert "3 consecutive rounds" in reason
    assert "rounds 1,2,3" in reason


def test_abandon_implementation_fails_do_not_count():
    """3 IMPLEMENTATION-attribution rounds — don't abandon a hypothesis just
    because the LLM kept writing buggy code. This is the core B6 invariant."""
    history = [
        {"round_index": 1, "pass_count": 0, "attribution": "implementation"},
        {"round_index": 2, "pass_count": 0, "attribution": "implementation"},
        {"round_index": 3, "pass_count": 0, "attribution": "implementation"},
    ]
    assert should_abandon_hypothesis(history) == (False, None)


def test_abandon_pass_in_window_resets():
    """If any of the last N rounds had PASS, don't abandon."""
    history = [
        {"round_index": 1, "pass_count": 0, "attribution": "hypothesis"},
        {"round_index": 2, "pass_count": 1, "attribution": "unknown"},  # PASS
        {"round_index": 3, "pass_count": 0, "attribution": "hypothesis"},
    ]
    assert should_abandon_hypothesis(history) == (False, None)


def test_abandon_mixed_attribution_window_no_trigger():
    """Last N rounds must ALL be HYPOTHESIS attribution."""
    history = [
        {"round_index": 1, "pass_count": 0, "attribution": "hypothesis"},
        {"round_index": 2, "pass_count": 0, "attribution": "both"},
        {"round_index": 3, "pass_count": 0, "attribution": "hypothesis"},
    ]
    assert should_abandon_hypothesis(history) == (False, None)


def test_abandon_n_param_overrides_default():
    """Custom n_rounds threshold."""
    history = [
        {"round_index": 1, "pass_count": 0, "attribution": "hypothesis"},
        {"round_index": 2, "pass_count": 0, "attribution": "hypothesis"},
    ]
    # Default N=3 → no abandon
    assert should_abandon_hypothesis(history)[0] is False
    # N=2 → abandon
    assert should_abandon_hypothesis(history, n_rounds=2)[0] is True


def test_abandon_only_looks_at_last_n():
    """If older rounds had implementation fails but last N are HYPOTHESIS-fail
    → abandon. The window is sliding over the most recent N entries only."""
    history = [
        {"round_index": 1, "pass_count": 0, "attribution": "implementation"},
        {"round_index": 2, "pass_count": 0, "attribution": "implementation"},
        # Last 3 are all hypothesis-fail
        {"round_index": 3, "pass_count": 0, "attribution": "hypothesis"},
        {"round_index": 4, "pass_count": 0, "attribution": "hypothesis"},
        {"round_index": 5, "pass_count": 0, "attribution": "hypothesis"},
    ]
    abandon, reason = should_abandon_hypothesis(history)
    assert abandon is True
    assert "rounds 3,4,5" in reason


# =============================================================================
# Integration — _process_hypothesis_feedback against live Postgres
# =============================================================================

def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


_TAG = f"_b5_{uuid.uuid4().hex[:8]}_"


@pytest.mark.skipif(not _pg_reachable(), reason="PG not reachable")
@pytest.mark.asyncio
async def test_b5_marks_promoted_when_round_has_pass():
    """End-to-end: round with 1 PASS alpha → hypothesis transitions PROPOSED
    → PROMOTED via mark_promoted."""
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService, HypothesisCreateData
    from backend.agents.graph.nodes.persistence import _process_hypothesis_feedback
    from backend.agents.graph.state import MiningState, AlphaCandidate

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        svc = HypothesisService(s)
        h = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}promoted-test",
            region="USA", target_tier=1,
        ))
        await s.commit()

        try:
            state = MiningState(
                task_id=999_001, region="USA", universe="TOP3000",
                dataset_id="pv1", fields=[], operators=[],
                current_hypothesis_id=h.id,
                current_hypothesis_ids=[h.id],
            )
            pending = [
                AlphaCandidate(
                    expression="rank(close)", is_valid=True,
                    is_simulated=True, simulation_success=True,
                    quality_status="PASS",
                    metrics={"sharpe": 2.0},
                ),
            ]
            history_out = await _process_hypothesis_feedback(
                state=state,
                round_index=1,
                pending_alphas=pending,
                history_so_far={},
                trace_service=None,
            )

            assert h.id in history_out
            entry = history_out[h.id][0]
            assert entry["pass_count"] == 1
            # PASS gets attribution=unknown (not abandon-relevant)
            assert entry["attribution"] == "unknown"

            # Verify DB lifecycle change
            await s.refresh(h)
            from backend.models import HypothesisStatus
            assert h.status == HypothesisStatus.PROMOTED.value
        finally:
            await s.execute(delete(Hypothesis).where(Hypothesis.id == h.id))
            await s.commit()
    await engine.dispose()


@pytest.mark.skipif(not _pg_reachable(), reason="PG not reachable")
@pytest.mark.asyncio
async def test_b6_abandons_after_3_hypothesis_fail_rounds():
    """End-to-end: 3 rounds of all-quality-fail alphas → hypothesis ABANDONED."""
    from backend.models import Hypothesis, HypothesisStatus
    from backend.services.hypothesis_service import HypothesisService, HypothesisCreateData
    from backend.agents.graph.nodes.persistence import _process_hypothesis_feedback
    from backend.agents.graph.state import MiningState, AlphaCandidate

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        svc = HypothesisService(s)
        h = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}abandon-test",
            region="USA", target_tier=1,
        ))
        await s.commit()

        try:
            history: dict = {}
            for round_idx in (1, 2, 3):
                state = MiningState(
                    task_id=999_002, region="USA", universe="TOP3000",
                    dataset_id="pv1", fields=[], operators=[],
                    current_hypothesis_id=h.id,
                    current_hypothesis_ids=[h.id],
                    hypothesis_round_history=history,
                )
                # All quality-fail (signal direction wrong)
                pending = [
                    AlphaCandidate(
                        expression=f"rank(close * {round_idx}.{i})",
                        is_valid=True,
                        is_simulated=True, simulation_success=True,
                        quality_status="FAIL",
                        metrics={"sharpe": -0.5},
                    )
                    for i in range(4)
                ]
                history = await _process_hypothesis_feedback(
                    state=state,
                    round_index=round_idx,
                    pending_alphas=pending,
                    history_so_far=history,
                    trace_service=None,
                )

            entries = history[h.id]
            assert len(entries) == 3
            assert all(e["attribution"] == "hypothesis" for e in entries)

            await s.refresh(h)
            assert h.status == HypothesisStatus.ABANDONED.value
            assert "3 consecutive rounds" in (h.abandon_reason or "")
        finally:
            await s.execute(delete(Hypothesis).where(Hypothesis.id == h.id))
            await s.commit()
    await engine.dispose()


@pytest.mark.skipif(not _pg_reachable(), reason="PG not reachable")
@pytest.mark.asyncio
async def test_b6_does_not_abandon_for_3_implementation_fails():
    """Critical invariant: 3 rounds of LLM-emit-bad-code does NOT trigger
    abandon. Hypothesis stays ACTIVE so the system keeps trying."""
    from backend.models import Hypothesis, HypothesisStatus
    from backend.services.hypothesis_service import HypothesisService, HypothesisCreateData
    from backend.agents.graph.nodes.persistence import _process_hypothesis_feedback
    from backend.agents.graph.state import MiningState, AlphaCandidate

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        svc = HypothesisService(s)
        h = await svc.create_hypothesis(HypothesisCreateData(
            statement=f"{_TAG}impl-fail-test",
            region="USA", target_tier=1,
        ))
        await s.commit()

        try:
            history: dict = {}
            for round_idx in (1, 2, 3):
                state = MiningState(
                    task_id=999_003, region="USA", universe="TOP3000",
                    dataset_id="pv1", fields=[], operators=[],
                    current_hypothesis_id=h.id,
                    current_hypothesis_ids=[h.id],
                    hypothesis_round_history=history,
                )
                # All syntax-fail (LLM can't write valid code)
                pending = [
                    AlphaCandidate(
                        expression="invalid_garbage(",
                        is_valid=False,
                        validation_error="syntax",
                        is_simulated=False,
                        quality_status="FAIL",
                        metrics={},
                    )
                    for _ in range(4)
                ]
                history = await _process_hypothesis_feedback(
                    state=state,
                    round_index=round_idx,
                    pending_alphas=pending,
                    history_so_far=history,
                    trace_service=None,
                )

            entries = history[h.id]
            assert all(e["attribution"] == "implementation" for e in entries)

            await s.refresh(h)
            # Should NOT be abandoned — hypothesis is fine, LLM is buggy
            assert h.status != HypothesisStatus.ABANDONED.value
            # Should be ACTIVE (alphas were generated, even though all failed)
            assert h.status == HypothesisStatus.ACTIVE.value
        finally:
            await s.execute(delete(Hypothesis).where(Hypothesis.id == h.id))
            await s.commit()
    await engine.dispose()
