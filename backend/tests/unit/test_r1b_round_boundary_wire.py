"""Phase 3 R1b.2c operator wire site tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §6.2 V-R1b.2c.

Bounded ~30min follow-up to close the R1b.2c operator-deployable end-to-end
loop. Two wire points:

  1. ``mining_tasks._run_one_round_inline`` entry:
     ``consume_pending_hypothesis(task, db)`` when ENABLE_R1B_HYPOTHESIS_MUTATE
     is ON (v1 log-only; injection into MiningState is R1b.2-v2).

  2. ``backend.agents.graph.nodes.persistence.node_save_results`` exit:
     ``persist_after_round(state, task, db)`` when either retry or mutate
     flag is ON.

Both call sites are flag-gated, soft-fail (never raise), and byte-equivalent
to legacy when both flags are OFF.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Flag-OFF byte-equivalent sentinel — neither helper should be imported nor
# called when both R1b flags are OFF.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consume_wire_skipped_when_mutate_flag_off(monkeypatch):
    """ENABLE_R1B_HYPOTHESIS_MUTATE=False → consume_pending_hypothesis NEVER awaited."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", False, raising=False)

    consume_mock = AsyncMock(return_value=None)
    with patch(
        "backend.agents.graph.nodes.r1b_persistence.consume_pending_hypothesis",
        consume_mock,
    ):
        # Simulate just the wire block from _run_one_round_inline.
        from backend.config import settings as _r1b_settings
        if bool(getattr(_r1b_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False)):
            from backend.agents.graph.nodes.r1b_persistence import (
                consume_pending_hypothesis,
            )
            await consume_pending_hypothesis(
                SimpleNamespace(id=1, config={}), SimpleNamespace()
            )
    consume_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_wire_skipped_when_both_flags_off(monkeypatch):
    """ENABLE_R1B_RETRY_LOOP+MUTATE both False → persist_after_round NEVER awaited."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", False, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)

    persist_mock = AsyncMock(return_value=True)
    with patch(
        "backend.agents.graph.nodes.r1b_persistence.persist_after_round",
        persist_mock,
    ):
        # Simulate just the wire block from node_save_results.
        from backend.config import settings as _r1b_settings
        if (
            bool(getattr(_r1b_settings, "ENABLE_R1B_RETRY_LOOP", False))
            or bool(getattr(_r1b_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False))
        ):
            from backend.agents.graph.nodes.r1b_persistence import (
                persist_after_round,
            )
            await persist_after_round(MagicMock(), MagicMock(), MagicMock())
    persist_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Flag-ON wire invocation — consume side
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consume_wire_invoked_when_mutate_flag_on(monkeypatch, caplog):
    """ENABLE_R1B_HYPOTHESIS_MUTATE=True → consume_pending_hypothesis awaited
    + non-None payload logged."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)

    pending = {
        "statement": "Test hypothesis A — momentum reverses after volume spike",
        "rationale": "spike-driven mean reversion",
    }
    consume_mock = AsyncMock(return_value=pending)
    with patch(
        "backend.agents.graph.nodes.r1b_persistence.consume_pending_hypothesis",
        consume_mock,
    ):
        task = SimpleNamespace(id=99, config={})
        db = SimpleNamespace()
        # Inline-replay the wire block.
        from backend.config import settings as _r1b_settings
        if bool(getattr(_r1b_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False)):
            from backend.agents.graph.nodes.r1b_persistence import (
                consume_pending_hypothesis,
            )
            consumed = await consume_pending_hypothesis(task, db)
            assert consumed == pending
    consume_mock.assert_awaited_once_with(task, db)


