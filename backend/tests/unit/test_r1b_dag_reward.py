"""Phase 3 R1b.5: R6 DAG retry-aware reward tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §9.4.

Final R1b sub-phase. Verifies the retry-aware reward bonus added to
compute_reward_for_node:
  - Flag OFF → no bonus (byte-equivalent legacy)
  - Flag ON + base > 0.6 + non-empty retry_chain → +0.10 per chain element
  - Flag ON + base <= 0.6 (failed alpha) → no bonus (reward only successful retries)
  - Bonus capped at 1.0
  - Empty / missing retry_chain → no bonus
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.graph.dag_state import compute_reward_for_node


def _mk_alpha(*, sharpe=None, composite=None, retry_chain=None):
    metrics = {}
    if sharpe is not None:
        metrics["sharpe"] = sharpe
    if composite is not None:
        metrics["composite_score"] = composite
    if retry_chain is not None:
        metrics["_r1b_retry_chain"] = retry_chain
    return SimpleNamespace(metrics=metrics)


# ---------------------------------------------------------------------------
# Flag-OFF byte-equivalent legacy behavior
# ---------------------------------------------------------------------------

def test_flag_off_no_bonus_applied(monkeypatch):
    """ENABLE_R1B_DAG_RETRY_REWARD=False → reward unchanged from legacy."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", False, raising=False)
    alpha = _mk_alpha(composite=0.8, retry_chain=["expr1", "expr2"])
    assert compute_reward_for_node(alpha) == 0.8


def test_flag_off_legacy_path_unchanged_for_no_retry(monkeypatch):
    """Flag OFF + no retry chain → standard composite reward."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", False, raising=False)
    alpha = _mk_alpha(composite=0.75)
    assert compute_reward_for_node(alpha) == 0.75


# ---------------------------------------------------------------------------
# Flag-ON retry bonus
# ---------------------------------------------------------------------------

def test_flag_on_applies_bonus_for_successful_retry(monkeypatch):
    """base > 0.6 + 2-element retry chain → +0.20 bonus."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", True, raising=False)
    alpha = _mk_alpha(composite=0.7, retry_chain=["expr1", "expr2"])
    # 0.7 + 0.10 * 2 = 0.90
    assert compute_reward_for_node(alpha) == pytest.approx(0.90, abs=1e-6)


def test_flag_on_no_bonus_for_failed_retry_chain(monkeypatch):
    """base <= 0.6 (failed alpha) → no bonus even with retry chain."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", True, raising=False)
    alpha = _mk_alpha(composite=0.4, retry_chain=["expr1", "expr2"])
    # base 0.4 → no bonus (reward only successful retries per plan §9.2)
    assert compute_reward_for_node(alpha) == 0.4


def test_flag_on_no_bonus_when_retry_chain_empty(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", True, raising=False)
    alpha = _mk_alpha(composite=0.8, retry_chain=[])
    assert compute_reward_for_node(alpha) == 0.8


def test_flag_on_bonus_caps_at_one(monkeypatch):
    """0.95 + 0.10*3 = 1.25 → capped at 1.0."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", True, raising=False)
    alpha = _mk_alpha(composite=0.95, retry_chain=["a", "b", "c"])
    assert compute_reward_for_node(alpha) == 1.0


def test_flag_on_handles_missing_retry_chain_key(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", True, raising=False)
    alpha = _mk_alpha(composite=0.8)  # no retry_chain key at all
    assert compute_reward_for_node(alpha) == 0.8


def test_flag_on_node_dict_falls_through_no_bonus(monkeypatch):
    """Node dict path (no .metrics) → returns node['reward'] unchanged."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", True, raising=False)
    node = {"reward": 0.7, "_r1b_retry_chain": ["a", "b"]}
    assert compute_reward_for_node(node) == 0.7


def test_flag_on_invalid_retry_chain_type_no_bonus(monkeypatch):
    """Non-list retry_chain (corrupted data) → no bonus."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_DAG_RETRY_REWARD", True, raising=False)
    alpha = SimpleNamespace(metrics={"composite_score": 0.8, "_r1b_retry_chain": "not-a-list"})
    assert compute_reward_for_node(alpha) == 0.8
