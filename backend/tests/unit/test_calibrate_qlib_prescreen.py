"""Phase 3 Q10 PR2c: calibrate_qlib_prescreen tests (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §4.5.

Exercises the sweep + recommendation logic on synthetic pairs (no DB
required). The DB JOIN path is tested via integration in a later PR
once production shadow data accumulates.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.calibrate_qlib_prescreen import (
    CalibrationRow,
    FloorEval,
    evaluate_floor,
    format_report,
    load_pairs_from_json,
    main,
    recommend_floor,
    sweep_floors,
)


def test_evaluate_floor_classifies_2x2_correctly():
    rows = [
        CalibrationRow(local_sharpe=0.5, brain_passed=True),   # TP (kept good)
        CalibrationRow(local_sharpe=0.1, brain_passed=True),   # FN (Q10 wrong)
        CalibrationRow(local_sharpe=0.5, brain_passed=False),  # FP (Q10 missed)
        CalibrationRow(local_sharpe=0.1, brain_passed=False),  # TN (saved cost)
    ]
    e = evaluate_floor(rows, floor=0.3)
    assert (e.tp, e.fn, e.fp, e.tn) == (1, 1, 1, 1)
    assert e.cost_saved_pct == 25.0  # 1/4 saved
    assert e.fn_rate == 0.5          # 1/(1+1)


def test_evaluate_floor_handles_empty():
    e = evaluate_floor([], floor=0.3)
    assert (e.tp, e.fn, e.fp, e.tn) == (0, 0, 0, 0)
    assert e.cost_saved_pct == 0.0
    assert e.fn_rate == 0.0


def test_sweep_floors_step_count():
    sweep = sweep_floors([], floor_min=0.1, floor_max=0.5, floor_step=0.05)
    # 0.10, 0.15, 0.20, ..., 0.50 → 9 entries
    assert len(sweep) == 9
    assert sweep[0].floor == 0.1
    assert sweep[-1].floor == 0.5


def test_recommend_floor_picks_max_cost_saved_within_gate():
    """Two candidates pass the gate; recommend the one with higher cost_saved."""
    rows = [
        CalibrationRow(local_sharpe=0.2, brain_passed=False),  # always TN
        CalibrationRow(local_sharpe=0.2, brain_passed=False),  # always TN
        CalibrationRow(local_sharpe=0.4, brain_passed=True),   # FN at floor 0.5, TP at 0.3
        CalibrationRow(local_sharpe=0.4, brain_passed=True),   # FN at floor 0.5, TP at 0.3
    ]
    sweep = sweep_floors(rows, floor_min=0.1, floor_max=0.5, floor_step=0.1)
    rec = recommend_floor(sweep, max_fn_rate=0.15)
    # At floor 0.3, both TN-rows still TN + both TP-rows still TP (sharpe=0.4>=0.3)
    # → cost_saved 50% / fn_rate 0
    # At floor 0.5, TP-rows flip to FN → fn_rate 1.0 → reject
    assert rec is not None
    assert rec.floor <= 0.4
    assert rec.fn_rate <= 0.15


def test_recommend_floor_returns_none_when_no_candidate_meets_gate():
    """All sweeps have fn_rate > 0.15 → no recommendation."""
    rows = [
        CalibrationRow(local_sharpe=0.05, brain_passed=True)   # always FN
        for _ in range(10)
    ]
    sweep = sweep_floors(rows, floor_min=0.1, floor_max=0.5, floor_step=0.1)
    rec = recommend_floor(sweep, max_fn_rate=0.15)
    assert rec is None


def test_format_report_marks_recommendation(capsys):
    sweep = [FloorEval(floor=0.3, tp=10, fn=1, fp=2, tn=5)]
    rec = sweep[0]
    out = format_report(sweep, rec)
    assert "RECOMMEND" in out
    assert "0.300" in out


def test_format_report_no_recommendation_message():
    sweep = [FloorEval(floor=0.3, tp=10, fn=5, fp=2, tn=5)]  # fn_rate 5/15 = 0.33
    out = format_report(sweep, None)
    assert "No floor satisfies" in out


def test_load_pairs_from_json_round_trip(tmp_path):
    payload = [
        {"local_sharpe": 0.5, "brain_passed": True},
        {"local_sharpe": 0.1, "brain_passed": False},
        {"local_sharpe": "bad", "brain_passed": True},  # invalid — skipped
    ]
    p = tmp_path / "pairs.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    rows = load_pairs_from_json(str(p))
    assert len(rows) == 2  # third row dropped on float coerce fail
    assert rows[0].local_sharpe == 0.5


def test_cli_main_insufficient_data_exits_zero(tmp_path):
    """< min_samples → exit 0 with insufficient data message (graceful)."""
    payload = [{"local_sharpe": 0.5, "brain_passed": True}]
    p = tmp_path / "tiny.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    rc = main(["--pairs-json", str(p), "--min-samples", "50"])
    assert rc == 0


def test_cli_main_with_pairs_prints_report(tmp_path, capsys):
    """Reasonable dataset → CLI prints Pareto frontier table."""
    payload = [{"local_sharpe": s, "brain_passed": s >= 0.4}
               for s in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9] * 10]
    p = tmp_path / "data.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    rc = main(["--pairs-json", str(p), "--min-samples", "50"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Pareto" in captured.out
    assert "floor" in captured.out