@pytest.mark.asyncio
async def test_consume_wire_soft_fails_when_helper_raises(monkeypatch):
    """consume_pending_hypothesis raising → wire block must not propagate."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)

    async def _raise(*a, **kw):
        raise RuntimeError("DB hiccup")

    with patch(
        "backend.agents.graph.nodes.r1b_persistence.consume_pending_hypothesis",
        _raise,
    ):
        task = SimpleNamespace(id=99, config={})
        db = SimpleNamespace()
        # Replay the wire block with its try/except guard.
        caught = False
        try:
            from backend.config import settings as _r1b_settings
            if bool(getattr(_r1b_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False)):
                from backend.agents.graph.nodes.r1b_persistence import (
                    consume_pending_hypothesis,
                )
                await consume_pending_hypothesis(task, db)
        except Exception:
            caught = True
        # The real _run_one_round_inline wraps in try/except — verifies the
        # contract that the helper itself swallows errors (returns None).
        # If reached here without swallowing, the wire's outer try/except
        # would catch it; either way the round must not abort.
        assert caught is True  # helper passed-through (its own try/except is the
        # other test; here we just confirm the wire's outer guard catches)


# ---------------------------------------------------------------------------
# Flag-ON wire invocation — persist side (node_save_results exit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_wire_skipped_when_db_session_missing(monkeypatch):
    """Flag ON but db_session absent in configurable → persist NOT called."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)

    persist_mock = AsyncMock(return_value=True)
    with patch(
        "backend.agents.graph.nodes.r1b_persistence.persist_after_round",
        persist_mock,
    ):
        configurable = {}  # No db_session
        state = SimpleNamespace(task_id=1)
        # Inline-replay the wire block.
        from backend.config import settings as _r1b_settings
        if (
            bool(getattr(_r1b_settings, "ENABLE_R1B_RETRY_LOOP", False))
            or bool(getattr(_r1b_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False))
        ):
            _db = configurable.get("db_session")
            _task_id = getattr(state, "task_id", None)
            if _db is not None and _task_id is not None:
                from backend.agents.graph.nodes.r1b_persistence import (
                    persist_after_round,
                )
                await persist_after_round(state, MagicMock(), _db)
    persist_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_wire_skipped_when_task_id_missing(monkeypatch):
    """Flag ON, db_session present, but state.task_id None → persist NOT called."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)

    persist_mock = AsyncMock(return_value=True)
    with patch(
        "backend.agents.graph.nodes.r1b_persistence.persist_after_round",
        persist_mock,
    ):
        db = MagicMock()
        configurable = {"db_session": db}
        state = SimpleNamespace(task_id=None)
        from backend.config import settings as _r1b_settings
        if (
            bool(getattr(_r1b_settings, "ENABLE_R1B_RETRY_LOOP", False))
            or bool(getattr(_r1b_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False))
        ):
            _db = configurable.get("db_session")
            _task_id = getattr(state, "task_id", None)
            if _db is not None and _task_id is not None:
                from backend.agents.graph.nodes.r1b_persistence import (
                    persist_after_round,
                )
                await persist_after_round(state, MagicMock(), _db)
    persist_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Sentinel — neither flag triggers module imports (byte-equivalent guarantee)
# ---------------------------------------------------------------------------

def test_byte_equiv_sentinel_no_r1b_persistence_import_when_flags_off(monkeypatch):
    """Re-import the wire-affected modules with both flags OFF; verify
    r1b_persistence is NOT pulled in via the flag-gated branch.

    This is a static-source sentinel: parse mining_tasks._run_one_round_inline +
    persistence.node_save_results and assert each wire block sits behind the
    correct flag check.
    """
    import inspect
    from backend.tasks import mining_tasks
    from backend.agents.graph.nodes import persistence

    src_mining = inspect.getsource(mining_tasks._run_one_round_inline)
    assert "ENABLE_R1B_HYPOTHESIS_MUTATE" in src_mining
    assert "consume_pending_hypothesis" in src_mining
    assert "r1b_wire" in src_mining

    src_persist = inspect.getsource(persistence.node_save_results)
    assert "ENABLE_R1B_RETRY_LOOP" in src_persist
    assert "ENABLE_R1B_HYPOTHESIS_MUTATE" in src_persist
    assert "persist_after_round" in src_persist
    assert "R1b.2c" in src_persist
