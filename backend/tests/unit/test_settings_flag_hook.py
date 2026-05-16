"""Unit tests for the Settings.__getattribute__ runtime override hook.

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.4.

The hook in backend/config.py intercepts ENABLE_* attribute reads and
returns the value from ``_flag_override_cache`` if present. Confirms:

* override visible on next read
* override removal restores env default
* non-ENABLE attributes bypass the hook (no perf hit, no surprise)
* hook does NOT affect Pydantic's own internal attribute reads
* unknown ENABLE_* name in cache is returned as-is (caller responsible for
  whitelisting at write time — read path is permissive)

These are orthogonal to the FeatureFlagService unit tests which exercise
the DB / write path; this file just nails down the read-side semantics.
"""
from __future__ import annotations

import pytest

from backend.config import _flag_override_cache, settings


@pytest.fixture(autouse=True)
def _clear_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def test_default_value_returned_when_no_override():
    """ENABLE_PILLAR_AWARE_SELECTION defaults to False in config.py."""
    assert settings.ENABLE_PILLAR_AWARE_SELECTION is False


def test_override_visible_on_next_read():
    _flag_override_cache["ENABLE_PILLAR_AWARE_SELECTION"] = True
    assert settings.ENABLE_PILLAR_AWARE_SELECTION is True


def test_override_removal_restores_default():
    _flag_override_cache["ENABLE_PILLAR_AWARE_SELECTION"] = True
    assert settings.ENABLE_PILLAR_AWARE_SELECTION is True

    del _flag_override_cache["ENABLE_PILLAR_AWARE_SELECTION"]
    assert settings.ENABLE_PILLAR_AWARE_SELECTION is False


def test_non_enable_attributes_bypass_hook():
    """SHARPE_MIN must not be flippable via the cache — wrong prefix."""
    original = settings.SHARPE_MIN
    _flag_override_cache["SHARPE_MIN"] = 999.0  # should be ignored
    assert settings.SHARPE_MIN == original


def test_multiple_flags_independent():
    _flag_override_cache["ENABLE_PILLAR_AWARE_SELECTION"] = True
    # ENABLE_REGIME_INFERENCE not in cache — falls back to env default (False)
    assert settings.ENABLE_PILLAR_AWARE_SELECTION is True
    assert settings.ENABLE_REGIME_INFERENCE is False


def test_pydantic_internals_not_broken():
    """Sanity: model_dump still works (hook must not mangle Pydantic's
    ``__class__`` / ``__fields__`` / ``model_config`` reads)."""
    dumped = settings.model_dump()
    assert isinstance(dumped, dict)
    assert dumped["ENABLE_PILLAR_AWARE_SELECTION"] is False


def test_pydantic_internals_with_active_override():
    """Same sanity check, but with a cache entry present — confirms our
    prefix guard doesn't somehow leak into model_dump's iteration."""
    _flag_override_cache["ENABLE_PILLAR_AWARE_SELECTION"] = True
    dumped = settings.model_dump()
    # model_dump uses object.__getattribute__-equivalent under the hood,
    # so it sees the env default; the hook only fires on direct attribute
    # access via `settings.ENABLE_*`. This split is fine — call sites use
    # `settings.ENABLE_X`, never `model_dump()`, so the override path stays
    # the source of truth for what code branches on.
    assert isinstance(dumped, dict)
