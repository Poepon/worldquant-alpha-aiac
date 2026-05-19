"""G3 Phase A — wiring contract test (2026-05-19).

Verifies that the G3 hook in ``backend/agents/graph/nodes/evaluation.py``:

  1. Is gated by ``ENABLE_AST_ORIGINALITY_GATE`` (flag OFF → zero side-effect).
  2. Iterates only PENDING / PASS_* alphas (skips R10-dropped FAIL alphas).
  3. Honors the active mode (shadow / soft / hard) when stamping metrics.
  4. Survives a per-alpha exception without breaking the round
     (soft-fail invariant).

To stay independent of the heavy ``node_evaluate`` warm-up (which the
existing ``test_r1a_hook_evaluation`` test skips when Postgres is
unreachable), we exercise the G3 logic in isolation by extracting the
hook into a small reusable shim test that operates on a list of
``AlphaCandidate``-like objects. The actual production code at the
``# === G3 AST Originality Gate ===`` block in evaluation.py is a
near-identical inline copy — we verify the contract surface (mode
matrix + skip-FAIL + soft-fail) without spinning up the LangGraph
agent stack.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import List

import pytest

from backend.alpha_originality import (
    OriginalityChecker,
    OriginalityVerdict,
    apply_to_alpha,
)
from backend.config import settings


def _mk_alpha(expr: str, status: str = "PENDING"):
    return SimpleNamespace(expression=expr, metrics={}, quality_status=status)


def _run_g3_inline(
    alphas: List, *, history: List[str], threshold: float, mode: str,
) -> dict:
    """Mirror the inline G3 block in evaluation.py (post-R10 wiring).

    Returns counters identical to the production block so the contract
    can be asserted from a clean fixture without spinning up node_evaluate.
    """
    if not getattr(settings, "ENABLE_AST_ORIGINALITY_GATE", False) or not alphas:
        return {"blocked": 0, "skipped": 0, "errs": 0, "checker_ran": False}

    checker = OriginalityChecker(threshold=threshold, mode=mode)
    checker.seed_history(history)

    blocked = skipped = errs = 0
    for a in alphas:
        status = getattr(a, "quality_status", None)
        status_str = getattr(status, "value", status) if status is not None else None
        if status_str in {"FAIL", "REJECT"}:
            continue
        try:
            verdict = checker.check(getattr(a, "expression", "") or "")
            apply_to_alpha(a, verdict)
            if verdict.verdict == "blocked":
                blocked += 1
            elif verdict.verdict == "skipped":
                skipped += 1
        except Exception:
            errs += 1
    return {"blocked": blocked, "skipped": skipped, "errs": errs, "checker_ran": True}


# ---------------------------------------------------------------------------
# Contract: flag OFF — zero side-effect on metrics
# ---------------------------------------------------------------------------

def test_g3_flag_off_no_side_effect(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_AST_ORIGINALITY_GATE", False)
    alphas = [_mk_alpha("rank(close)"), _mk_alpha("ts_rank(close, 20)")]
    out = _run_g3_inline(
        alphas, history=["rank(close)"], threshold=0.15, mode="hard",
    )
    assert out == {"blocked": 0, "skipped": 0, "errs": 0, "checker_ran": False}
    # No metrics keys written
    for a in alphas:
        assert all(not k.startswith("_g3_") for k in a.metrics)
    # No quality_status flipped
    for a in alphas:
        assert a.quality_status == "PENDING"


# ---------------------------------------------------------------------------
# Contract: FAIL / REJECT alphas SKIPPED (R10-dropped not re-stamped)
# ---------------------------------------------------------------------------

def test_g3_skips_r10_dropped_alphas(monkeypatch):
    """R10-dropped alphas already terminal-failed; G3 must not touch them."""
    monkeypatch.setattr(settings, "ENABLE_AST_ORIGINALITY_GATE", True)
    alphas = [
        _mk_alpha("rank(close)", status="PENDING"),
        _mk_alpha("rank(close)", status="FAIL"),    # R10 drop
        _mk_alpha("rank(close)", status="REJECT"),  # validation drop
    ]
    out = _run_g3_inline(
        alphas, history=["rank(close)"], threshold=0.99, mode="hard",
    )
    # 1 PENDING alpha is identical to history → blocked
    assert out["blocked"] == 1
    # FAIL / REJECT alphas not touched — no _g3_ metrics
    assert not any(k.startswith("_g3_") for k in alphas[1].metrics)
    assert not any(k.startswith("_g3_") for k in alphas[2].metrics)
    # PENDING alpha got blocked → status flipped to FAIL (hard mode)
    assert alphas[0].quality_status == "FAIL"
    # FAIL / REJECT statuses preserved
    assert alphas[1].quality_status == "FAIL"
    assert alphas[2].quality_status == "REJECT"


# ---------------------------------------------------------------------------
# Mode matrix: shadow vs soft vs hard side-effect divergence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,expected_status,expected_blocked_tag", [
    ("shadow", "PENDING", True),                 # tag stamped, status untouched
    ("soft", "PASS_PROVISIONAL", True),          # status downgrade, still simulate
    ("hard", "FAIL", True),                      # status reject
])
def test_g3_mode_matrix(monkeypatch, mode, expected_status, expected_blocked_tag):
    monkeypatch.setattr(settings, "ENABLE_AST_ORIGINALITY_GATE", True)
    alpha = _mk_alpha("rank(close)", status="PENDING")
    out = _run_g3_inline(
        [alpha], history=["rank(close)"], threshold=0.99, mode=mode,
    )
    assert out["blocked"] == 1
    assert alpha.quality_status == expected_status
    if expected_blocked_tag:
        assert alpha.metrics.get("_g3_ast_originality_blocked") is True
    assert alpha.metrics.get("_g3_mode") == mode


# ---------------------------------------------------------------------------
# Contract: per-alpha exception isolated (soft-fail invariant)
# ---------------------------------------------------------------------------

def test_g3_per_alpha_exception_does_not_break_batch(monkeypatch):
    """Forcing apply_to_alpha to raise for one alpha must not break the rest."""
    monkeypatch.setattr(settings, "ENABLE_AST_ORIGINALITY_GATE", True)
    alphas = [
        _mk_alpha("rank(close)"),
        _mk_alpha("ts_rank(close, 20)"),
        _mk_alpha("vec_sum(group_neutralize(returns, industry))"),
    ]
    # Patch apply_to_alpha to raise for the second alpha only
    real_apply = apply_to_alpha
    call_count = {"n": 0}

    def _flaky(a, v):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated apply failure")
        return real_apply(a, v)

    import backend.alpha_originality as mod
    monkeypatch.setattr(mod, "apply_to_alpha", _flaky, raising=True)

    # Re-import the local helper so it sees the patched module symbol
    def _run_with_patched_apply():
        if not getattr(settings, "ENABLE_AST_ORIGINALITY_GATE", False):
            return {"blocked": 0, "skipped": 0, "errs": 0}
        checker = OriginalityChecker(threshold=0.15, mode="shadow")
        checker.seed_history(["rank(close)"])
        b = s = e = 0
        for a in alphas:
            try:
                v = checker.check(a.expression)
                mod.apply_to_alpha(a, v)
                if v.verdict == "blocked":
                    b += 1
                elif v.verdict == "skipped":
                    s += 1
            except Exception:
                e += 1
        return {"blocked": b, "skipped": s, "errs": e}

    out = _run_with_patched_apply()
    assert out["errs"] == 1, "one apply must have raised"
    # Other two were processed — at least the first one must have a _g3_ tag
    assert any(k.startswith("_g3_") for k in alphas[0].metrics)
    # Second alpha untouched because apply raised before stamping
    assert all(not k.startswith("_g3_") for k in alphas[1].metrics)
    # Third alpha processed normally
    assert any(k.startswith("_g3_") for k in alphas[2].metrics)


# ---------------------------------------------------------------------------
# Contract: empty alpha list short-circuits
# ---------------------------------------------------------------------------

def test_g3_empty_batch_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_AST_ORIGINALITY_GATE", True)
    out = _run_g3_inline(
        [], history=["rank(close)"], threshold=0.15, mode="hard",
    )
    assert out["checker_ran"] is False  # empty alpha list → no checker call
