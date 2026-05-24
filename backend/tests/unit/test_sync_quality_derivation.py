"""P2.C sync hardening tests (2026-05-20).

Covers:
- _derive_quality_status_from_metrics band logic (main / provisional / FAIL / PENDING)
- _update_existing_alpha MERGES metrics (preserves AIAC `_`-keys) + derives
  quality_status only when PENDING [V1.1-M3 / V1.1-S2]
- sync_user_alphas skips when BRAIN_AUTH_CIRCUIT open [V1.2-R1]
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# _derive_quality_status_from_metrics
# ---------------------------------------------------------------------------

def test_derive_main_band_passes_provisional():
    from backend.tasks.sync_tasks import _derive_quality_status_from_metrics
    # main band (sharpe>=1.5, fitness>=1.2, 0.01<=turnover<=0.4)
    assert _derive_quality_status_from_metrics(1.6, 1.3, 0.2) == "PASS_PROVISIONAL"


def test_derive_provisional_band():
    from backend.tasks.sync_tasks import _derive_quality_status_from_metrics
    # below main but in provisional band (sharpe>=1.25, fitness>=1.0, turnover<=0.55)
    assert _derive_quality_status_from_metrics(1.3, 1.1, 0.5) == "PASS_PROVISIONAL"


def test_derive_fail():
    from backend.tasks.sync_tasks import _derive_quality_status_from_metrics
    assert _derive_quality_status_from_metrics(0.4, 0.0, 0.3) == "FAIL"


def test_derive_pending_on_missing_metric():
    from backend.tasks.sync_tasks import _derive_quality_status_from_metrics
    assert _derive_quality_status_from_metrics(None, 1.3, 0.2) == "PENDING"
    assert _derive_quality_status_from_metrics(1.6, None, 0.2) == "PENDING"
    assert _derive_quality_status_from_metrics(1.6, 1.3, None) == "PENDING"


# ---------------------------------------------------------------------------
# _update_existing_alpha — MERGE metrics + derive on PENDING only
# ---------------------------------------------------------------------------

def _mk_existing(quality_status="PENDING", metrics=None):
    return SimpleNamespace(
        quality_status=quality_status,
        metrics=metrics or {},
        status=None, stage=None, settings=None, tags=None, checks=None,
        is_metrics=None, os_metrics=None,
        is_sharpe=None, is_fitness=None, is_returns=None,
        is_turnover=None, is_drawdown=None, is_margin=None,
        is_long_count=None, is_short_count=None,
        date_modified=None, date_submitted=None, dataset_id=None,
        can_submit=None, metrics_snapshot_at=None,
    )


def test_update_existing_merges_metrics_preserving_aiac_keys():
    """[V1.1-M3] sync must MERGE metrics, preserving mining-stamped `_`-keys
    (e.g. _direction_bandit_recommended_arm) instead of replacing the dict."""
    from backend.tasks.sync_tasks import _update_existing_alpha

    existing = _mk_existing(
        quality_status="FAIL",
        metrics={"_direction_bandit_recommended_arm": "rag_template",
                 "_pre_brain_skip": True, "sharpe": 0.4},
    )
    a_data = {"status": "UNSUBMITTED", "is": {"checks": []}}
    is_metrics = {"sharpe": 0.42, "fitness": 0.05, "turnover": 0.3}

    with patch("backend.can_submit.compute_can_submit", return_value=(False, [], [])):
        _update_existing_alpha(existing, a_data, "IS", {"datasetId": "pv1"},
                               is_metrics, {}, None)

    # AIAC-stamped keys survive the sync merge
    assert existing.metrics["_direction_bandit_recommended_arm"] == "rag_template"
    assert existing.metrics["_pre_brain_skip"] is True
    # BRAIN-fresh metric overrides the stale value
    assert existing.metrics["sharpe"] == 0.42
    # _brain_* keys added
    assert existing.metrics["_brain_can_submit"] is False
    # quality_status NOT overwritten (was FAIL, not PENDING)
    assert existing.quality_status == "FAIL"


def test_update_existing_derives_quality_only_when_pending():
    """[V1.1-S2] PENDING rows get reclassified from metrics; non-PENDING
    verdicts (mining-direct authoritative) are preserved."""
    from backend.tasks.sync_tasks import _update_existing_alpha

    existing = _mk_existing(quality_status="PENDING", metrics={})
    a_data = {"status": "UNSUBMITTED", "is": {"checks": []}}
    is_metrics = {"sharpe": 0.4, "fitness": 0.0, "turnover": 0.3}  # FAIL band

    with patch("backend.can_submit.compute_can_submit", return_value=(False, [], [])):
        _update_existing_alpha(existing, a_data, "IS", {"datasetId": "pv1"},
                               is_metrics, {}, None)

    assert existing.quality_status == "FAIL"


def test_update_existing_preserves_dataset_id_when_brain_empty():
    """2026-05-24: FLAT (cross-dataset) alphas have an empty BRAIN
    settings.datasetId; sync must NOT wipe the AIAC field-derived dataset_id
    (else every 6h sync zeroes the dataset bandit's per-dataset attribution).
    Empty / None / missing datasetId → preserve the existing stamp."""
    from backend.tasks.sync_tasks import _update_existing_alpha

    a_data = {"status": "UNSUBMITTED", "is": {"checks": []}}
    is_metrics = {"sharpe": 1.5, "fitness": 1.3, "turnover": 0.2}

    for empty_settings in ({"datasetId": ""}, {"datasetId": None}, {}):
        existing = _mk_existing(quality_status="PASS_PROVISIONAL")
        existing.dataset_id = "pv1"  # AIAC-derived stamp
        with patch("backend.can_submit.compute_can_submit", return_value=(True, [], [])):
            _update_existing_alpha(existing, a_data, "IS", empty_settings,
                                   is_metrics, {}, None)
        assert existing.dataset_id == "pv1", f"dataset_id wiped by settings={empty_settings}"


def test_update_existing_overwrites_dataset_id_when_brain_present():
    """When BRAIN actually returns a datasetId, it wins (genuine reconciliation)."""
    from backend.tasks.sync_tasks import _update_existing_alpha

    existing = _mk_existing(quality_status="PASS_PROVISIONAL")
    existing.dataset_id = "pv1"
    a_data = {"status": "UNSUBMITTED", "is": {"checks": []}}
    is_metrics = {"sharpe": 1.5, "fitness": 1.3, "turnover": 0.2}
    with patch("backend.can_submit.compute_can_submit", return_value=(True, [], [])):
        _update_existing_alpha(existing, a_data, "IS", {"datasetId": "analyst4"},
                               is_metrics, {}, None)
    assert existing.dataset_id == "analyst4"


# ---------------------------------------------------------------------------
# sync_user_alphas — BRAIN_AUTH_CIRCUIT skip guard [V1.2-R1]
# ---------------------------------------------------------------------------

def test_sync_user_alphas_skips_when_circuit_open():
    """When BRAIN_AUTH_CIRCUIT is open, sync returns a skip dict without
    opening a DB session or hitting BRAIN."""
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    from backend.tasks.sync_tasks import sync_user_alphas

    with patch.object(BRAIN_AUTH_CIRCUIT, "is_open", return_value=True), \
         patch.object(BRAIN_AUTH_CIRCUIT, "status", return_value={"state": "open"}):
        result = sync_user_alphas()

    assert result.get("status") == "skipped_circuit_open"
