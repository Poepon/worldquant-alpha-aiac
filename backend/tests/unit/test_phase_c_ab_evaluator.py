"""Unit tests for the Phase C per-node LLM-routing A/B evaluator pure functions
(scripts/phase_c_llm_routing_ab.py). No DB — exercises assemble_arm + decide_ab.

Covers the decision matrix the operator relies on:
  - clear GO (treatment higher PASS-per-real-sim, valid arms, cost OK)
  - clear NO-GO (treatment worse)
  - INVALID (treatment ran the SAME model as control → override didn't take)
  - sample-starved binary → defers to the higher-power in-sample-sharpe signal
  - cost guardrail downgrades a quality-GO to PARTIAL when $/PASS blows up
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.phase_c_llm_routing_ab import assemble_arm, decide_ab  # noqa: E402


def _raw(*, passes, den_alphas, den_fail, models, total_cost, sharpe_n=0,
         sharpe_mean=None, sharpe_var=None, tokens=0, node_cost=None,
         model_counts=None):
    # model_counts defaults to 1 call per listed model (routed dominates) so the
    # routed_share contamination check passes unless a test sets it explicitly.
    counts = model_counts if model_counts is not None else {m: 1 for m in models}
    return {
        "passes": passes, "den_alphas": den_alphas, "den_fail": den_fail,
        "sharpe_n": sharpe_n, "sharpe_mean": sharpe_mean, "sharpe_var": sharpe_var,
        "node_calls": 10, "node_tokens": tokens, "node_cost_usd": node_cost,
        "node_latency_ms": 100.0, "node_models": models, "node_model_counts": counts,
        "total_cost_usd": total_cost, "total_tokens": tokens,
    }


# --------------------------------------------------------------------------- assemble
def test_assemble_arm_rates_and_cost():
    arm = assemble_arm(_raw(passes=10, den_alphas=200, den_fail=300,
                            models=["deepseek-chat"], total_cost=20.0))
    assert arm["real_sims"] == 500
    assert arm["pass_rate"] == round(10 / 500, 4)
    assert arm["cost_per_pass_usd"] == round(20.0 / 10, 4)


def test_assemble_arm_zero_pass_is_safe():
    arm = assemble_arm(_raw(passes=0, den_alphas=0, den_fail=0,
                            models=[], total_cost=0.0))
    assert arm["real_sims"] == 0
    assert arm["pass_rate"] is None
    assert arm["cost_per_pass_usd"] is None  # no div-by-zero


# --------------------------------------------------------------------------- decide
def test_decide_clear_go():
    control = assemble_arm(_raw(passes=10, den_alphas=200, den_fail=300,
                                models=["deepseek-chat"], total_cost=10.0))
    treatment = assemble_arm(_raw(passes=40, den_alphas=200, den_fail=300,
                                  models=["qwen3.6-plus"], total_cost=20.0))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["validity"] == "OK"
    assert v["binary_gate"] == "GO"
    assert v["decision"] == "GO"
    assert v["effect_pct_pts"] > 0


def test_decide_clear_no_go():
    control = assemble_arm(_raw(passes=40, den_alphas=200, den_fail=300,
                                models=["deepseek-chat"], total_cost=10.0))
    treatment = assemble_arm(_raw(passes=10, den_alphas=200, den_fail=300,
                                  models=["qwen3.6-plus"], total_cost=10.0))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["binary_gate"] == "NO-GO"
    assert v["decision"] == "NO-GO"


def test_decide_invalid_same_model():
    # treatment ran the SAME model as control → override never took → INVALID
    control = assemble_arm(_raw(passes=10, den_alphas=200, den_fail=300,
                                models=["deepseek-chat"], total_cost=10.0))
    treatment = assemble_arm(_raw(passes=40, den_alphas=200, den_fail=300,
                                  models=["deepseek-chat"], total_cost=10.0))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["validity"] == "INVALID_SAME_MODEL"
    assert v["decision"] == "INVALID"


def test_decide_no_node_calls_invalid():
    control = assemble_arm(_raw(passes=10, den_alphas=200, den_fail=300,
                                models=["deepseek-chat"], total_cost=10.0))
    treatment = assemble_arm(_raw(passes=40, den_alphas=200, den_fail=300,
                                  models=[], total_cost=10.0))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["validity"] == "NO_NODE_CALLS"
    assert v["decision"] == "INVALID"


def test_decide_sample_starved_positive_sharpe_capped_at_partial():
    # real_sims < 100 per arm → binary insufficient → defer to sharpe. A positive
    # in-sample-sharpe effect is NOT trusted as a hard GO (survivorship-laden, thin
    # n) → capped at PARTIAL.
    control = assemble_arm(_raw(passes=1, den_alphas=30, den_fail=0,
                                models=["deepseek-chat"], total_cost=5.0,
                                sharpe_n=30, sharpe_mean=0.8, sharpe_var=0.04))
    treatment = assemble_arm(_raw(passes=4, den_alphas=30, den_fail=0,
                                  models=["qwen3.6-plus"], total_cost=5.0,
                                  sharpe_n=30, sharpe_mean=1.2, sharpe_var=0.04))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["insufficient_sample"] is True
    assert v["sharpe"]["cohens_d"] is not None and v["sharpe"]["cohens_d"] > 0
    assert v["sharpe"]["p_value"] < 0.05
    assert v["decision"] == "PARTIAL"  # positive sharpe never a hard GO at thin n


def test_decide_sample_starved_negative_sharpe_is_no_go():
    # Protective direction: a significant NEGATIVE in-sample-sharpe effect at thin
    # n IS trusted as NO-GO (don't ship a regression).
    control = assemble_arm(_raw(passes=4, den_alphas=30, den_fail=0,
                                models=["deepseek-chat"], total_cost=5.0,
                                sharpe_n=30, sharpe_mean=1.2, sharpe_var=0.04))
    treatment = assemble_arm(_raw(passes=1, den_alphas=30, den_fail=0,
                                  models=["qwen3.6-plus"], total_cost=5.0,
                                  sharpe_n=30, sharpe_mean=0.8, sharpe_var=0.04))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["insufficient_sample"] is True
    assert v["sharpe"]["cohens_d"] < 0 and v["sharpe"]["p_value"] < 0.05
    assert v["decision"] == "NO-GO"


def test_decide_small_positive_effect_ci_crosses_zero_is_partial():
    # The lax-GO fix: a small positive point effect whose 80% CI straddles 0 must
    # be PARTIAL, NOT GO (GO requires the CI lower bound strictly > 0).
    control = assemble_arm(_raw(passes=15, den_alphas=600, den_fail=0,
                                models=["deepseek-chat"], total_cost=10.0))
    treatment = assemble_arm(_raw(passes=18, den_alphas=600, den_fail=0,
                                  models=["qwen3.6-plus"], total_cost=10.0))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["effect_pct_pts"] > 0          # positive point estimate
    assert v["ci"]["ci_lower"] <= 0         # but CI crosses zero
    assert v["binary_gate"] == "PARTIAL"
    assert v["decision"] == "PARTIAL"


def test_decide_contaminated_when_routed_share_low():
    # Override "took" (routed model present) but a brown-out fell back to default
    # on most calls → routed_share below threshold → CONTAMINATED, not OK.
    control = assemble_arm(_raw(passes=10, den_alphas=200, den_fail=300,
                                models=["deepseek-chat"], total_cost=10.0,
                                model_counts={"deepseek-chat": 100}))
    treatment = assemble_arm(_raw(passes=40, den_alphas=200, den_fail=300,
                                  models=["qwen3.6-plus", "deepseek-chat"], total_cost=20.0,
                                  model_counts={"qwen3.6-plus": 30, "deepseek-chat": 70}))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["routed_share"] == 0.3
    assert v["validity"] == "CONTAMINATED"
    assert v["decision"] == "INVALID"


def test_decide_ok_when_routed_share_high_despite_some_fallback():
    # A few brown-out fallbacks are tolerated: routed_share ≥ 0.90 → still OK.
    control = assemble_arm(_raw(passes=10, den_alphas=200, den_fail=300,
                                models=["deepseek-chat"], total_cost=10.0,
                                model_counts={"deepseek-chat": 100}))
    treatment = assemble_arm(_raw(passes=40, den_alphas=200, den_fail=300,
                                  models=["qwen3.6-plus", "deepseek-chat"], total_cost=20.0,
                                  model_counts={"qwen3.6-plus": 95, "deepseek-chat": 5}))
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["routed_share"] == 0.95
    assert v["validity"] == "OK"


def test_decide_unpriced_model_cost_unknown_never_false_ok():
    # An unpriced routed model → total_cost None → cost_per_pass None → cost flag
    # stays "unknown" (never a false "OK" that masks an expensive model).
    control = assemble_arm(_raw(passes=40, den_alphas=200, den_fail=300,
                                models=["deepseek-chat"], total_cost=20.0))
    treatment = assemble_arm(_raw(passes=50, den_alphas=200, den_fail=300,
                                  models=["new-unpriced-model"], total_cost=None))
    assert treatment["cost_per_pass_usd"] is None
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["cost"]["flag"] == "unknown"


def test_decide_cost_guardrail_downgrades_go_to_partial():
    # treatment wins on PASS-rate with a CI strictly > 0 (real GO under the
    # corrected lo>0 rule) but costs much more per PASS → cost guardrail
    # downgrades GO → PARTIAL. Needs a big, significant PASS gap so the binary
    # is GO before the cost check fires.
    control = assemble_arm(_raw(passes=40, den_alphas=500, den_fail=0,
                                models=["deepseek-chat"], total_cost=20.0))   # cpp=0.5
    treatment = assemble_arm(_raw(passes=120, den_alphas=500, den_fail=0,
                                  models=["claude-opus-4-7"], total_cost=100.0))  # cpp≈0.83
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["binary_gate"] == "GO"          # ~16pp gap, CI lower > 0
    assert v["cost"]["flag"] == "WORSE"
    assert v["decision"] == "PARTIAL"
