"""Fix C — BRAIN-aware PASS downgrade + should_optimize override (2026-05-07).

Problem:
- 8 PASS alpha in tasks 276-283 all had can_submit=False, even sharpe=2.08.
- Root cause: internal PASS gate (sharpe>=1.0/fitness>=0.5) is a 探索 bar,
  BRAIN submission gate is stricter (top-level fitness>=~1.0, weight<=10%).
- should_optimize used train_fitness (>=1.0 for some) but BRAIN evaluates
  top-level fitness (0.80 for the same alpha). Result: "已接近/达到门槛...
  跳过优化" fires for BRAIN-rejected alphas, killing the only refinement loop.

Fix:
1. should_optimize: read sim_result.checks; if BRAIN reports
   LOW_FITNESS/LOW_SHARPE/CONCENTRATED_WEIGHT FAIL, force return True with
   a wrapper-prioritizing reason BEFORE the internal-IS skip branches.
2. evaluation.py: when hard_gate_pass=True but brain_can_submit=False on
   actionable failures, downgrade PASS -> PASS_PROVISIONAL so the alpha
   enters _collect_optimization_candidates.
"""
from backend.alpha_scoring import should_optimize


def _make_sim_result(*, train_sharpe, train_fitness, top_fitness, brain_checks):
    """Mimic BRAIN sim_result shape used by should_optimize."""
    return {
        "train": {"sharpe": train_sharpe, "fitness": train_fitness},
        "is_stats": [{"sharpe": train_sharpe, "fitness": train_fitness}],
        "is": {"checks": brain_checks},
        "fitness": top_fitness,
        "sharpe": train_sharpe,
    }


class TestShouldOptimizeBrainOverride:
    def test_brain_low_fitness_overrides_skip(self):
        """pk=6589 scenario: train_sh=2.48 train_fit=1.02 (passes internal
        skip threshold) but BRAIN top-level fitness=0.80 -> LOW_FITNESS FAIL.
        Old behavior returned False ('skip optimize'). Fix returns True."""
        sr = _make_sim_result(
            train_sharpe=2.48, train_fitness=1.02, top_fitness=0.80,
            brain_checks=[
                {"name": "LOW_FITNESS", "result": "FAIL", "limit": 1.0, "value": 0.80},
                {"name": "SELF_CORRELATION", "result": "PENDING"},
            ],
        )
        should_opt, reason = should_optimize(sr)
        assert should_opt is True, f"expected True, got ({should_opt}, {reason})"
        assert "BRAIN" in reason
        assert "LOW_FITNESS" in reason

    def test_brain_concentrated_weight_triggers_wrapper_hint(self):
        """CONCENTRATED_WEIGHT FAIL -> reason carries '集中' so
        optimization_chain._determine_optimization_priorities prioritizes
        wrappers (winsorize)."""
        sr = _make_sim_result(
            train_sharpe=2.0, train_fitness=1.5, top_fitness=0.95,
            brain_checks=[
                {"name": "CONCENTRATED_WEIGHT", "result": "FAIL", "limit": 0.10, "value": 0.50},
            ],
        )
        should_opt, reason = should_optimize(sr)
        assert should_opt is True
        assert "集中" in reason or "concentrat" in reason.lower()

    def test_brain_multiple_actionable_fails(self):
        """LOW_FITNESS + CONCENTRATED_WEIGHT both FAIL -> single combined
        reason. Concentration takes wrapper-priority precedence."""
        sr = _make_sim_result(
            train_sharpe=2.48, train_fitness=1.02, top_fitness=0.80,
            brain_checks=[
                {"name": "LOW_FITNESS", "result": "FAIL", "limit": 1.0, "value": 0.80},
                {"name": "CONCENTRATED_WEIGHT", "result": "FAIL", "limit": 0.10, "value": 0.50},
            ],
        )
        should_opt, reason = should_optimize(sr)
        assert should_opt is True
        assert "LOW_FITNESS" in reason
        assert "CONCENTRATED_WEIGHT" in reason

    def test_brain_pending_check_is_not_actionable(self):
        """PENDING (SELF_CORRELATION) is not a FAIL — should not trigger
        the BRAIN override; falls through to existing logic."""
        sr = _make_sim_result(
            train_sharpe=2.48, train_fitness=1.02, top_fitness=1.05,
            brain_checks=[
                {"name": "SELF_CORRELATION", "result": "PENDING"},
                {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": 2.0},
            ],
        )
        should_opt, reason = should_optimize(sr)
        # Internal heuristic kicks in: train_sh=2.48 / train_fit=1.02 with no
        # OS data -> 'OS 暂未跑，跳过优化'
        assert should_opt is False
        assert "BRAIN" not in reason

    def test_brain_pass_check_is_not_actionable(self):
        """LOW_FITNESS with result=PASS is not a fail — no override."""
        sr = _make_sim_result(
            train_sharpe=2.0, train_fitness=1.0, top_fitness=1.5,
            brain_checks=[
                {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.5},
            ],
        )
        should_opt, reason = should_optimize(sr)
        assert "BRAIN" not in reason  # falls through to internal heuristic

    def test_no_brain_checks_keeps_legacy_behavior(self):
        """sim_result with no checks key -> legacy heuristic only.
        train_sh=2.48 / train_fit=1.02 / no OS -> skip."""
        sr = {
            "train": {"sharpe": 2.48, "fitness": 1.02},
            "is_stats": [{"sharpe": 2.48, "fitness": 1.02}],
        }
        should_opt, reason = should_optimize(sr)
        assert should_opt is False
        assert "BRAIN" not in reason

    def test_brain_low_sharpe_actionable(self):
        """LOW_SHARPE FAIL -> trigger window/decay path."""
        sr = _make_sim_result(
            train_sharpe=1.0, train_fitness=0.6, top_fitness=0.8,
            brain_checks=[
                {"name": "LOW_SHARPE", "result": "FAIL", "limit": 1.25, "value": 1.0},
            ],
        )
        should_opt, reason = should_optimize(sr)
        assert should_opt is True
        assert "LOW_SHARPE" in reason
        assert "fitness" in reason or "稳健" in reason
