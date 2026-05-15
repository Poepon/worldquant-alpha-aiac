"""P2-C node_evaluate × regime threshold gating integration tests (2026-05-16).

PG-only via S5 ``_pg_reachable`` + module-level pytestmark (kept consistent
with the rest of the P2-C integration suite; the actual node_evaluate path
under test is engine-agnostic but the surrounding fixtures depend on the
agents stack which we warm up the same way).

Covers:
    E1 regime threshold adjust ONLY when flag is True:
       alpha sharpe=0.95, T1 base sharpe_min=1.25, regime='crisis'.
       - flag=False → FAIL (0.95 < 1.25)
       - flag=True  → PASS (1.25 × 0.70 = 0.875, 0.95 > 0.875)
    E2 regime_at_eval stamp triggers when strategy.regime is injected
       even with STYLE_PRESET_GUIDANCE=False (S2 — stamp gated by
       regime injection, not flag combination).
"""
from __future__ import annotations

import os
import socket
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

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
    reason="P2-C regime tests require Postgres reachable",
)

# Warm-up
import backend.tasks  # noqa: E402, F401

from backend.agents.graph.nodes.evaluation import node_evaluate  # noqa: E402
from backend.agents.graph.state import AlphaCandidate, MiningState  # noqa: E402


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
    # factor_tier is a top-level attribute on MiningState (see state.py).
    s.factor_tier = factor_tier
    return s


