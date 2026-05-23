"""Integration: the soft-regularizer orchestration in node_simulate.

Drives the REAL `_apply_soft_regularizer` helper (extracted from node_simulate)
with a mocked OriginalityChecker + run_r5_judge. The pure-math is unit-tested
in test_soft_regularizer.py; this locks the parts that only exist in the node
glue: the local↔global index keying (cand_exprs[_li] ↔ pending_alphas[
indices_to_simulate[_li]]), shadow=no-op, soft keep/skip re-derive, top-K judge
selection, and R5 soft-fail. Uses a non-trivial indices_to_simulate=[5,2,8] so
an off-by-one in the mapping would stamp the wrong candidate and fail.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _alpha(expr, hyp, expl="rationale"):
    return SimpleNamespace(expression=expr, hypothesis=hyp, explanation=expl, metrics={})


class _FakeChecker:
    """OriginalityChecker stand-in: load_history no-op; check().min_distance
    looked up per-expression from the class-level map (default 1.0 = original)."""
    dist_map: dict = {}

    async def load_history(self, **_):
        return None

    def check(self, expression):
        return SimpleNamespace(min_distance=self.dist_map.get(expression, 1.0))


def _state(pending):
    return SimpleNamespace(task_id=42, region="USA", universe="TOP3000", pending_alphas=pending)


@contextmanager
def _patched(settings_overrides: dict, dist_map: dict, r5_mock=None):
    """Patch settings + OriginalityChecker + run_r5_judge/get_llm_service for
    one helper call."""
    _FakeChecker.dist_map = dist_map
    patches = [patch(f"backend.config.settings.{k}", v) for k, v in settings_overrides.items()]
    patches.append(patch("backend.alpha_originality.OriginalityChecker", _FakeChecker))
    patches.append(patch(
        "backend.agents.services.llm_service.get_llm_service",
        return_value=SimpleNamespace(),
    ))
    if r5_mock is not None:
        patches.append(patch("backend.agents.graph.r5_judge.run_r5_judge", r5_mock))
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


def _call(state, indices, cand_exprs, probas, threshold=0.10):
    from backend.agents.graph.nodes.evaluation import _apply_soft_regularizer
    keep = [i for i, p in enumerate(probas) if p >= threshold]
    skip = [i for i, p in enumerate(probas) if p < threshold]
    return asyncio.run(_apply_soft_regularizer(
        state, indices, cand_exprs, list(probas), keep, skip, threshold,
    ))


def test_shadow_is_no_op_but_stamps_all():
    pending = [_alpha("rank(close)", "h0"), _alpha("rank(volume)", "h1"), _alpha("rank(returns)", "h2")]
    state = _state(pending)
    probas = [0.9, 0.5, 0.2]
    with _patched({"CODE_GEN_SOFT_REG_MODE": "shadow", "CODE_GEN_SOFT_REG_W_ALIGNMENT": 0.0},
                  {"rank(close)": 0.9, "rank(volume)": 0.1, "rank(returns)": 1.0}):
        keep, skip, out_probas = _call(state, [0, 1, 2], [a.expression for a in pending], probas)
    # shadow never changes keep/skip/probas
    assert keep == [0, 1, 2]
    assert skip == []
    assert out_probas == probas
    # but every candidate is stamped, and none is judged (W_ALIGNMENT=0)
    for a in pending:
        assert "_soft_reg_penalty" in a.metrics
        assert a.metrics["_soft_reg_alignment_judged"] is False
        assert "_soft_reg_r5_composite" not in a.metrics


def test_soft_p1_downweights_and_moves_to_skip():
    # A near-threshold candidate with a duplicate signal (min_distance=0) should
    # be down-weighted below threshold and move keep→skip (P1, no alignment).
    pending = [_alpha("rank(close)", "h0"), _alpha("rank(volume)", "h1")]
    state = _state(pending)
    probas = [0.9, 0.12]  # both initially >= 0.10
    with _patched({"CODE_GEN_SOFT_REG_MODE": "soft", "CODE_GEN_SOFT_REG_W_ALIGNMENT": 0.0,
                   "CODE_GEN_SOFT_REG_LAMBDA": 0.5},
                  {"rank(close)": 1.0, "rank(volume)": 0.0}):  # cand1 = duplicate
        keep, skip, out_probas = _call(state, [0, 1], [a.expression for a in pending], probas)
    # cand0 (original, high p) kept; cand1 (duplicate, near-threshold) down-weighted out
    assert keep == [0]
    assert skip == [1]
    assert out_probas[1] < 0.10  # 0.12 * (1 - 0.5*0.5) = 0.09
    assert out_probas[0] == pytest.approx(0.9)  # original → 0 penalty → unchanged


def test_topk_judges_highest_effp_and_index_keying():
    # Non-trivial global indices: candidates live at pending[5],[2],[8].
    pending = [_alpha("dummy", "hd") for _ in range(9)]
    pending[5] = _alpha("rank(close)", "HYP_A")    # local 0 — highest eff-P
    pending[2] = _alpha("rank(volume)", "HYP_B")   # local 1
    pending[8] = _alpha("rank(returns)", "HYP_C")  # local 2 — lowest
    state = _state(pending)
    indices = [5, 2, 8]
    cand_exprs = [pending[i].expression for i in indices]
    probas = [0.9, 0.5, 0.2]
    r5 = AsyncMock(return_value={"r5_composite_score": 0.5, "r5_cost_usd": 0.001})
    with _patched({"CODE_GEN_SOFT_REG_MODE": "soft", "CODE_GEN_SOFT_REG_W_ALIGNMENT": 1.0,
                   "CODE_GEN_SOFT_REG_W_COMPLEXITY": 1.0, "CODE_GEN_SOFT_REG_W_ORIGINALITY": 1.0,
                   "CODE_GEN_SOFT_REG_ALIGNMENT_TOPK": 1, "CODE_GEN_SOFT_REG_LAMBDA": 0.5},
                  {"rank(close)": 0.9, "rank(volume)": 0.1, "rank(returns)": 1.0},
                  r5_mock=r5):
        _call(state, indices, cand_exprs, probas)
    # Exactly the top-1 (local 0 → global 5) is judged, with its OWN hypothesis.
    assert r5.await_count == 1
    _, kw = r5.call_args
    assert kw["hypothesis_statement"] == "HYP_A"   # read from pending[5], not [0]
    assert kw["expression"] == "rank(close)"
    assert pending[5].metrics["_soft_reg_alignment_judged"] is True
    assert pending[5].metrics["_soft_reg_r5_composite"] == pytest.approx(0.5)
    assert pending[2].metrics["_soft_reg_alignment_judged"] is False
    assert pending[8].metrics["_soft_reg_alignment_judged"] is False
    # un-judged candidates never carry R5 detail keys
    assert "_soft_reg_r5_composite" not in pending[2].metrics


def test_r5_failure_soft_fails_to_zero_alignment():
    pending = [_alpha("rank(close)", "HYP_A"), _alpha("rank(volume)", "HYP_B")]
    state = _state(pending)
    r5 = AsyncMock(side_effect=RuntimeError("LLM down"))
    with _patched({"CODE_GEN_SOFT_REG_MODE": "soft", "CODE_GEN_SOFT_REG_W_ALIGNMENT": 1.0,
                   "CODE_GEN_SOFT_REG_ALIGNMENT_TOPK": 1},
                  {"rank(close)": 0.9, "rank(volume)": 0.1},
                  r5_mock=r5):
        # gather(return_exceptions=True) → the helper must not raise
        keep, skip, out_probas = _call(state, [0, 1], [a.expression for a in pending], [0.9, 0.5])
    judged = pending[0] if pending[0].metrics["_soft_reg_alignment_judged"] else pending[1]
    # judged-but-failed → composite None → alignment leg contributes 0 penalty
    assert judged.metrics["_soft_reg_alignment_judged"] is True
    assert judged.metrics["_soft_reg_r5_composite"] is None
    assert judged.metrics["_soft_reg_alignment_pen"] == 0.0


def test_mode_off_returns_inputs_untouched():
    pending = [_alpha("rank(close)", "h0")]
    state = _state(pending)
    with _patched({"CODE_GEN_SOFT_REG_MODE": "off"}, {"rank(close)": 0.5}):
        keep, skip, out_probas = _call(state, [0], ["rank(close)"], [0.9])
    assert keep == [0] and skip == [] and out_probas == [0.9]
    assert pending[0].metrics == {}  # no stamping when off
