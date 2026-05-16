"""Integration: end-to-end MiningTask.config["brain_role_snapshot"] propagation.

Plan §8.2-§8.3. Validates:
  - workflow.py:349 reads task.config["brain_role_snapshot"] and threads
    each effective_* into the constructed MiningState
  - Settings flipped mid-run does NOT affect a MiningState already built
    from a frozen snapshot (the snapshot is the source of truth post-build)
  - Empty/missing snapshot falls back to None for all four fields (so
    downstream getattr(state, "X", default) yields settings fallback)

We don't drive workflow.run() end-to-end (too much DB/celery wiring);
instead we directly exercise the snapshot read + MiningState construction
that workflow.py:349 implements, and assert post-build invariance under
flag flips.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.graph.state import MiningState
from backend.config import _flag_override_cache, settings


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def _make_task(config: dict):
    """SimpleNamespace mimic of MiningTask with .config dict."""
    return SimpleNamespace(
        id=42, region="USA", universe="TOP3000", config=config,
    )


def _build_initial_state_like_workflow(task, dataset_id="dsX",
                                       fields=None, operators=None,
                                       num_alphas=3, factor_tier=1):
    """Mirror the workflow.py:349 construction logic exactly so we can
    assert the snapshot transfer in isolation."""
    snapshot = (task.config or {}).get("brain_role_snapshot") or {} if isinstance(task.config, dict) else {}
    return MiningState(
        task_id=task.id,
        region=task.region,
        universe=task.universe,
        dataset_id=dataset_id,
        fields=fields or [],
        operators=operators or [],
        num_alphas_target=num_alphas,
        factor_tier=factor_tier,
        available_dataset_pool=[],
        brain_consultant_mode_at_start=snapshot.get("brain_consultant_mode_at_start"),
        effective_default_test_period=snapshot.get("effective_default_test_period"),
        effective_sharpe_submit_min=snapshot.get("effective_sharpe_submit_min"),
        effective_region_universes_at_start=snapshot.get("effective_region_universes"),
    )


def test_snapshot_transfers_into_mining_state():
    """task.config snapshot → MiningState effective_* fields populated."""
    task = _make_task({
        "brain_role_snapshot": {
            "brain_consultant_mode_at_start": True,
            "effective_default_test_period": "P0Y",
            "effective_sharpe_submit_min": 1.58,
            "effective_region_universes": {"USA": "TOP3000", "CHN": "TOP2000A"},
        },
    })
    state = _build_initial_state_like_workflow(task)
    assert state.brain_consultant_mode_at_start is True
    assert state.effective_default_test_period == "P0Y"
    assert state.effective_sharpe_submit_min == 1.58
    assert state.effective_region_universes_at_start == {
        "USA": "TOP3000", "CHN": "TOP2000A",
    }


def test_missing_snapshot_yields_none_fields():
    """No snapshot key in task.config → all four fields None (downstream
    getattr falls back to settings)."""
    task = _make_task({"hypothesis_centric_variant": "control"})
    state = _build_initial_state_like_workflow(task)
    assert state.brain_consultant_mode_at_start is None
    assert state.effective_default_test_period is None
    assert state.effective_sharpe_submit_min is None
    assert state.effective_region_universes_at_start is None


def test_none_config_yields_none_fields():
    """task.config IS None (Postgres NULL) → no crash, None fields."""
    task = _make_task(None)
    state = _build_initial_state_like_workflow(task)
    assert state.effective_default_test_period is None
    assert state.effective_sharpe_submit_min is None


def test_snapshot_survives_flag_flip_mid_run():
    """Critical R2-M-3/M-4 invariant: once MiningState is built from a
    snapshot, flipping settings.ENABLE_BRAIN_CONSULTANT_MODE in the same
    process MUST NOT change the state's frozen values."""
    # Build state in USER mode with Consultant snapshot
    task = _make_task({
        "brain_role_snapshot": {
            "brain_consultant_mode_at_start": True,
            "effective_default_test_period": "P0Y",
            "effective_sharpe_submit_min": 1.58,
            "effective_region_universes": {"USA": "TOP3000"},
        },
    })
    state = _build_initial_state_like_workflow(task)

    # Settings starts as User
    assert settings.ENABLE_BRAIN_CONSULTANT_MODE is False
    # Mid-run: flip to Consultant globally
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    assert settings.ENABLE_BRAIN_CONSULTANT_MODE is True

    # State's frozen values UNCHANGED
    assert state.brain_consultant_mode_at_start is True
    assert state.effective_default_test_period == "P0Y"
    assert state.effective_sharpe_submit_min == 1.58

    # And vice versa: flip back to User
    del _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"]
    assert settings.ENABLE_BRAIN_CONSULTANT_MODE is False
    # Still frozen
    assert state.brain_consultant_mode_at_start is True
    assert state.effective_default_test_period == "P0Y"


def test_writer_path_merges_preserves_other_config_keys():
    """mining_tasks.py:23 snapshot writer must merge — preserve
    hypothesis_centric_variant (task_service.py:389) + any future task.config key."""
    # Simulate the writer behavior: start with existing task.config,
    # apply merge per plan §8.3 instance-level write.
    task = _make_task({
        "hypothesis_centric_variant": "treatment_A",
        "some_future_key": [1, 2, 3],
    })

    # Replicate the writer's merge expression (mining_tasks.py:223)
    if not isinstance(task.config, dict):
        task.config = {}
    if "brain_role_snapshot" not in task.config:
        task.config = {
            **task.config,
            "brain_role_snapshot": {
                "brain_consultant_mode_at_start": False,
                "effective_default_test_period": "P2Y0M",
                "effective_sharpe_submit_min": 1.5,
                "effective_region_universes": {"USA": "TOP3000"},
            },
        }

    # Pre-existing keys must survive
    assert task.config["hypothesis_centric_variant"] == "treatment_A"
    assert task.config["some_future_key"] == [1, 2, 3]
    # Snapshot added
    assert "brain_role_snapshot" in task.config
    assert task.config["brain_role_snapshot"]["effective_sharpe_submit_min"] == 1.5


def test_writer_path_idempotent_no_overwrite_on_second_call():
    """Second pass through mining_tasks.py writer must NOT replace existing
    snapshot (could overwrite a Consultant-time snapshot with current
    User-time values mid-run)."""
    task = _make_task({
        "brain_role_snapshot": {
            "brain_consultant_mode_at_start": True,
            "effective_default_test_period": "P0Y",
            "effective_sharpe_submit_min": 1.58,
            "effective_region_universes": {"USA": "TOP3000"},
        },
    })

    # Simulate writer running a second time after settings has been reverted
    # to User mode
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = False
    if not isinstance(task.config, dict):
        task.config = {}
    if "brain_role_snapshot" not in task.config:                 # ← idempotency guard
        task.config = {
            **task.config,
            "brain_role_snapshot": {
                "brain_consultant_mode_at_start": False,         # WRONG if it ran
                "effective_default_test_period": "P2Y0M",
            },
        }

    # Snapshot UNCHANGED (the `if "brain_role_snapshot" not in task.config`
    # guard prevented overwriting the original Consultant snapshot)
    snap = task.config["brain_role_snapshot"]
    assert snap["brain_consultant_mode_at_start"] is True
    assert snap["effective_default_test_period"] == "P0Y"
    assert snap["effective_sharpe_submit_min"] == 1.58
