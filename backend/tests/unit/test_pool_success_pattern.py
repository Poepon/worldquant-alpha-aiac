"""Pool Phase 2 (1b, LB3) — SUCCESS_PATTERN write in the shared E-persister.

The pool RAG read side already injects SUCCESS_PATTERN into code_gen, but the
pool never WROTE one (the KB write asymmetry). build_persister now mirrors the
dead node_save_results write with THREE gates: a BRAIN alpha_id present, the
robustness what-if not failed, verdict ∈ {PASS, PASS_PROVISIONAL}. Soft-fail.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.pipeline.persister import build_persister
from backend.agents.pipeline.types import Candidate, SimResult


def _result(quality_status, *, alpha_id="X1", robustness_failed=False, hid=5):
    metrics = {"sharpe": 1.6, "fitness": 1.1}
    if robustness_failed:
        metrics["_robustness_failed"] = True
    cand_alpha = AlphaCandidate(
        expression="ts_rank(close, 5)",
        is_valid=True,
        alpha_id=alpha_id,
        quality_status=quality_status,
        metrics=metrics,
    )
    st = MiningState(
        task_id=7, region="USA", universe="TOP3000", dataset_id="pv1",
        current_hypothesis_id=hid, pending_alphas=[cand_alpha],
    )
    return SimResult(
        candidate=Candidate(expression="ts_rank(close, 5)", context={}),
        ok=quality_status in ("PASS", "PASS_PROVISIONAL"),
        metrics=metrics,
        verdict=quality_status,
        trace_records=[],
        error=None,
        state=st,
    )


def _persister():
    """build_persister with all DB writes stubbed → isolates the KB hook."""
    return build_persister(
        run_id=None,
        save_fn=AsyncMock(return_value=[]),
        save_failures_fn=AsyncMock(return_value=0),
        flush_trace_fn=AsyncMock(return_value=0),
    )


@pytest.fixture
def captured(monkeypatch):
    calls = []

    async def _fake_record(self, **kw):
        calls.append(kw)
        return True

    monkeypatch.setattr(
        "backend.agents.services.rag_service.RAGService.record_success_pattern",
        _fake_record,
    )
    return calls


@pytest.mark.asyncio
async def test_written_for_pass(captured):
    await _persister()(MagicMock(), [_result("PASS")])
    assert len(captured) == 1
    kw = captured[0]
    assert kw["alpha_id"] == "X1"
    assert kw["hypothesis_id"] == 5
    assert kw["source"] == "pool_evaluate"
    assert kw["region"] == "USA" and kw["dataset_id"] == "pv1"
    assert kw["expression"] == "ts_rank(close, 5)"


@pytest.mark.asyncio
async def test_written_for_provisional(captured):
    await _persister()(MagicMock(), [_result("PASS_PROVISIONAL")])
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_skipped_without_alpha_id(captured):
    await _persister()(MagicMock(), [_result("PASS", alpha_id=None)])
    assert captured == []


@pytest.mark.asyncio
async def test_skipped_when_robustness_failed(captured):
    await _persister()(MagicMock(), [_result("PASS", robustness_failed=True)])
    assert captured == []


@pytest.mark.asyncio
async def test_skipped_for_non_pass_verdict(captured):
    await _persister()(MagicMock(), [_result("REJECTED")])
    assert captured == []


@pytest.mark.asyncio
async def test_kb_write_failure_is_soft(monkeypatch):
    """A record_success_pattern exception must NOT bubble out of persist."""
    async def _boom(self, **kw):
        raise RuntimeError("kb down")

    monkeypatch.setattr(
        "backend.agents.services.rag_service.RAGService.record_success_pattern",
        _boom,
    )
    # Should not raise.
    n = await _persister()(MagicMock(), [_result("PASS")])
    assert n == 0  # save_fn stubbed → 0 persisted, but no exception propagated
