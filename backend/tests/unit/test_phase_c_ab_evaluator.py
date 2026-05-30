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
         sharpe_mean=None, sharpe_var=None, tokens=0, node_cost=None):
    return {
        "passes": passes, "den_alphas": den_alphas, "den_fail": den_fail,
        "sharpe_n": sharpe_n, "sharpe_mean": sharpe_mean, "sharpe_var": sharpe_var,
        "node_calls": 10, "node_tokens": tokens, "node_cost_usd": node_cost,
        "node_latency_ms": 100.0, "node_models": models,
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


def test_decide_sample_starved_defers_to_sharpe_go():
    # real_sims < 100 per arm → binary insufficient → defer to sharpe (sig, d>0)
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
    assert v["decision"] == "GO"  # deferred to the higher-power continuous signal


def test_decide_cost_guardrail_downgrades_go_to_partial():
    # treatment wins on PASS-rate but costs ~4x per PASS → GO downgraded to PARTIAL
    control = assemble_arm(_raw(passes=40, den_alphas=200, den_fail=300,
                                models=["deepseek-chat"], total_cost=20.0))   # cpp=0.5
    treatment = assemble_arm(_raw(passes=50, den_alphas=200, den_fail=300,
                                  models=["claude-opus-4-7"], total_cost=100.0))  # cpp=2.0
    v = decide_ab(control, treatment, node="code_gen", seed=42)
    assert v["binary_gate"] == "GO"
    assert v["cost"]["flag"] == "WORSE"
    assert v["decision"] == "PARTIAL"
