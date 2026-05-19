"""Phase 4 Sprint 1 A1.1 — llm_mode_service unit + integration tests.

Coverage:
  - resolve_mode:
    - flag OFF (kill switch) → MODE_AUTHOR regardless of task.config
    - flag ON + task.config["llm_mode"]="assistant" → MODE_ASSISTANT
    - flag ON + task.config["llm_mode"]="author" → MODE_AUTHOR
    - flag ON + task.config missing → MODE_AUTHOR (default)
    - flag ON + invalid llm_mode value → MODE_AUTHOR (fallback)
    - broken task object → MODE_AUTHOR (soft-fail)
  - mode_change_requires_drain:
    - prev=None → False (fresh task, no residue)
    - prev=None, new=assistant → False
    - prev=author, new=assistant → True
    - prev=assistant, new=author → True
    - prev=author, new=author → False
    - non-string prev → False
  - drain_pending_residue:
    - drains all 5 configured residue keys
    - returns forensic dict {key: prior_value}
    - keeps unrelated keys (brain_role_snapshot, stop_loss_state, etc)
    - non-dict task.config → soft-fail with _error
    - empty task.config → empty drain
  - resolve_mode_and_drain_if_needed composer
  - MiningState.llm_mode_used default + assignment
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_task(config_overrides=None, *, task_id=42):
    t = MagicMock()
    t.id = task_id
    t.config = dict(config_overrides) if config_overrides else {}
    return t


def _make_settings(*, enable_assistant=True, residue_keys=None):
    ns = MagicMock()
    ns.ENABLE_LLM_ASSISTANT_MODE = enable_assistant
    ns.LLM_ASSISTANT_RESIDUE_KEYS = list(residue_keys) if residue_keys is not None else [
        "g5_pending_offspring",
        "__pending_hypothesis",
        "__g5_consumed_offspring",
        "__r1b_consumed_pending_hypothesis",
        "contextual_bandit_v1",
    ]
    return ns


# ---------------------------------------------------------------------------
# resolve_mode
# ---------------------------------------------------------------------------


def test_resolve_mode_kill_switch_forces_author():
    """ENABLE_LLM_ASSISTANT_MODE=False → MODE_AUTHOR even if task.config
    asks for assistant."""
    from backend.services.llm_mode_service import resolve_mode, MODE_AUTHOR
    task = _make_task({"llm_mode": "assistant"})
    s = _make_settings(enable_assistant=False)
    assert resolve_mode(task, settings=s) == MODE_AUTHOR


def test_resolve_mode_task_config_assistant():
    """Flag ON + task.config['llm_mode']='assistant' → MODE_ASSISTANT."""
    from backend.services.llm_mode_service import resolve_mode, MODE_ASSISTANT
    task = _make_task({"llm_mode": "assistant"})
    s = _make_settings(enable_assistant=True)
    assert resolve_mode(task, settings=s) == MODE_ASSISTANT


def test_resolve_mode_task_config_explicit_author():
    """Flag ON + task.config['llm_mode']='author' → MODE_AUTHOR
    (operator can pin author even with flag globally on)."""
    from backend.services.llm_mode_service import resolve_mode, MODE_AUTHOR
    task = _make_task({"llm_mode": "author"})
    s = _make_settings(enable_assistant=True)
    assert resolve_mode(task, settings=s) == MODE_AUTHOR


def test_resolve_mode_default_when_unset():
    """Flag ON + no llm_mode in task.config → MODE_AUTHOR (default)."""
    from backend.services.llm_mode_service import resolve_mode, MODE_AUTHOR
    task = _make_task({})
    s = _make_settings(enable_assistant=True)
    assert resolve_mode(task, settings=s) == MODE_AUTHOR


def test_resolve_mode_invalid_value_falls_back_author():
    """task.config['llm_mode']='turbo' (not in VALID_MODES) → MODE_AUTHOR."""
    from backend.services.llm_mode_service import resolve_mode, MODE_AUTHOR
    task = _make_task({"llm_mode": "turbo"})
    s = _make_settings(enable_assistant=True)
    assert resolve_mode(task, settings=s) == MODE_AUTHOR


def test_resolve_mode_soft_fails_on_broken_task():
    """Task object that raises on .config attr → MODE_AUTHOR (no crash)."""
    from backend.services.llm_mode_service import resolve_mode, MODE_AUTHOR
    task = MagicMock()
    # Configure side_effect to raise when .config is read
    type(task).config = property(lambda self: (_ for _ in ()).throw(RuntimeError("broken")))
    s = _make_settings(enable_assistant=True)
    assert resolve_mode(task, settings=s) == MODE_AUTHOR


# ---------------------------------------------------------------------------
# mode_change_requires_drain
# ---------------------------------------------------------------------------


def test_change_requires_drain_first_round_no_drain():
    """Fresh task → prev_mode=None → no drain (nothing to clear)."""
    from backend.services.llm_mode_service import (
        mode_change_requires_drain, MODE_AUTHOR, MODE_ASSISTANT,
    )
    assert mode_change_requires_drain(None, MODE_AUTHOR) is False
    assert mode_change_requires_drain(None, MODE_ASSISTANT) is False


def test_change_requires_drain_mode_flip():
    from backend.services.llm_mode_service import (
        mode_change_requires_drain, MODE_AUTHOR, MODE_ASSISTANT,
    )
    assert mode_change_requires_drain(MODE_AUTHOR, MODE_ASSISTANT) is True
    assert mode_change_requires_drain(MODE_ASSISTANT, MODE_AUTHOR) is True


def test_change_requires_drain_same_mode():
    from backend.services.llm_mode_service import (
        mode_change_requires_drain, MODE_AUTHOR, MODE_ASSISTANT,
    )
    assert mode_change_requires_drain(MODE_AUTHOR, MODE_AUTHOR) is False
    assert mode_change_requires_drain(MODE_ASSISTANT, MODE_ASSISTANT) is False


def test_change_requires_drain_non_string_prev():
    """Defensive: non-string prev → no drain (corrupted state)."""
    from backend.services.llm_mode_service import mode_change_requires_drain
    assert mode_change_requires_drain(123, "author") is False  # type: ignore
    assert mode_change_requires_drain({}, "author") is False   # type: ignore


# ---------------------------------------------------------------------------
# drain_pending_residue
# ---------------------------------------------------------------------------


def test_drain_clears_all_configured_keys():
    """All 5 residue keys popped from task.config."""
    from backend.services.llm_mode_service import drain_pending_residue
    task = _make_task({
        "g5_pending_offspring": [{"expr": "x"}],
        "__pending_hypothesis": {"statement": "y"},
        "__g5_consumed_offspring": ["a", "b"],
        "__r1b_consumed_pending_hypothesis": {"statement": "z"},
        "contextual_bandit_v1": {"state": "v"},
        # Plus an unrelated key that MUST be preserved
        "brain_role_snapshot": {"role": "consultant"},
    })
    s = _make_settings(enable_assistant=True)
    drained = drain_pending_residue(task, settings=s)

    # All 5 residue keys gone from task.config
    for k in ("g5_pending_offspring", "__pending_hypothesis",
              "__g5_consumed_offspring", "__r1b_consumed_pending_hypothesis",
              "contextual_bandit_v1"):
        assert k not in task.config, f"residue key {k} should be drained"
        assert k in drained, f"forensic dict missing {k}"

    # Unrelated key preserved
    assert task.config["brain_role_snapshot"] == {"role": "consultant"}


def test_drain_returns_prior_values_for_audit():
    """Forensic dict carries the PRIOR value of each drained key."""
    from backend.services.llm_mode_service import drain_pending_residue
    payload = [{"expr": "rank(close)"}]
    task = _make_task({"g5_pending_offspring": payload})
    s = _make_settings(enable_assistant=True)
    drained = drain_pending_residue(task, settings=s)
    assert drained["g5_pending_offspring"] == payload


def test_drain_keeps_brain_role_snapshot_and_stop_loss():
    """Explicit invariant — F-A5 fix: brain_role_snapshot + stop_loss_state
    MUST NOT be drained (they're cross-round persistent state, not
    cross-mode residue)."""
    from backend.services.llm_mode_service import drain_pending_residue
    task = _make_task({
        "g5_pending_offspring": [{"expr": "x"}],  # drained
        "brain_role_snapshot": {"role": "consultant"},  # preserved
        "stop_loss_state": {"ema": 0.05, "consecutive_zero": 2},  # preserved
    })
    s = _make_settings(enable_assistant=True)
    drain_pending_residue(task, settings=s)
    assert "g5_pending_offspring" not in task.config
    assert task.config["brain_role_snapshot"]["role"] == "consultant"
    assert task.config["stop_loss_state"]["ema"] == 0.05
    assert task.config["stop_loss_state"]["consecutive_zero"] == 2


def test_drain_empty_config_returns_empty():
    """Task with no residue keys → empty drain dict (not an error)."""
    from backend.services.llm_mode_service import drain_pending_residue
    task = _make_task({})
    s = _make_settings(enable_assistant=True)
    drained = drain_pending_residue(task, settings=s)
    assert drained == {}


def test_drain_partial_keys_drains_only_present():
    """Some residue keys present, others absent → drain only the present."""
    from backend.services.llm_mode_service import drain_pending_residue
    task = _make_task({"g5_pending_offspring": [{"x": 1}]})
    s = _make_settings(enable_assistant=True)
    drained = drain_pending_residue(task, settings=s)
    assert set(drained.keys()) == {"g5_pending_offspring"}


def test_drain_non_dict_config_soft_fails():
    """task.config not a dict → forensic dict with _error key (no crash)."""
    from backend.services.llm_mode_service import drain_pending_residue
    task = MagicMock()
    task.id = 9
    task.config = "not a dict"  # malformed
    s = _make_settings(enable_assistant=True)
    drained = drain_pending_residue(task, settings=s)
    assert "_error" in drained


def test_drain_with_explicit_key_override():
    """keys= argument overrides settings.LLM_ASSISTANT_RESIDUE_KEYS."""
    from backend.services.llm_mode_service import drain_pending_residue
    task = _make_task({"only_this_one": 42, "g5_pending_offspring": [{"x": 1}]})
    s = _make_settings(enable_assistant=True)
    drained = drain_pending_residue(task, settings=s, keys=["only_this_one"])
    assert "only_this_one" in drained
    assert "g5_pending_offspring" in task.config  # not drained (override)


# ---------------------------------------------------------------------------
# resolve_mode_and_drain_if_needed — composer
# ---------------------------------------------------------------------------


def test_composer_no_drain_on_first_round():
    """prev=None → no drain even if mode resolves to a different mode."""
    from backend.services.llm_mode_service import resolve_mode_and_drain_if_needed
    task = _make_task({"llm_mode": "assistant", "g5_pending_offspring": [{"x": 1}]})
    s = _make_settings(enable_assistant=True)
    new_mode, drained = resolve_mode_and_drain_if_needed(task, prev_mode=None, settings=s)
    assert new_mode == "assistant"
    assert drained == {}
    # Residue NOT drained
    assert "g5_pending_offspring" in task.config


def test_composer_drain_on_mode_flip():
    """prev=author → new=assistant → drain triggers."""
    from backend.services.llm_mode_service import resolve_mode_and_drain_if_needed
    task = _make_task({"llm_mode": "assistant", "g5_pending_offspring": [{"x": 1}]})
    s = _make_settings(enable_assistant=True)
    new_mode, drained = resolve_mode_and_drain_if_needed(task, prev_mode="author", settings=s)
    assert new_mode == "assistant"
    assert "g5_pending_offspring" in drained
    assert "g5_pending_offspring" not in task.config


def test_composer_no_drain_when_mode_unchanged():
    """prev=author → new=author → no drain."""
    from backend.services.llm_mode_service import resolve_mode_and_drain_if_needed
    task = _make_task({"g5_pending_offspring": [{"x": 1}]})  # no llm_mode → default author
    s = _make_settings(enable_assistant=True)
    new_mode, drained = resolve_mode_and_drain_if_needed(task, prev_mode="author", settings=s)
    assert new_mode == "author"
    assert drained == {}
    assert "g5_pending_offspring" in task.config


# ---------------------------------------------------------------------------
# MiningState.llm_mode_used field
# ---------------------------------------------------------------------------


def test_mining_state_llm_mode_used_defaults_to_author():
    from backend.agents.graph.state import MiningState
    state = MiningState(task_id=1, dataset_id="x", region="USA", universe="TOP3000")
    assert state.llm_mode_used == "author"


def test_mining_state_llm_mode_used_assignable():
    """MiningState pydantic config allows assignment to llm_mode_used."""
    from backend.agents.graph.state import MiningState
    state = MiningState(
        task_id=1, dataset_id="x", region="USA", universe="TOP3000",
        llm_mode_used="assistant",
    )
    assert state.llm_mode_used == "assistant"
    state.llm_mode_used = "author"
    assert state.llm_mode_used == "author"
