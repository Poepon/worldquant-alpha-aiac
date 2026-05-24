"""sync verdict-derivation tests (P2.C 2026-05-20 + Feature 1 2026-05-24).

Covers:
- _derive_verdict_from_brain: the synced-alpha verdict now runs through the SAME
  compute_verdict_from_signals as mining (score=0/should_opt=False → never OPTIMIZE),
  + the sync-side S1 guardrail (full PASS demoted when compute_can_submit=False)
  + the raw-None → PENDING guard + M5 os_sharpe / V-12 interaction.
- _update_existing_alpha MERGES metrics (preserves AIAC `_`-keys) + derives
  quality_status only when PENDING [V1.1-M3 / V1.1-S2].
- _update_existing_alpha preserves AIAC dataset_id when BRAIN's is empty.
- sync_user_alphas skips when BRAIN_AUTH_CIRCUIT open [V1.2-R1].
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# _derive_verdict_from_brain (Feature 1)
# ---------------------------------------------------------------------------

def _checks_all_pass(sharpe, fitness, turnover, self_corr_result="PENDING",
                     self_corr_value=None, extra=None):
    checks = [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": sharpe},
        {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": fitness},
        {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": turnover},
        {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": turnover},
    ]
    sc = {"name": "SELF_CORRELATION", "result": self_corr_result}
    if self_corr_value is not None:
        sc["value"] = self_corr_value
    checks.append(sc)
    if extra:
        checks.extend(extra)
    return checks


def _derive(is_metrics, *, os_metrics=None, can_sub=None, expression="ts_rank(close,20)"):
    from backend.tasks.sync_tasks import _derive_verdict_from_brain
    a_data = {"is": {"checks": is_metrics.get("checks", [])}}
    return _derive_verdict_from_brain(a_data, is_metrics, os_metrics or {}, expression, can_sub)


def test_derive_pending_on_missing_metric():
    """raw-None guard FIRST: a missing core metric → None (caller keeps PENDING).
    _safe_metric would coerce None→0.0 and mis-route to FAIL, so this guard is
    load-bearing — it's why the 338 degenerate synced rows stay PENDING."""
    assert _derive({"fitness": 1.3, "turnover": 0.2, "checks": []}) is None  # sharpe None
    assert _derive({"sharpe": 1.6, "turnover": 0.2, "checks": []}) is None   # fitness None
    assert _derive({"sharpe": 1.6, "fitness": 1.3, "checks": []}) is None    # turnover None


def test_derive_fail_band():
    """Weak metrics, no BRAIN checks, not submittable, score=0/should_opt=False → FAIL."""
    vr = _derive({"sharpe": 0.4, "fitness": 0.0, "turnover": 0.3, "checks": []},
                 can_sub=False)
    assert vr is not None
    assert vr.decision.status == "FAIL"


def test_derive_near_pass_provisional():
    """Provisional-band metrics + SELF_CORRELATION PENDING (synced reality →
    UNKNOWN, unverified) → hard_gate blocked, near_pass → PASS_PROVISIONAL."""
    checks = _checks_all_pass(1.3, 1.1, 0.2)  # SELF_CORRELATION PENDING
    vr = _derive({"sharpe": 1.3, "fitness": 1.1, "turnover": 0.2, "checks": checks},
                 can_sub=True)
    assert vr.decision.status == "PASS_PROVISIONAL"
    assert vr.decision.reason == "near_pass"


def test_derive_s1_guardrail_demotes_pass_when_unsubmittable():
    """When the verdict would be full PASS (verified self_corr + hard band) but
    compute_can_submit says unsubmittable (can_sub=False, e.g. an ERROR check),
    the S1 guardrail demotes PASS → PASS_PROVISIONAL/brain_unsubmittable. With
    can_sub=True the same alpha stays PASS."""
    checks = _checks_all_pass(1.8, 1.5, 0.2, self_corr_result="PASS", self_corr_value=0.1)
    im = {"sharpe": 1.8, "fitness": 1.5, "turnover": 0.2, "checks": checks}

    vr_block = _derive(im, can_sub=False)
    assert vr_block.decision.status == "PASS_PROVISIONAL"
    assert vr_block.decision.reason == "brain_unsubmittable"

    vr_ok = _derive(im, can_sub=True)
    assert vr_ok.decision.status == "PASS"
    assert vr_ok.decision.reason == "hard_gate_pass"

    # can_sub=None ("no BRAIN signal") must NOT demote (`is False`, not `not`)
    vr_none = _derive(im, can_sub=None)
    assert vr_none.decision.status == "PASS"


