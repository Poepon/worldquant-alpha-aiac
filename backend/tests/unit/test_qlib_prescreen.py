"""Phase 3 Q10 PR1c: prescreen_alpha skeleton + soft-fail contract (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §7.2.

PR1c ships QlibEngine with all tiers disabled — every prescreen_alpha call
returns verdict='skip'. These tests verify the SKELETON CONTRACT only:
  - skip_reason='untranslatable' when translator returns None
  - skip_reason='engine_disabled' when engine.kind='disabled' (default)
  - PrescreenResult shape + elapsed_ms populated
  - Never raises (even with deeply broken inputs)
  - Mode flows from settings (and override)

PR1d will add 8 pandas-engine parity tests; PR1d-then-on adds the pass/
reject verdict tests with real metrics. This file's tests stay valid.
"""
from __future__ import annotations

import pytest

from backend.qlib_prescreen import (
    PrescreenResult,
    QlibEngine,
    _reset_engine_for_test,
    prescreen_alpha,
)


@pytest.fixture(autouse=True)
def _reset_engine():
    """Each test gets a fresh QlibEngine singleton."""
    _reset_engine_for_test()
    yield
    _reset_engine_for_test()


# ---------------------------------------------------------------------------
# Skeleton contract — PR1c default behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prescreen_translatable_expression_returns_skip_engine_disabled():
    """Translatable expression but engine disabled → skip:engine_disabled."""
    r = await prescreen_alpha("ts_mean(close, 20)", region="USA")
    assert r.verdict == "skip"
    assert r.skip_reason == "engine_disabled"
    assert r.qlib_expression == "Mean($close, 20)"  # translation succeeded
    assert r.engine_kind == "disabled"
    assert r.brain_expression == "ts_mean(close, 20)"
    assert r.region == "USA"


@pytest.mark.asyncio
async def test_prescreen_untranslatable_returns_skip_untranslatable():
    """Untranslatable (group_neutralize) → skip:untranslatable before engine probe."""
    r = await prescreen_alpha("group_neutralize(close, sector)", region="USA")
    assert r.verdict == "skip"
    assert r.skip_reason == "untranslatable"
    assert r.qlib_expression is None
    assert r.translation_error == "translator returned None"


@pytest.mark.asyncio
async def test_prescreen_empty_expression_returns_skip_untranslatable():
    """Empty string → translator returns None → skip:untranslatable."""
    r = await prescreen_alpha("", region="USA")
    assert r.verdict == "skip"
    assert r.skip_reason == "untranslatable"


@pytest.mark.asyncio
async def test_prescreen_unknown_field_cascades_to_untranslatable():
    """Unknown field (fnd28_assets) cascades up through the translator → skip."""
    r = await prescreen_alpha("ts_mean(fnd28_assets, 20)", region="USA")
    assert r.verdict == "skip"
    assert r.skip_reason == "untranslatable"


# ---------------------------------------------------------------------------
# Result shape + bookkeeping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prescreen_elapsed_ms_populated():
    """elapsed_ms is a non-negative int on every code path."""
    r = await prescreen_alpha("ts_mean(close, 20)")
    assert isinstance(r.elapsed_ms, int)
    assert r.elapsed_ms >= 0


@pytest.mark.asyncio
async def test_prescreen_local_metrics_unset_on_skip():
    """Skip path leaves local_sharpe / local_ic as None."""
    r = await prescreen_alpha("ts_mean(close, 20)")
    assert r.local_sharpe is None
    assert r.local_ic is None


@pytest.mark.asyncio
async def test_prescreen_mode_at_call_defaults_from_settings(monkeypatch):
    """mode_at_call defaults to settings.QLIB_PRESCREEN_MODE."""
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_MODE", "soft", raising=False)
    r = await prescreen_alpha("ts_mean(close, 20)")
    assert r.mode_at_call == "soft"


@pytest.mark.asyncio
async def test_prescreen_mode_override_wins_over_settings(monkeypatch):
    """Explicit mode kwarg overrides settings."""
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_MODE", "shadow", raising=False)
    r = await prescreen_alpha("ts_mean(close, 20)", mode="hard")
    assert r.mode_at_call == "hard"


# ---------------------------------------------------------------------------
# Never-raises contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prescreen_never_raises_on_unbalanced_paren():
    """Unbalanced paren in translator → None → skip, NOT a raised exception."""
    try:
        r = await prescreen_alpha("ts_mean(close, 20")
    except Exception as e:
        pytest.fail(f"prescreen_alpha must never raise; got: {e}")
    assert r.verdict == "skip"


@pytest.mark.asyncio
async def test_prescreen_never_raises_on_none_input():
    """None brain_expr is gracefully handled."""
    try:
        r = await prescreen_alpha(None)  # type: ignore
    except Exception as e:
        pytest.fail(f"prescreen_alpha must never raise; got: {e}")
    assert r.verdict == "skip"


# ---------------------------------------------------------------------------
# QlibEngine probe
# ---------------------------------------------------------------------------

def test_engine_probe_returns_disabled_in_pr1c():
    """PR1c skeleton always selects 'disabled' tier."""
    engine = QlibEngine()
    assert engine.kind == "disabled"


def test_engine_evaluate_returns_none_in_pr1c():
    """PR1c skeleton evaluate() always returns None."""
    engine = QlibEngine()
    assert engine.evaluate("Mean($close, 20)", "USA", "TOP3000") is None


def test_engine_singleton_is_reusable():
    """_get_engine returns the same instance until _reset_engine_for_test."""
    from backend.qlib_prescreen import _get_engine
    e1 = _get_engine()
    e2 = _get_engine()
    assert e1 is e2
    _reset_engine_for_test()
    e3 = _get_engine()
    assert e3 is not e1


# ---------------------------------------------------------------------------
# Dataclass sanity
# ---------------------------------------------------------------------------

def test_prescreen_result_defaults():
    """Default PrescreenResult has expected None / default fields."""
    r = PrescreenResult(brain_expression="x", region="USA", universe="TOP3000")
    assert r.verdict == "skip"
    assert r.engine_kind == "disabled"
    assert r.local_sharpe is None
    assert r.local_ic is None
    assert r.elapsed_ms == 0
    assert r.mode_at_call == "shadow"
    assert r.extra == {}
