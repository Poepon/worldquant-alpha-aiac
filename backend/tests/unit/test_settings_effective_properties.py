"""Unit tests for BRAIN Consultant mode effective_* synthesizer properties.

Plan §1 + §12. Validates:
* User mode (flag=False, default): effective_* returns User values
* Consultant mode (flag=True via _flag_override_cache): effective_* returns Consultant values
* max() semantics on sharpe (Consultant 1.58 only wins if SHARPE_MIN < 1.58)
* env→override→clear precedence round-trip

These properties are NOT in SUPPORTED_FLAGS (they synthesize, not toggle)
— verified by attempting to override them and confirming they still return
the synthesized value.
"""
from __future__ import annotations

import pytest

from backend.config import _flag_override_cache, settings


@pytest.fixture(autouse=True)
def _clear_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


# ---------------------------------------------------------------------------
# User mode (default — flag=False, cache empty)
# ---------------------------------------------------------------------------

def test_user_mode_effective_default_test_period():
    """User mode → FULL_TEST_PERIOD (P2Y0M)."""
    assert settings.ENABLE_BRAIN_CONSULTANT_MODE is False
    assert settings.effective_default_test_period == "P2Y0M"


def test_user_mode_effective_sharpe_submit_min():
    """User mode → SHARPE_MIN (1.5 default)."""
    assert settings.effective_sharpe_submit_min == settings.SHARPE_MIN


def test_user_mode_effective_region_universes():
    """User mode → USA only with TOP3000."""
    assert settings.effective_region_universes == {"USA": "TOP3000"}


# ---------------------------------------------------------------------------
# Consultant mode (flag=True)
# ---------------------------------------------------------------------------

def test_consultant_mode_effective_default_test_period():
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    assert settings.effective_default_test_period == "P0Y"


def test_consultant_mode_effective_sharpe_submit_min():
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    # max(SHARPE_MIN=1.5, CONSULTANT_SHARPE_SUBMIT_MIN=1.58) == 1.58
    assert settings.effective_sharpe_submit_min == max(
        settings.SHARPE_MIN, settings.CONSULTANT_SHARPE_SUBMIT_MIN
    )
    assert settings.effective_sharpe_submit_min == 1.58


def test_consultant_mode_effective_region_universes():
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    result = settings.effective_region_universes
    # Phase 1: 5 regions
    assert set(result.keys()) == {"USA", "CHN", "HKG", "JPN", "EUR"}
    assert result["USA"] == "TOP3000"
    assert result["HKG"] == "TOP500"
    assert result["JPN"] == "TOP1600"


def test_consultant_mode_returns_copy_not_reference():
    """effective_region_universes returns a fresh dict — mutation by caller
    must NOT affect future reads."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    r1 = settings.effective_region_universes
    r1["HACKED"] = "evil"
    r2 = settings.effective_region_universes
    assert "HACKED" not in r2


# ---------------------------------------------------------------------------
# max() semantics: user override SHARPE_MIN higher than 1.58
# ---------------------------------------------------------------------------

def test_consultant_mode_respects_higher_user_sharpe_min(monkeypatch):
    """If user already configures SHARPE_MIN > 1.58, Consultant must not lower it."""
    monkeypatch.setattr(settings, "SHARPE_MIN", 2.0)
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    assert settings.effective_sharpe_submit_min == 2.0


# ---------------------------------------------------------------------------
# env → override → clear round-trip
# ---------------------------------------------------------------------------

def test_round_trip_override_set_then_clear():
    # initial: cache empty → user mode
    assert settings.effective_sharpe_submit_min == settings.SHARPE_MIN

    # set override → consultant mode
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    assert settings.effective_sharpe_submit_min == 1.58

    # clear → fall back to env default (False)
    del _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"]
    assert settings.effective_sharpe_submit_min == settings.SHARPE_MIN


# ---------------------------------------------------------------------------
# Anti-regression: effective_* must NOT be overridable as flags
# ---------------------------------------------------------------------------

def test_effective_properties_are_not_overridable():
    """Even if someone puts effective_* in _flag_override_cache, it must NOT
    affect the property output (hook only fires on ENABLE_ prefix)."""
    _flag_override_cache["effective_sharpe_submit_min"] = 999.0  # should be ignored
    assert settings.effective_sharpe_submit_min == settings.SHARPE_MIN
