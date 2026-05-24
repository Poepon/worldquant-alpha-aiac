"""Metric-driven end-to-end test of the extracted verdict (Feature 1, 2026-05-24).

Before this, NO test drove node_evaluate with band-deciding metrics — the
extraction of compute_verdict_from_signals (559-639) + _unpack_eval_thresholds
(1803-1824 → th → _EvalCtx) had no end-to-end net. Driving the full node_evaluate
(brain=None → self_corr 'skipped', corr I/O skipped) exercises BOTH the threshold
unpacking→ctx threading AND the verdict routing, so a mis-mapped th key (e.g.
prov_turnover_min↔max) or a broken extraction surfaces as a wrong quality_status.

PG-gated like the rest of the agents integration suite (warm-up imports the stack).
"""
from __future__ import annotations

import os
import socket
from typing import Any, Dict

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
    reason="verdict integration test requires Postgres reachable",
)

import backend.tasks  # noqa: E402, F401
from backend.agents.graph.nodes.evaluation import node_evaluate  # noqa: E402
from backend.agents.graph.state import AlphaCandidate, MiningState  # noqa: E402


def _mk_state(alpha: AlphaCandidate) -> MiningState:
    return MiningState(
        task_id=1, region="USA", universe="TOP3000", dataset_id="ds1",
        pending_alphas=[alpha], hypotheses=[], fields=[],
    )


def _mk_alpha(*, sharpe, fitness, turnover, extra_checks=None, can_submit=True,
              alpha_id="a1") -> AlphaCandidate:
    checks = [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": sharpe},
        {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": fitness},
        {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": turnover},
        {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": turnover},
    ]
    if extra_checks:
        checks.extend(extra_checks)
    metrics: Dict[str, Any] = {
        "sharpe": sharpe, "fitness": fitness, "turnover": turnover,
        "returns": 0.18, "drawdown": 0.05, "checks": checks, "can_submit": can_submit,
    }
    a = AlphaCandidate(
        expression=f"ts_rank(close, 20) /* {alpha_id} */",
        is_simulated=True, simulation_success=True, alpha_id=alpha_id, metrics=metrics,
    )
    a.quality_status = "PENDING"
    return a


async def _run(alpha: AlphaCandidate) -> AlphaCandidate:
    from backend.config import settings
    orig = settings.ENABLE_REGIME
    settings.ENABLE_REGIME = False  # deterministic thresholds (no regime scaling)
    try:
        out = await node_evaluate(_mk_state(alpha), brain=None,
                                  config={"configurable": {}})
    finally:
        settings.ENABLE_REGIME = orig
    return out["pending_alphas"][0]


class TestNodeEvaluateVerdict:
    @pytest.mark.asyncio
    async def test_hard_gate_pass(self):
        """sharpe≥1.5, fitness≥1.2, turnover in band, all-PASS checks,
        submittable → PASS / hard_gate_pass (exercises th sharpe_min=1.5 etc.)."""
        a = await _run(_mk_alpha(sharpe=1.8, fitness=1.5, turnover=0.2))
        assert a.quality_status == "PASS"
        assert a.metrics.get("_routing_reason") == "hard_gate_pass"

    @pytest.mark.asyncio
    async def test_near_pass_provisional(self):
        """Between provisional (1.25/1.0) and hard (1.5/1.2) bands → near_pass.
        Pins BOTH sharpe_min and prov_sharpe_min were threaded into ctx."""
        a = await _run(_mk_alpha(sharpe=1.3, fitness=1.1, turnover=0.2))
        assert a.quality_status == "PASS_PROVISIONAL"
        assert a.metrics.get("_routing_reason") == "near_pass"

    @pytest.mark.asyncio
    async def test_concentrated_weight_fail_blocks_pass(self):
        """Strong scalars but CONCENTRATED_WEIGHT=FAIL → hard_gate + near_pass both
        blocked. The alpha routes into the optimization chain (OPTIMIZE) rather
        than PASS/PROVISIONAL — the decisive proof is it NEVER reaches PASS/
        PROVISIONAL despite passing scalars."""
        a = await _run(_mk_alpha(
            sharpe=1.8, fitness=1.5, turnover=0.2,
            extra_checks=[{"name": "CONCENTRATED_WEIGHT", "result": "FAIL",
                           "limit": 0.1, "value": 0.5}],
        ))
        assert a.quality_status in ("OPTIMIZE", "FAIL")
        assert a.quality_status not in ("PASS", "PASS_PROVISIONAL")
        # _routing_reason is only stamped for PASS/PASS_PROVISIONAL (evaluation.py:737)
        assert "_routing_reason" not in (a.metrics or {})

    @pytest.mark.asyncio
    async def test_negative_signal_fail(self):
        a = await _run(_mk_alpha(sharpe=-0.5, fitness=-0.1, turnover=0.2,
                                 can_submit=False))
        assert a.quality_status == "FAIL"
