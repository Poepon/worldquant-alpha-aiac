"""Phase 3 Q10 PR2d: q10_layer_telemetry_report tests (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §9 + §15.2.

Exercises aggregation, threshold alerts, and report formatting on synthetic
rows. DB query path is left for integration when production shadow data
accumulates.
"""
from __future__ import annotations

import json

import pytest

from scripts.q10_layer_telemetry_report import (
    Q10Row,
    Q10Summary,
    aggregate,
    format_report,
    load_rows_from_json,
    main,
)


# ---------------------------------------------------------------------------
# aggregate()
# ---------------------------------------------------------------------------

def _row(verdict="skip", mode="shadow", engine="pandas_snapshot", ms=50,
         skip_reason=None, brain_disagreement=None):
    return Q10Row(
        verdict=verdict, mode_at_call=mode, engine_kind=engine,
        elapsed_ms=ms, skip_reason=skip_reason,
        brain_disagreement=brain_disagreement,
    )


def test_aggregate_empty_returns_zero_metrics():
    s = aggregate([], window_hours=24)
    assert s.total_rows == 0
    assert s.cost_saved_pct == 0.0
    assert s.fn_rate is None
    assert s.alert_level == "INFO"  # no signal → INFO


def test_aggregate_verdict_and_mode_breakdown():
    rows = [
        _row(verdict="pass", mode="shadow"),
        _row(verdict="reject", mode="hard", skip_reason=None),
        _row(verdict="skip", mode="shadow", skip_reason="untranslatable"),
    ]
    s = aggregate(rows, window_hours=24)
    assert s.total_rows == 3
    assert s.verdict_counts == {"pass": 1, "reject": 1, "skip": 1}
    assert s.mode_counts == {"shadow": 2, "hard": 1}


def test_aggregate_cost_saved_only_counts_hard_rejects():
    """Soft and shadow rejects still went to BRAIN → don't count as saved."""
    rows = [
        _row(verdict="reject", mode="shadow"),
        _row(verdict="reject", mode="soft"),
        _row(verdict="reject", mode="hard"),
        _row(verdict="reject", mode="hard"),
        _row(verdict="pass", mode="hard"),
    ]
    s = aggregate(rows, window_hours=24)
    # 2 hard rejects out of 5 = 40%
    assert s.cost_saved_pct == 40.0


def test_aggregate_translation_success_rate():
    rows = [
        _row(verdict="pass"),
        _row(verdict="skip", skip_reason="untranslatable"),
        _row(verdict="skip", skip_reason="engine_disabled"),
        _row(verdict="skip", skip_reason="untranslatable"),
    ]
    s = aggregate(rows, window_hours=24)
    # 2 untranslatable / 4 → 50% untranslatable → 50% success
    assert s.translation_success_pct == 50.0


def test_aggregate_fn_rate_requires_min_followups():
    """FN rate needs ≥10 followup-disagreement rows to be reported."""
    # 5 disagreement rows is below threshold
    rows = [_row(brain_disagreement="true") for _ in range(5)]
    s = aggregate(rows, window_hours=24)
    assert s.fn_rate is None
    # 10+ rows trigger reporting
    rows = [_row(brain_disagreement="true") for _ in range(8)] + \
           [_row(brain_disagreement="false") for _ in range(5)]
    s = aggregate(rows, window_hours=24)
    assert s.fn_rate is not None
    # 8 / 13 = ~0.615
    assert abs(s.fn_rate - 8 / 13) < 1e-6


def test_aggregate_latency_percentiles():
    rows = [_row(ms=10), _row(ms=20), _row(ms=50), _row(ms=100), _row(ms=1000)]
    s = aggregate(rows, window_hours=24)
    assert s.median_elapsed_ms == 50
    # p99 of 5 values → index 4 → 1000
    assert s.p99_elapsed_ms == 1000


# ---------------------------------------------------------------------------
# alert_level
# ---------------------------------------------------------------------------

def test_alert_level_alert_when_fn_rate_high():
    rows = [_row(brain_disagreement="true") for _ in range(15)] + \
           [_row(brain_disagreement="false") for _ in range(5)]
    s = aggregate(rows, window_hours=24)
    assert s.alert_level == "ALERT"  # fn_rate 15/20 = 0.75 > 0.15


def test_alert_level_info_when_cost_saved_low_and_no_fn_signal():
    rows = (
        [_row(verdict="reject", mode="hard")] +          # 1 hard reject
        [_row(verdict="pass") for _ in range(20)]        # 20 passes
    )
    s = aggregate(rows, window_hours=24)
    # cost_saved = 1/21 ≈ 4.76% < 10% → INFO
    assert s.alert_level == "INFO"


def test_alert_level_ok_for_healthy_run():
    rows = (
        [_row(verdict="reject", mode="hard") for _ in range(5)] +
        [_row(verdict="pass") for _ in range(10)]
    )
    s = aggregate(rows, window_hours=24)
    # cost_saved 5/15 = 33% > 10%, no fn_rate signal → OK
    assert s.alert_level == "OK"


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------

def test_format_report_empty_window():
    s = aggregate([], window_hours=24)
    out = format_report(s)
    assert "No qlib_prescreen_log rows" in out
    assert "INFO" in out


def test_format_report_alert_includes_action_advice():
    rows = [_row(brain_disagreement="true") for _ in range(15)] + \
           [_row(brain_disagreement="false") for _ in range(5)]
    s = aggregate(rows, window_hours=24)
    out = format_report(s)
    assert "ALERT" in out
    assert "demote" in out.lower()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_with_rows_json_prints_report(tmp_path, capsys):
    payload = [
        {"verdict": "pass", "mode_at_call": "shadow",
         "engine_kind": "pandas_snapshot", "elapsed_ms": 50},
        {"verdict": "reject", "mode_at_call": "hard",
         "engine_kind": "pandas_snapshot", "elapsed_ms": 45},
    ]
    p = tmp_path / "rows.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    rc = main(["--rows-json", str(p), "--window-hours", "24"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Q10 telemetry" in captured.out
    assert "cost saved" in captured.out


def test_cli_exit_nonzero_on_alert(tmp_path):
    payload = [{"verdict": "pass", "mode_at_call": "shadow",
                "engine_kind": "pandas_snapshot", "elapsed_ms": 50,
                "brain_disagreement": "true"} for _ in range(15)]
    p = tmp_path / "alert.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    rc = main([
        "--rows-json", str(p), "--exit-nonzero-on-alert",
    ])
    assert rc == 2
