"""Fix D — empty BRAIN checks must NOT be treated as approve (2026-05-07).

Symptom: batch 284-287 mining produced 5 alpha with `_brain_can_submit=true`
at sim time, but post-refresh ALL flipped to bcs=false. Refresh re-queried
BRAIN /alphas/{id} and found CONCENTRATED_WEIGHT / LOW_SUB_UNIVERSE_SHARPE /
LOW_SHARPE / LOW_FITNESS failures that mining-time `is.checks` block didn't
contain.

Root cause: `evaluate_with_brain_checks` treats empty checks as "approve":
```
if not can_submit and not failed and not pending:
    can_submit = True
```
This fires when checks=[] (BRAIN sim completed before async checks finished).

Fix: align with backend.can_submit.compute_can_submit semantics —
  - empty checks → can_submit=False (unknown, not approve)
  - non-empty + no FAIL → True (legacy behavior)
"""
from backend.alpha_scoring import evaluate_with_brain_checks


class TestEmptyChecks:
    def test_empty_checks_returns_false_not_true(self):
        """sim_result with no checks block — common during BRAIN simulate
        completion before async checks finish. Must NOT return can_submit=True."""
        sim_result = {"is": {}, "fitness": 1.5, "sharpe": 2.0}
        result = evaluate_with_brain_checks(sim_result)
        assert result["can_submit"] is False, (
            "empty checks should be treated as unknown (False), not approve"
        )
        assert result["failed_checks"] == []
        assert result["pending_checks"] == []

    def test_empty_top_level_checks(self):
        """Top-level checks key explicitly empty list."""
        sim_result = {"checks": [], "fitness": 1.5}
        result = evaluate_with_brain_checks(sim_result)
        assert result["can_submit"] is False

    def test_no_checks_key_at_all(self):
        """sim_result without any checks structure."""
        sim_result = {"fitness": 1.5}
        result = evaluate_with_brain_checks(sim_result)
        assert result["can_submit"] is False


class TestNonEmptyChecks:
    def test_all_pass_returns_true(self):
        """Legacy positive case: all checks PASS → can_submit=True."""
        sim_result = {
            "is": {
                "checks": [
                    {"name": "LOW_SHARPE", "result": "PASS", "value": 2.0, "limit": 1.25},
                    {"name": "LOW_FITNESS", "result": "PASS", "value": 1.2, "limit": 1.0},
                    {"name": "HIGH_TURNOVER", "result": "PASS", "value": 0.4, "limit": 0.7},
                ]
            }
        }
        result = evaluate_with_brain_checks(sim_result)
        assert result["can_submit"] is True
        assert result["failed_checks"] == []

    def test_one_fail_returns_false(self):
        sim_result = {
            "is": {
                "checks": [
                    {"name": "LOW_SHARPE", "result": "PASS", "value": 2.0, "limit": 1.25},
                    {"name": "LOW_FITNESS", "result": "FAIL", "value": 0.8, "limit": 1.0},
                ]
            }
        }
        result = evaluate_with_brain_checks(sim_result)
        assert result["can_submit"] is False
        assert "LOW_FITNESS" in result["failed_checks"]

    def test_pending_blocks_can_submit(self):
        """PENDING is treated as not-yet-decided in mining path. Legacy
        behavior preserved."""
        sim_result = {
            "is": {
                "checks": [
                    {"name": "LOW_SHARPE", "result": "PASS"},
                    {"name": "SELF_CORRELATION", "result": "PENDING"},
                ]
            }
        }
        result = evaluate_with_brain_checks(sim_result)
        # mining-path is conservative: PENDING blocks
        assert result["can_submit"] is False
        assert "SELF_CORRELATION" in result["pending_checks"]


class TestProductionScenarioFromBatch284_287:
    """Reproduce the exact failure mode that hit batch 284-287."""

    def test_pk_6595_scenario_post_fix(self):
        """pk=6595: sh=0.94 fit=0.69. At sim time BRAIN returned no checks
        block (still computing). Old code → bcs=true (false positive).
        Post-fix → bcs=false (correct unknown)."""
        sim_result = {
            "is": {"sharpe": 0.94, "fitness": 0.69, "checks": []},
            "fitness": 0.69, "sharpe": 0.94,
        }
        result = evaluate_with_brain_checks(sim_result)
        assert result["can_submit"] is False, (
            "pk=6595 scenario: empty checks at sim time should be False, "
            "not the false-positive True from old logic"
        )
