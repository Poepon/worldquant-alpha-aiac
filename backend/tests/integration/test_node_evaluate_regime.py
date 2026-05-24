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
        """E1: a borderline alpha (sharpe=0.95) sits just below the PROVISIONAL
        sharpe bar with no regime, but the crisis multiplier (×0.70) relaxes the
        bars enough that it clears PROVISIONAL.

        The invariant is config-ROBUST and intentionally avoids hardcoding an
        absolute band (the prior version asserted PASS, which silently broke when
        EVAL_SHARPE_MIN moved 1.25→1.5): with the current config
        (EVAL_SHARPE_MIN=1.5, EVAL_PROVISIONAL_SHARPE_MIN=1.25) sharpe=0.95 is
        below the provisional bar 1.25 → flag-OFF lands at OPTIMIZE/FAIL; the
        crisis ×0.70 drops the provisional bar to 0.875 → flag-ON reaches
        PASS_PROVISIONAL.

        The decisive checks are therefore (a) flag-ON verdict is STRICTLY
        stronger than flag-OFF (PASS > PASS_PROVISIONAL > OPTIMIZE > FAIL),
        proving the multipliers flipped the gate, and (b) flag-ON reaches at
        least PASS_PROVISIONAL (the alpha entered the pass pool). Neither pins an
        exact band, so this survives future threshold-config moves.
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

        # (Consolidated 2026-05-19: ENABLE_REGIME_AWARE_THRESHOLDS now derived
        # from ENABLE_REGIME + REGIME_STAGE. AWARE_THRESHOLDS truthy ⇔
        # ENABLE_REGIME=True AND REGIME_STAGE in {"thresholds","style"}.)
        original_enabled = settings.ENABLE_REGIME
        original_stage = settings.REGIME_STAGE

        # ---- flag OFF: sharpe=0.95 below the provisional bar → not in pass pool ----
        settings.ENABLE_REGIME = False
        try:
            out_off = await node_evaluate(state_off, brain=None, config=cfg)
        finally:
            settings.ENABLE_REGIME = original_enabled
            settings.REGIME_STAGE = original_stage
        alpha_off = out_off["pending_alphas"][0]
        # flag=OFF must not reach the pass pool (sharpe below provisional bar)
        assert alpha_off.quality_status not in ("PASS", "PASS_PROVISIONAL"), (
            f"flag=OFF: expected below-provisional with sharpe=0.95, got "
            f"{alpha_off.quality_status}"
        )

        # ---- flag ON: crisis ×0.70 relaxes the provisional bar → reaches pass pool ----
        settings.ENABLE_REGIME = True
        settings.REGIME_STAGE = "thresholds"
        try:
            out_on = await node_evaluate(state_on, brain=None, config=cfg)
        finally:
            settings.ENABLE_REGIME = original_enabled
            settings.REGIME_STAGE = original_stage
        alpha_on = out_on["pending_alphas"][0]

        # DECISIVE (config-robust): flag=ON verdict is STRICTLY stronger than
        # flag=OFF, AND flag=ON reaches at least PASS_PROVISIONAL — proving the
        # multipliers relaxed the gate. We deliberately do NOT assert an exact
        # band (that coupling to EVAL_SHARPE_MIN is what broke the old version).
        rank_off = _STATUS_ORDER.get(alpha_off.quality_status, 0)
        rank_on = _STATUS_ORDER.get(alpha_on.quality_status, 0)
        assert rank_on > rank_off, (
            f"P2-C threshold flip didn't improve verdict: "
            f"off={alpha_off.quality_status} on={alpha_on.quality_status}"
        )
        assert rank_on >= _STATUS_ORDER["PASS_PROVISIONAL"], (
            f"flag=ON: expected at least PASS_PROVISIONAL, got "
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

        # (Consolidated 2026-05-19: AWARE=True + STYLE=False ⇔ REGIME_STAGE="thresholds".)
        original_enabled = settings.ENABLE_REGIME
        original_stage = settings.REGIME_STAGE
        settings.ENABLE_REGIME = True
        settings.REGIME_STAGE = "thresholds"
        try:
            out = await node_evaluate(state, brain=None, config=cfg)
        finally:
            settings.ENABLE_REGIME = original_enabled
            settings.REGIME_STAGE = original_stage

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
        # (Consolidated 2026-05-19: AWARE=True + STYLE=True ⇔ REGIME_STAGE="style".)
        original_enabled = settings.ENABLE_REGIME
        original_stage = settings.REGIME_STAGE
        settings.ENABLE_REGIME = True
        settings.REGIME_STAGE = "style"
        try:
            out = await node_evaluate(state, brain=None, config=cfg)
        finally:
            settings.ENABLE_REGIME = original_enabled
            settings.REGIME_STAGE = original_stage

        alpha = out["pending_alphas"][0]
        assert "_regime_at_eval" not in (alpha.metrics or {})
        assert "_regime_applied_thresholds" not in (alpha.metrics or {})
