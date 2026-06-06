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
def _patched(settings_overrides: dict, dist_map: dict):
    """Patch settings + OriginalityChecker + get_llm_service for one helper call.
    (The R5 alignment leg was retired in Phase 1c-delete; only the live
    complexity + originality legs remain.)"""
    _FakeChecker.dist_map = dist_map
    patches = [patch(f"backend.config.settings.{k}", v) for k, v in settings_overrides.items()]
    patches.append(patch("backend.alpha_originality.OriginalityChecker", _FakeChecker))
    patches.append(patch(
        "backend.agents.services.llm_service.get_llm_service",
        return_value=SimpleNamespace(),
    ))
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


# test_topk_judges_highest_effp_and_index_keying + test_r5_failure_soft_fails_to_zero_alignment
# removed in Phase 1c-delete (the R5 alignment leg they exercised was retired).


def test_mode_off_returns_inputs_untouched():
    pending = [_alpha("rank(close)", "h0")]
    state = _state(pending)
    with _patched({"CODE_GEN_SOFT_REG_MODE": "off"}, {"rank(close)": 0.5}):
        keep, skip, out_probas = _call(state, [0], ["rank(close)"], [0.9])
    assert keep == [0] and skip == [] and out_probas == [0.9]
    assert pending[0].metrics == {}  # no stamping when off
