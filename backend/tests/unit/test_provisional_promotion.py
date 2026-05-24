"""Unit tests for _should_promote_provisional (refresh_tasks.py).

2026-05-24: the PASS_PROVISIONAL → PASS promotion gate. refresh_kb only ever
DEMOTED, so an alpha held provisional purely because BRAIN's is.checks were
empty at sim-time (routing reason `brain_checks_unverified`) was frozen forever
even after BRAIN confirmed it submission-grade. This predicate is the promotion
gate, exercised here as a pure function (the inline caller in
_refresh_can_submit_async runs it after the 30s post-sim can_submit refresh).
"""
from backend.tasks.refresh_tasks import _should_promote_provisional

# Representative flat eval band (mirrors _eval_thresholds output).
THRESH = {
    "sharpe_min": 1.5,
    "fitness_min": 1.2,
    "turnover_min": 0.01,
    "turnover_max": 0.4,
}


def _call(**over):
    kw = dict(
        quality_status="PASS_PROVISIONAL",
        can_submit=True,
        routing_reason="brain_checks_unverified",
        is_sharpe=1.52,
        is_fitness=1.27,
        is_turnover=0.123,
        thresholds=THRESH,
    )
    kw.update(over)
    return _should_promote_provisional(**kw)


def test_promotes_when_all_conditions_met():
    # The alpha 14315 case: provisional, can_submit, brain_checks_unverified,
    # full hard band → promote.
    assert _call() is True


def test_no_promote_when_can_submit_not_true():
    assert _call(can_submit=False) is False
    assert _call(can_submit=None) is False


def test_no_promote_when_reason_is_near_pass():
    # near_pass holds are genuinely only in the provisional band — must NOT
    # auto-promote. Only the empty-checks timing artifact is eligible.
    assert _call(routing_reason="near_pass") is False
    assert _call(routing_reason="v16_hard_flags") is False
    assert _call(routing_reason=None) is False


def test_no_promote_when_not_provisional():
    assert _call(quality_status="PASS") is False
    assert _call(quality_status="FAIL") is False
    assert _call(quality_status="OPTIMIZE") is False


def test_no_promote_when_below_full_band():
    # Provisional-band metrics (sharpe 1.3 ≥ 1.25 provisional but < 1.5 full)
    # must stay provisional.
    assert _call(is_sharpe=1.3) is False
    assert _call(is_fitness=1.1) is False
    assert _call(is_turnover=0.5) is False   # over turnover_max
    assert _call(is_turnover=0.005) is False  # under turnover_min


def test_none_metrics_treated_as_zero_no_promote():
    assert _call(is_sharpe=None) is False
    assert _call(is_fitness=None) is False
    assert _call(is_turnover=None) is False


def test_or_intent_can_submit_alone_suffices_for_selfcorr():
    # The local-OR-BRAIN self_corr intent: the predicate requires can_submit=True
    # (BRAIN doesn't reject) but does NOT require a verified BRAIN SELF_CORRELATION
    # — a still-PENDING BRAIN self-corr is fine because the local self_corr already
    # satisfied the gate at eval time. So can_submit=True + full band promotes,
    # regardless of BRAIN self-corr being PENDING.
    assert _call() is True
