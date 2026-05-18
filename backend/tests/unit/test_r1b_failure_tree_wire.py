"""Phase 3 R1b.3c: failure_tree producer-side wire tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §7.1 + §7.2.

R1b.3c closes the failure_tree sub-phase by wiring `record_failure_tree`
into the mutate node — after a successful mutate emits
pending_new_hypothesis, the helper persists a 2-node {parent → new} chain
to KB so the next round's R8 RAG L2 can surface it.

These tests verify the WIRE behavior (producer side); the
`record_failure_tree` orchestration itself is covered by
test_r1b_failure_tree.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.agents.graph.nodes import r1b_loop


class _FakeAlpha(SimpleNamespace):
    def model_copy(self):
        clone = _FakeAlpha(**self.__dict__)
        clone.metrics = dict(self.metrics or {})
        return clone


def _mk_alpha(expression, *, hypothesis="parent thesis", attribution="hypothesis",
              hypothesis_id=None):
    metrics = {
        "_r1a_attribution": attribution,
        "_r5_c1_reason": "h vs d misaligned",
        "sharpe": 0.1, "fitness": 0.0,
    }
    if hypothesis_id is not None:
        metrics["hypothesis_id"] = hypothesis_id
    return _FakeAlpha(
        alpha_id="alpha-0", expression=expression,
        is_valid=True, is_simulated=True, simulation_success=False,
        quality_status="FAIL", hypothesis=hypothesis, metrics=metrics,
    )


def _mk_state(alphas):
    return SimpleNamespace(
        pending_alphas=alphas, fields=[], region="USA",
        task_id=42, round_idx=1, dataset_id="pv13",
        r1b_retries_attempted_this_alpha=0,
        r1b_mutations_attempted_this_cycle=0,
        r1b_token_cost_this_alpha=0.0,
    )


def _mk_llm(new_statement, diff="changed scope"):
    parsed = {
        "new_hypothesis": {
            "statement": new_statement, "rationale": "x",
            "expected_signal": "momentum",
            "key_fields": [], "suggested_operators": [],
        },
        "diff_from_original": diff,
    }
    resp = SimpleNamespace(success=True, parsed=parsed, content="", tokens_used=200)
    svc = SimpleNamespace(model="claude-haiku-4-5-20251001")
    svc.call = AsyncMock(return_value=resp)
    return svc


@pytest.fixture(autouse=True)
def _stub_log_writer():
    """Suppress real DB writes in mutate node."""
    with patch.object(
        r1b_loop, "_write_r1b_retry_log_rows",
        new=AsyncMock(return_value=None),
    ):
        yield


# ---------------------------------------------------------------------------
# Wire behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_tree_wire_called_when_flag_on_and_pending_emitted(monkeypatch):
    """Flag ON + successful mutate → _maybe_record_failure_tree fires."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)

    alpha = _mk_alpha("rank(close)", hypothesis="parent thesis", hypothesis_id=123)
    state = _mk_state([alpha])
    llm = _mk_llm("new revised thesis", diff="scope narrowed")

    with patch.object(
        r1b_loop, "_maybe_record_failure_tree",
        new=AsyncMock(return_value=None),
    ) as mock_record:
        out = await r1b_loop.node_hypothesis_mutate(state, llm)

    assert "r1b_pending_new_hypothesis" in out
    mock_record.assert_awaited_once()
    kwargs = mock_record.await_args.kwargs
    assert kwargs["primary_hyp"] == "parent thesis"
    assert kwargs["pending"]["statement"] == "new revised thesis"
    # log_rows includes the mutate_hyp entry
    assert any(r["attempt_type"] == "mutate_hyp" for r in kwargs["log_rows"])


@pytest.mark.asyncio
async def test_failure_tree_wire_skipped_when_no_pending_hypothesis(monkeypatch):
    """LLM returns same statement → no pending_new_hypothesis → no wire fire."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)

    alpha = _mk_alpha("rank(close)", hypothesis="same thesis")
    state = _mk_state([alpha])
    llm = _mk_llm("same thesis")  # same as original → no pending emit

    with patch.object(
        r1b_loop, "_maybe_record_failure_tree",
        new=AsyncMock(return_value=None),
    ) as mock_record:
        out = await r1b_loop.node_hypothesis_mutate(state, llm)

    assert "r1b_pending_new_hypothesis" not in out
    mock_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_tree_wire_returns_early_when_flag_off(monkeypatch):
    """ENABLE_R1B_FAILURE_TREE=False → _maybe_record_failure_tree early-returns
    without DB touch (verified via mocking record_failure_tree)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", False, raising=False)

    alpha = _mk_alpha("rank(close)", hypothesis="parent")
    state = _mk_state([alpha])
    llm = _mk_llm("new revised")

    # _maybe_record_failure_tree IS called (we don't gate at the caller),
    # but it internally returns early without touching record_failure_tree.
    with patch(
        "backend.knowledge_extraction.record_failure_tree",
        new=AsyncMock(return_value=False),
    ) as mock_writer:
        await r1b_loop.node_hypothesis_mutate(state, llm)

    # record_failure_tree not called because flag OFF early-return
    mock_writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_tree_wire_soft_fails_on_exception(monkeypatch):
    """record_failure_tree raises → mutate node still returns OK (soft-fall)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)

    alpha = _mk_alpha("rank(close)", hypothesis="parent")
    state = _mk_state([alpha])
    llm = _mk_llm("new revised")

    with patch(
        "backend.knowledge_extraction.record_failure_tree",
        new=AsyncMock(side_effect=RuntimeError("simulated DB blowup")),
    ):
        # Patch session context manager to a usable dummy
        class _DummySession:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False
        with patch(
            "backend.database.AsyncSessionLocal",
            new=lambda: _DummySession(),
        ):
            try:
                out = await r1b_loop.node_hypothesis_mutate(state, llm)
            except Exception as e:
                pytest.fail(f"mutate node must never raise; got {e}")
    assert "r1b_pending_new_hypothesis" in out


@pytest.mark.asyncio
async def test_maybe_record_failure_tree_chain_structure(monkeypatch):
    """Direct call to _maybe_record_failure_tree builds the expected 2-node chain."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)

    captured = {}
    async def _capture(**kwargs):
        captured.update(kwargs)
        return True

    class _DummySession:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    with patch(
        "backend.knowledge_extraction.record_failure_tree",
        new=AsyncMock(side_effect=_capture),
    ), patch(
        "backend.database.AsyncSessionLocal",
        new=lambda: _DummySession(),
    ):
        await r1b_loop._maybe_record_failure_tree(
            primary_hyp="parent thesis",
            pending={"statement": "new thesis", "diff_from_original": "scope"},
            log_rows=[{"attempt_type": "mutate_hyp"}],
            primary_alpha=SimpleNamespace(alpha_id="a"),
            primary_metrics={"hypothesis_id": 99},
        )
    chain = captured["hypothesis_chain"]
    assert len(chain) == 2
    assert chain[0]["id"] == 99
    assert chain[0]["statement"] == "parent thesis"
    assert chain[0]["mutation_depth"] == 0
    assert chain[1]["id"] is None
    assert chain[1]["statement"] == "new thesis"
    assert chain[1]["mutation_depth"] == 1
    assert chain[1]["diff_from_parent"] == "scope"
