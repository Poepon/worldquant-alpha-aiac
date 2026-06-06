"""Phase 1.5-Pydantic (plan v1.3 §4) — TaskConfig schema tests.

Covers:
- Empty + happy-path parses for TaskConfig + sub-models
- [V1.2-B5] All 3 sub-models (BrainRoleSnapshot, ContextualBanditState,
  WatchdogReviveInfo) MUST explicitly allow extra keys (Pydantic v2
  default 'ignore' would silently drop Phase 3+ schema additions)
- hypothesis_centric_variant Union[int, str] accepts both forms
- Unknown top-level keys tolerated (extra='allow')
- model_dump round-trip safe
- Phase 1 R2/Q7 ContextualBanditState shape matches evolution_strategy
  to_dict output
"""
from __future__ import annotations

import pytest

from backend.schemas.task_config import (
    BrainRoleSnapshot,
    ContextualBanditState,
    TaskConfig,
    WatchdogReviveInfo,
)


# ---------------------------------------------------------------------------
# TaskConfig empty + basic parses
# ---------------------------------------------------------------------------

class TestTaskConfigBasic:
    def test_empty_dict_parses(self):
        cfg = TaskConfig.model_validate({})
        assert cfg.brain_role_snapshot is None
        assert cfg.contextual_bandit_v1 is None
        assert cfg.hypothesis_centric_variant is None
        assert cfg.watchdog_revive is None
        assert cfg.cascade_lock_token is None

    def test_default_construction(self):
        cfg = TaskConfig()
        assert cfg.brain_role_snapshot is None

    def test_hypothesis_centric_variant_int(self):
        cfg = TaskConfig.model_validate({"hypothesis_centric_variant": 2})
        assert cfg.hypothesis_centric_variant == 2

    def test_hypothesis_centric_variant_str(self):
        # Old code emitted string variants like "treatment_A"
        cfg = TaskConfig.model_validate({"hypothesis_centric_variant": "treatment_A"})
        assert cfg.hypothesis_centric_variant == "treatment_A"

    def test_cascade_lock_token_str(self):
        cfg = TaskConfig.model_validate({"cascade_lock_token": "tok123"})
        assert cfg.cascade_lock_token == "tok123"


# ---------------------------------------------------------------------------
# [V1.2-B5] Sub-models all explicit extra='allow'
# ---------------------------------------------------------------------------

class TestSubModelExtraAllow:
    def test_brain_role_snapshot_allows_extra_field(self):
        """Phase 3+ may add new role-snapshot fields without code change."""
        snap = BrainRoleSnapshot(
            brain_consultant_mode_at_start=True,
            effective_default_test_period="P0Y",
            effective_sharpe_submit_min=1.58,
            effective_region_universes={},
            future_phase3_key="x",  # type: ignore[call-arg]
        )
        # extra='allow' preserves the unknown key as model_extra
        dump = snap.model_dump()
        assert dump.get("future_phase3_key") == "x", (
            "BrainRoleSnapshot.extra='allow' missing — Pydantic v2 default "
            "would silently drop future_phase3_key"
        )

    def test_contextual_bandit_state_allows_extra_field(self):
        state = ContextualBanditState(
            arm_names=["a", "b"],
            future_phase2_field={"reward_buffer": [1.0, 2.0]},  # type: ignore[call-arg]
        )
        dump = state.model_dump()
        assert dump.get("future_phase2_field") == {"reward_buffer": [1.0, 2.0]}

    def test_watchdog_revive_info_allows_extra_field(self):
        info = WatchdogReviveInfo(
            at="2026-05-17T12:00:00",
            kind="CONTINUOUS_CASCADE",
            original_round_idx=42,  # type: ignore[call-arg]
        )
        dump = info.model_dump()
        assert dump.get("original_round_idx") == 42


# ---------------------------------------------------------------------------
# BrainRoleSnapshot — Phase 0 P3-Brain shape from mining_tasks.py:225 writer
# ---------------------------------------------------------------------------