def test_derive_high_sharpe_needs_os_evidence_else_provisional():
    """M5: a high-sharpe (≥2) synced alpha without OS sharpe is V-12-blocked
    (hard_gate fails on is_overfit_safe) → only PASS_PROVISIONAL even with a
    verified self_corr. Injecting os_metrics['sharpe'] unblocks the hard gate."""
    checks = _checks_all_pass(2.5, 1.8, 0.2, self_corr_result="PASS", self_corr_value=0.1)
    im = {"sharpe": 2.5, "fitness": 1.8, "turnover": 0.2, "checks": checks}

    vr_no_os = _derive(im, os_metrics={}, can_sub=True)
    assert vr_no_os.decision.status == "PASS_PROVISIONAL"  # V-12 blocked → near_pass

    vr_os = _derive(im, os_metrics={"sharpe": 1.5}, can_sub=True)  # os/is = 0.6 ≥ 0.4
    assert vr_os.decision.status == "PASS"


# ---------------------------------------------------------------------------
# _update_existing_alpha — MERGE metrics + derive on PENDING only
# ---------------------------------------------------------------------------

def _mk_existing(quality_status="PENDING", metrics=None):
    return SimpleNamespace(
        quality_status=quality_status,
        metrics=metrics or {},
        expression="ts_rank(close, 20)",
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
    (e.g. _direction_bandit_recommended_arm) instead of replacing the dict.
    quality_status=FAIL (not PENDING) → verdict derivation is skipped."""
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

    assert existing.metrics["_direction_bandit_recommended_arm"] == "rag_template"
    assert existing.metrics["_pre_brain_skip"] is True
    assert existing.metrics["sharpe"] == 0.42
    assert existing.metrics["_brain_can_submit"] is False
    assert existing.quality_status == "FAIL"  # NOT overwritten (was not PENDING)


def test_update_existing_derives_quality_only_when_pending():
    """[V1.1-S2 + Feature 1] PENDING rows get reclassified via the shared verdict;
    a weak alpha with no BRAIN checks → FAIL (routed, not crashed)."""
    from backend.tasks.sync_tasks import _update_existing_alpha

    existing = _mk_existing(quality_status="PENDING", metrics={})
    a_data = {"status": "UNSUBMITTED", "is": {"checks": []}}
    is_metrics = {"sharpe": 0.4, "fitness": 0.0, "turnover": 0.3}  # FAIL band

    with patch("backend.can_submit.compute_can_submit", return_value=(False, [], [])):
        _update_existing_alpha(existing, a_data, "IS", {"datasetId": "pv1"},
                               is_metrics, {}, None)

    assert existing.quality_status == "FAIL"
    # T3: FAIL does NOT stamp _routing_reason
    assert "_routing_reason" not in existing.metrics


def test_update_existing_pending_stamps_routing_reason_on_provisional():
    """[Feature 1 / M6+T3] a PENDING row that derives to PASS_PROVISIONAL gets
    _routing_reason stamped (aligns synced rows with mining's annotation)."""
    from backend.tasks.sync_tasks import _update_existing_alpha

    existing = _mk_existing(quality_status="PENDING", metrics={})
    checks = _checks_all_pass(1.3, 1.1, 0.2)  # near_pass band, SELF_CORR PENDING
    a_data = {"status": "UNSUBMITTED", "is": {"checks": checks}}
    is_metrics = {"sharpe": 1.3, "fitness": 1.1, "turnover": 0.2, "checks": checks}

    with patch("backend.can_submit.compute_can_submit", return_value=(True, [], [])):
        _update_existing_alpha(existing, a_data, "IS", {"datasetId": "pv1"},
                               is_metrics, {}, None)

    assert existing.quality_status == "PASS_PROVISIONAL"
    assert existing.metrics.get("_routing_reason") == "near_pass"


def test_update_existing_preserves_dataset_id_when_brain_empty():
    """2026-05-24: FLAT (cross-dataset) alphas have an empty BRAIN
    settings.datasetId; sync must NOT wipe the AIAC field-derived dataset_id."""
    from backend.tasks.sync_tasks import _update_existing_alpha

    a_data = {"status": "UNSUBMITTED", "is": {"checks": []}}
    is_metrics = {"sharpe": 1.5, "fitness": 1.3, "turnover": 0.2}

    for empty_settings in ({"datasetId": ""}, {"datasetId": None}, {}):
        existing = _mk_existing(quality_status="PASS_PROVISIONAL")
        existing.dataset_id = "pv1"
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