def _mk_alpha(sharpe: float, alpha_id: str = "a1") -> AlphaCandidate:
    metrics: Dict[str, Any] = {
        "sharpe": sharpe,
        "fitness": 1.05,
        "turnover": 0.25,
        "returns": 0.18,
        "drawdown": 0.05,
        "checks": [
            {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": sharpe},
            {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.05},
            {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": 0.25},
            {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": 0.25},
        ],
        "can_submit": True,
        "_sim_settings": {
            "region": "USA", "universe": "TOP3000",
            "delay": 1, "decay": 4, "neutralization": "INDUSTRY",
        },
    }
    a = AlphaCandidate(
        expression=f"ts_rank(close, 20) /* {alpha_id} */",
        is_simulated=True,
        simulation_success=True,
        alpha_id=alpha_id,
        metrics=metrics,
    )
    a.quality_status = "PENDING"
    return a


def _crisis_strategy_blob() -> dict:
    return {
        "mode": "balanced",
        "temperature": 0.7,
        "exploration_weight": 0.5,
        "regime": "crisis",
        "style_preset": {
            "regime": "crisis",
            "style_label": "Risk-Off Defensive",
            "style_philosophy": "Capital preservation.",
            "pillar_bias": ["quality", "value", "volatility"],
        },
    }


def _elevated_strategy_blob() -> dict:
    return {
        "mode": "balanced",
        "temperature": 0.7,
        "exploration_weight": 0.5,
        "regime": "elevated",
        "style_preset": {
            "regime": "elevated",
            "style_label": "Cautious Tactical",
            "style_philosophy": "Stress-test ahead of full conviction.",
            "pillar_bias": ["quality", "momentum", "volatility"],
        },
    }


class TestNodeEvaluateRegime:

    @pytest.mark.asyncio
    async def test_regime_threshold_adjust_only_when_flag(self):
        """E1: alpha sharpe=0.95 under T1 (base sharpe_min=1.25,
        PROV sharpe_min=0.80), regime='crisis' (PASS×0.70=0.875,
        PROV×0.70=0.56).

        - flag=False → quality_status='PASS_PROVISIONAL' (PASS bar=1.25
          fails BUT PROV bar=0.80 passes; sharpe=0.95 is in [0.80, 1.25)).
        - flag=True  → quality_status='PASS' or stronger (PASS bar drops
          to 0.875, sharpe=0.95>0.875).

        The decisive comparison is **flag-on quality MUST be at least as
        strong as flag-off quality** (PASS > PASS_PROVISIONAL > OPTIMIZE
        > FAIL), proving the multipliers flipped the gate verdict.
        """
        from backend.config import settings

        _STATUS_ORDER = {
            "PASS": 4,
            "PASS_PROVISIONAL": 3,
            "OPTIMIZE": 2,
            "FAIL": 1,
            "PENDING": 0,
        }

        state_off = _mk_state([_mk_alpha(0.95, "off_a")])
        state_on = _mk_state([_mk_alpha(0.95, "on_a")])

        cfg = {
            "configurable": {
                "strategy": _crisis_strategy_blob(),
            }
        }

        original_flag = settings.ENABLE_REGIME_AWARE_THRESHOLDS

        # ---- flag OFF: PASS bar=1.25 → not PASS, but PROV bar=0.80 → PROV ----
        settings.ENABLE_REGIME_AWARE_THRESHOLDS = False
        try:
            out_off = await node_evaluate(state_off, brain=None, config=cfg)
        finally:
            settings.ENABLE_REGIME_AWARE_THRESHOLDS = original_flag
        alpha_off = out_off["pending_alphas"][0]
        # flag=OFF should NOT land at PASS (sharpe<1.25)
        assert alpha_off.quality_status != "PASS", (
            f"flag=OFF: expected NOT-PASS with sharpe=0.95<1.25, got "
            f"{alpha_off.quality_status}"
        )

        # ---- flag ON: PASS bar drops to 0.875 → sharpe=0.95 can reach PASS ----
        settings.ENABLE_REGIME_AWARE_THRESHOLDS = True
        try:
            out_on = await node_evaluate(state_on, brain=None, config=cfg)
        finally:
            settings.ENABLE_REGIME_AWARE_THRESHOLDS = original_flag
        alpha_on = out_on["pending_alphas"][0]

        # The DECISIVE assertion: flag=ON quality must be >= flag=OFF
        # quality. With sharpe=0.95 above the relaxed PASS bar=0.875 the
        # alpha should land at PASS (4) while flag=OFF lands at most
        # PASS_PROVISIONAL (3) since sharpe<1.25.
        rank_off = _STATUS_ORDER.get(alpha_off.quality_status, 0)
        rank_on = _STATUS_ORDER.get(alpha_on.quality_status, 0)
        assert rank_on > rank_off, (
            f"P2-C threshold flip didn't improve verdict: "
            f"off={alpha_off.quality_status} on={alpha_on.quality_status}"
        )
        assert alpha_on.quality_status == "PASS", (
            f"flag=ON: expected PASS with sharpe=0.95>0.875, got "
            f"{alpha_on.quality_status}"
        )

    @pytest.mark.asyncio
    async def test_regime_at_eval_stamp_independent_of_style_flag(self):
        """E2 / S2: ENABLE_REGIME_AWARE_THRESHOLDS=True +
        ENABLE_STYLE_PRESET_GUIDANCE=False + regime injected →
        alpha.metrics['_regime_at_eval'] == 'elevated'
        + alpha.metrics['_regime_applied_thresholds'] is True.

        Verifies that the stamp's trigger is regime injection (i.e.
        strategy.regime is present), NOT the STYLE flag.
        """
        from backend.config import settings

        state = _mk_state([_mk_alpha(1.50, "stamp_a")])
        cfg = {
            "configurable": {
                "strategy": _elevated_strategy_blob(),
            }
        }

        original_aware = settings.ENABLE_REGIME_AWARE_THRESHOLDS
        original_style = settings.ENABLE_STYLE_PRESET_GUIDANCE
        settings.ENABLE_REGIME_AWARE_THRESHOLDS = True
        settings.ENABLE_STYLE_PRESET_GUIDANCE = False
        try:
            out = await node_evaluate(state, brain=None, config=cfg)
        finally:
            settings.ENABLE_REGIME_AWARE_THRESHOLDS = original_aware
            settings.ENABLE_STYLE_PRESET_GUIDANCE = original_style

        alpha = out["pending_alphas"][0]
        assert isinstance(alpha.metrics, dict)
        assert alpha.metrics.get("_regime_at_eval") == "elevated", (
            f"_regime_at_eval missing/wrong: "
            f"{alpha.metrics.get('_regime_at_eval')}"
        )
        # S8 audit: AWARE=True triggered multiplier application
        assert alpha.metrics.get("_regime_applied_thresholds") is True

    @pytest.mark.asyncio
    async def test_no_stamp_when_regime_not_injected(self):
        """When strategy carries no regime (legacy path), neither stamp
        appears even if both flags are on — the stamp follows regime
        injection, not flag state."""
        from backend.config import settings

        state = _mk_state([_mk_alpha(1.50, "nostamp_a")])
        cfg = {
            "configurable": {
                "strategy": {
                    "mode": "balanced",
                    "temperature": 0.7,
                    "exploration_weight": 0.5,
                    # NO regime / style_preset
                }
            }
        }
        original_aware = settings.ENABLE_REGIME_AWARE_THRESHOLDS
        original_style = settings.ENABLE_STYLE_PRESET_GUIDANCE
        settings.ENABLE_REGIME_AWARE_THRESHOLDS = True
        settings.ENABLE_STYLE_PRESET_GUIDANCE = True
        try:
            out = await node_evaluate(state, brain=None, config=cfg)
        finally:
            settings.ENABLE_REGIME_AWARE_THRESHOLDS = original_aware
            settings.ENABLE_STYLE_PRESET_GUIDANCE = original_style

        alpha = out["pending_alphas"][0]
        assert "_regime_at_eval" not in (alpha.metrics or {})
        assert "_regime_applied_thresholds" not in (alpha.metrics or {})