class TestBrainRoleSnapshot:
    @pytest.fixture
    def sample_payload(self):
        # Mirrors backend/tasks/mining_tasks.py:225-234 writer shape
        return {
            "brain_consultant_mode_at_start": False,
            "effective_default_test_period": "P3Y",
            "effective_sharpe_submit_min": 1.25,
            "effective_region_universes": {
                "USA": ["TOP3000"],
                "EUR": ["TOP1000"],
            },
        }

    def test_parses_phase0_writer_payload(self, sample_payload):
        snap = BrainRoleSnapshot.model_validate(sample_payload)
        assert snap.brain_consultant_mode_at_start is False
        assert snap.effective_default_test_period == "P3Y"
        assert snap.effective_sharpe_submit_min == 1.25
        assert snap.effective_region_universes["USA"] == ["TOP3000"]

    def test_round_trip_via_taskconfig(self, sample_payload):
        cfg = TaskConfig.model_validate({"brain_role_snapshot": sample_payload})
        assert cfg.brain_role_snapshot is not None
        assert cfg.brain_role_snapshot.brain_consultant_mode_at_start is False
        dumped = cfg.model_dump(exclude_none=True)
        assert dumped["brain_role_snapshot"]["effective_sharpe_submit_min"] == 1.25


# ---------------------------------------------------------------------------
# ContextualBanditState — Phase 1 R2/Q7 shape from evolution_strategy.to_dict
# ---------------------------------------------------------------------------

class TestContextualBanditState:
    # test_evolution_strategy_to_dict_round_trips removed in Phase 1c-delete
    # (ContextualDirectionBandit lived in the now-deleted evolution_strategy).
    # The ContextualBanditState SCHEMA survives (validates legacy task.config
    # JSONB), so the empty-state parse test is kept.

    def test_empty_state_parses(self):
        state = ContextualBanditState()
        assert state.v == 1
        assert state.arm_names == []
        assert state.segments == {}
        assert state.last_select is None


# ---------------------------------------------------------------------------
# Unknown top-level keys tolerated (extra='allow')
# ---------------------------------------------------------------------------

class TestExtraAllow:
    def test_unknown_key_does_not_raise(self):
        # Phase 2+ may add new task.config keys; existing TaskConfig must
        # tolerate them without raising at boundary read.
        raw = {
            "future_phase2_r5_key": [1, 2, 3],
            "future_phase3_dag_state": {"branches": []},
        }
        cfg = TaskConfig.model_validate(raw)
        # Unknown keys preserved via extra='allow'
        dump = cfg.model_dump(exclude_none=True)
        assert dump.get("future_phase2_r5_key") == [1, 2, 3]
        assert dump.get("future_phase3_dag_state") == {"branches": []}


# ---------------------------------------------------------------------------
# model_dump round-trip
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_full_round_trip(self):
        raw = {
            "brain_role_snapshot": {
                "brain_consultant_mode_at_start": True,
                "effective_default_test_period": "P0Y",
                "effective_sharpe_submit_min": 1.58,
                "effective_region_universes": {"USA": ["TOP3000"]},
            },
            "hypothesis_centric_variant": 1,
            "contextual_bandit_v1": {
                "v": 1,
                "arm_names": ["a"],
                "cold_threshold": 5,
                "global_arms": {"a": {"name": "a", "alpha": 2.0, "beta": 1.0}},
                "segments": {},
                "last_select": None,
            },
            "cascade_lock_token": "tok",
        }
        cfg = TaskConfig.model_validate(raw)
        dumped = cfg.model_dump(exclude_none=True)
        # All explicit keys preserved
        for k in raw:
            assert k in dumped


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------

class TestModuleExports:
    def test_top_level_imports(self):
        from backend.schemas import (
            BrainRoleSnapshot,
            ContextualBanditState,
            TaskConfig,
            WatchdogReviveInfo,
        )
        assert BrainRoleSnapshot is not None
        assert ContextualBanditState is not None
        assert TaskConfig is not None
        assert WatchdogReviveInfo is not None
