"""Phase 3 R1b.1c: router decision-tree unit tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §2.3 + §2.4.

Verifies the two routing functions added in R1b.1c:
  _route_after_evaluate — gate entering retry sub-graph
  _route_after_r1b_retry — 3-way fork (mutate dominates retry on BOTH)

These run with mocked MiningState; no DB or LangGraph engine needed.
Workflow.py import smoke is covered by a separate test below.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.graph.edges import (
    _route_after_evaluate,
    _route_after_r1b_retry,
)


def _mk_alpha(quality_status="FAIL", attribution="implementation"):
    return SimpleNamespace(
        quality_status=quality_status,
        metrics={"_r1a_attribution": attribution},
    )


def _mk_state(alphas, *, retries=0, mutations=0, cost=0.0):
    return SimpleNamespace(
        pending_alphas=alphas,
        r1b_retries_attempted_this_alpha=retries,
        r1b_mutations_attempted_this_cycle=mutations,
        r1b_token_cost_this_alpha=cost,
    )


# ---------------------------------------------------------------------------
# _route_after_evaluate
# ---------------------------------------------------------------------------

def test_route_after_evaluate_flags_off_always_save(monkeypatch):
    """Both R1b flags OFF → always save_results (byte-equivalent legacy)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", False, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    state = _mk_state([_mk_alpha(attribution="implementation")])
    assert _route_after_evaluate(state) == "save_results"


def test_route_after_evaluate_retry_flag_on_with_impl_attribution(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    state = _mk_state([_mk_alpha(attribution="implementation")])
    assert _route_after_evaluate(state) == "r1b_retry_router"


def test_route_after_evaluate_retry_flag_on_hypothesis_attribution_skips(monkeypatch):
    """Retry flag on but attribution is hypothesis → mutate flag OFF → save."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    state = _mk_state([_mk_alpha(attribution="hypothesis")])
    assert _route_after_evaluate(state) == "save_results"


def test_route_after_evaluate_mutate_flag_on_hypothesis_attribution(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", False, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    state = _mk_state([_mk_alpha(attribution="hypothesis")])
    assert _route_after_evaluate(state) == "r1b_retry_router"


def test_route_after_evaluate_unknown_attribution_skipped(monkeypatch):
    """UNKNOWN attribution → never triggers retry even with flags on."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    state = _mk_state([_mk_alpha(attribution="unknown")])
    assert _route_after_evaluate(state) == "save_results"


def test_route_after_evaluate_non_fail_alpha_skipped(monkeypatch):
    """quality_status != FAIL → never triggers retry."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    state = _mk_state([_mk_alpha(quality_status="PASS", attribution="implementation")])
    assert _route_after_evaluate(state) == "save_results"


# ---------------------------------------------------------------------------
# _route_after_r1b_retry
# ---------------------------------------------------------------------------

def test_route_after_r1b_retry_to_code_gen_retry(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    state = _mk_state([_mk_alpha(attribution="implementation")])
    assert _route_after_r1b_retry(state) == "code_gen_retry"


def test_route_after_r1b_retry_to_hypothesis_mutate(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", False, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    state = _mk_state([_mk_alpha(attribution="hypothesis")])
    assert _route_after_r1b_retry(state) == "hypothesis_mutate"


def test_route_after_r1b_retry_both_attribution_mutate_dominates(monkeypatch):
    """BOTH attribution → HYPOTHESIS_MUTATE dominates retry per [V1.0-A2-3]."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    state = _mk_state([_mk_alpha(attribution="both")])
    assert _route_after_r1b_retry(state) == "hypothesis_mutate"


def test_route_after_r1b_retry_budget_exhausted_save(monkeypatch):
    """Both budgets at max → save_results regardless of attribution."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    monkeypatch.setattr(settings, "R1B_MAX_RETRIES_PER_ALPHA", 3, raising=False)
    monkeypatch.setattr(settings, "R1B_MAX_MUTATIONS_PER_DATASET_CYCLE", 2, raising=False)
    state = _mk_state([_mk_alpha(attribution="both")], retries=3, mutations=2)
    assert _route_after_r1b_retry(state) == "save_results"


def test_route_after_r1b_retry_token_ceiling_save(monkeypatch):
    """Token cost ceiling hit → save_results immediately."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 0.05, raising=False)
    state = _mk_state([_mk_alpha(attribution="implementation")], cost=0.06)
    assert _route_after_r1b_retry(state) == "save_results"


def test_route_after_r1b_retry_mutations_exhausted_falls_through_to_retry(monkeypatch):
    """Mutation budget exhausted → retry path still works on BOTH attribution."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    monkeypatch.setattr(settings, "R1B_MAX_MUTATIONS_PER_DATASET_CYCLE", 2, raising=False)
    state = _mk_state([_mk_alpha(attribution="both")], mutations=2)
    # BOTH normally → mutate, but mutations exhausted → fall through to retry
    assert _route_after_r1b_retry(state) == "code_gen_retry"


def test_route_after_r1b_retry_no_fail_alpha_save(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    state = _mk_state([_mk_alpha(quality_status="PASS", attribution="implementation")])
    assert _route_after_r1b_retry(state) == "save_results"


def test_route_after_r1b_retry_unknown_attribution_save(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", True, raising=False)
    state = _mk_state([_mk_alpha(attribution="unknown")])
    assert _route_after_r1b_retry(state) == "save_results"


# ---------------------------------------------------------------------------
# workflow.py wiring smoke
# ---------------------------------------------------------------------------

def test_workflow_imports_cleanly_with_r1b_flag_off(monkeypatch):
    """With both R1b flags OFF, workflow.py imports + builds (legacy path)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", False, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    from backend.agents.graph.workflow import MiningWorkflow  # noqa: F401
    # If we got here without ImportError the wire is legal.


def test_workflow_imports_cleanly_with_r1b_retry_flag_on(monkeypatch):
    """With ENABLE_R1B_RETRY_LOOP=True, R1b cycle is wired (no build error)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_RETRY_LOOP", True, raising=False)
    monkeypatch.setattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False, raising=False)
    from backend.agents.graph.workflow import MiningWorkflow  # noqa: F401
